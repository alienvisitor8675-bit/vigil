"""The rules every remote sandbox host must meet (ADR-0004 §3): `vigil doctor`
prints one line per host per rule, and vigild refuses to start if a configured
remote host fails one. The SSH shell attempt, the DNS lookup and the probe
container are injected, so the rules are tested offline with fakes; the real
ones run only against a host the owner configures.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from vigild.config import Config, SandboxHost, is_private_address
from vigild.doctor import Check
from vigild.executors.disk import reclaimable_bytes, usage_bytes
from vigild.executors.hosts import Hosts, normalize_arch

GPU_RUNTIMES = ("nvidia", "gpu", "rocm", "amd")
HOST_PORTS = (22, 2375, 2376, 11434)  # a host's own: ssh, docker, a model server
INTERNET = (("1.1.1.1", 443), ("example.com", 443))
GB = 1 << 30


class HostRuleError(RuntimeError):
    pass


@dataclass
class ProbeReport:
    """What a probe container saw from inside a sandbox on the host."""

    gateway_health: bool
    reached: dict[str, bool]  # target name -> could it connect, direct or through the proxy
    gpu_nodes: list[str] = field(default_factory=list)
    memory_max: str | None = None  # the slice's memory.max
    cpu_max: str | None = None  # the slice's cpu.max


class SshProbe(Protocol):
    def shell_refused(self, endpoint: str) -> tuple[bool, str]: ...


class Prober(Protocol):
    def probe(self, host: SandboxHost) -> ProbeReport: ...


MARKER = "vigil-plain-shell"
# Only sshd's refusal shows the key forbids forwarding; "open failed: connect failed"
# means forwarding is allowed and the target merely refused (Opus review, round 2)
FORWARD_REFUSED = ("administratively prohibited",)


@dataclass
class SshRun:
    """One ssh invocation: its exit code (None: still running when stopped),
    stdout and stderr."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""


class SshTransport(Protocol):
    def run(self, argv: list[str]) -> SshRun: ...

    def forward(self, argv: list[str]) -> SshRun: ...


class RealSsh:
    """The real ssh. `run` has stdin closed, as `docker system dial-stdio` is
    tried. `forward` starts the port forward, makes one connection through it
    (a refusal only shows when something connects), and returns what ssh said."""

    def run(self, argv: list[str]) -> SshRun:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
        return SshRun(p.returncode, p.stdout, p.stderr)

    def forward(self, argv: list[str]) -> SshRun:
        import time

        with socket.socket() as s:  # a free local port for the forward
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        argv = [a.replace("{port}", str(port)) for a in argv]
        p = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )  # fmt: skip
        try:
            for _ in range(50):  # wait for the listener, or for ssh to give up
                if p.poll() is not None:
                    break
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                except OSError:
                    time.sleep(0.2)
                    continue
                time.sleep(1)  # let the channel open (or be refused)
                break
        finally:
            code = p.poll()
            if code is None:
                p.terminate()
            try:
                out, err = p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
        return SshRun(code, out, err)


class SubprocessSsh:
    """The restricted-key check (ADR-0004 rule 1) over the Docker context's own
    target and port, with the key ssh resolves for that target, as Docker does.
    A restricted `authorized_keys` entry runs `docker system dial-stdio` and
    nothing else, with no pty and no port forward. Anything unproven fails."""

    def __init__(self, transport: SshTransport | None = None) -> None:
        self.t = transport or RealSsh()

    @staticmethod
    def _base(endpoint: str) -> tuple[list[str], str]:
        u = urlsplit(endpoint)
        target = f"{u.username}@{u.hostname}" if u.username else str(u.hostname)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if u.port:
            cmd += ["-p", str(u.port)]
        return cmd, target

    def shell_refused(self, endpoint: str) -> tuple[bool, str]:
        base, target = self._base(endpoint)
        try:
            return self._check(base, target)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"could not try the restricted-key checks ({type(e).__name__})"

    def _check(self, base: list[str], target: str) -> tuple[bool, str]:
        a = self.t.run([*base, "-T", target, f"echo {MARKER}"])
        if a.returncode == 255:
            return False, "ssh could not connect (a broken key or an unreachable host)"
        if MARKER in a.stdout or MARKER in a.stderr:
            return False, "a plain command ran: restrict the key in authorized_keys"
        if a.stdout.strip():
            return False, "the session is not `docker system dial-stdio` on a closed stdin"
        b = self.t.run([*base, "-tt", target, f"echo {MARKER}"])
        if b.returncode == 255:
            return False, "ssh could not connect for the pty check"
        if MARKER in b.stdout or "pty allocation request failed" not in b.stderr.lower():
            return False, "a pty was not refused: add no-pty to the key"
        fwd = [*base, "-N", "-L", "127.0.0.1:{port}:127.0.0.1:22",
               "-o", "ExitOnForwardFailure=yes", target]  # fmt: skip
        c = self.t.forward(fwd)
        if not any(m in c.stderr.lower() for m in FORWARD_REFUSED):
            return False, "a port forward was not refused: add no-port-forwarding to the key"
        return True, "plain command, pty and port forward refused"


def _model_endpoints(cfg: Config) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for name, p in cfg.backends.presets.items():
        u = urlsplit(p.base_url)
        if u.hostname:
            out[f"model:{name}"] = (u.hostname, u.port or (443 if u.scheme == "https" else 80))
    return out


def egress_targets(cfg: Config, host: SandboxHost) -> dict[str, tuple[str, int]]:
    """Everything a sandbox on `host` must fail to reach."""
    t: dict[str, tuple[str, int]] = {}
    for h, port in INTERNET:
        t[f"internet:{h}"] = (h, port)
    assert host.private_address
    for port in HOST_PORTS:
        t[f"host-port:{port}"] = (host.private_address, port)
    for other in cfg.sandbox_hosts.remote_hosts():
        if other.name != host.name and other.private_address:
            t[f"private:{other.name}"] = (other.private_address, 22)
    if cfg.sandbox_hosts.gateway_bind:
        # vigild's own address, on ports that aren't the gateway's
        t["private:vigild-ssh"] = (cfg.sandbox_hosts.gateway_bind, 22)
        t["private:vigild-api"] = (cfg.sandbox_hosts.gateway_bind, cfg.vigild.api.port)
    t.update(_model_endpoints(cfg))
    return t


def _rule(host: SandboxHost, n: int) -> str:
    return f"sandbox host {host.name}: rule {n}"


def _rule1(
    host: SandboxHost, endpoint: str, ssh: SshProbe, resolve: Callable[[str], list[str]]
) -> Check:
    name = f"{_rule(host, 1)} ssh only, private, restricted key"
    if not endpoint.startswith("ssh://"):
        return Check(name, False, "the context's endpoint must be ssh://, never tcp://")
    target = urlsplit(endpoint).hostname or ""
    addrs = resolve(target)
    if not addrs or not all(is_private_address(a) for a in addrs):
        return Check(name, False, "the ssh target does not resolve to a private address")
    refused, note = ssh.shell_refused(endpoint)
    return Check(name, refused, note)


def _rule2(host: SandboxHost, client: Any) -> Check:
    name = f"{_rule(host, 2)} pinned engine with userns-remap"
    version = str(client.version().get("Version"))
    if version != host.engine_pin:
        return Check(name, False, f"engine {version} is not the host's pin {host.engine_pin}")
    info = client.info()
    opts = [str(o) for o in info.get("SecurityOptions") or []]
    if any("rootless" in o for o in opts):
        return Check(name, False, "rootless mode is not supported; use userns-remap")
    if not any(o.split(",")[0] == "name=userns" for o in opts):
        return Check(name, False, "userns is not in docker info's security options")
    try:
        arch = normalize_arch(str(info.get("Architecture")))
    except Exception:
        arch = str(info.get("Architecture"))
    if arch != host.arch:
        return Check(name, False, f"docker info says arch {arch}, config says {host.arch}")
    return Check(name, True, f"engine {version}, userns, {arch}")


def _rule3(cfg: Config, host: SandboxHost, report: ProbeReport) -> Check:
    name = f"{_rule(host, 3)} egress to the gateway only"
    if not report.gateway_health:
        return Check(name, False, "the probe could not reach the gateway's health route")
    expected = egress_targets(cfg, host)
    untried = sorted(set(expected) - set(report.reached))
    if untried:
        return Check(name, False, f"the probe did not try {', '.join(untried)}")
    reached = sorted(n for n, ok in report.reached.items() if ok)
    if reached:
        return Check(name, False, f"the probe reached {', '.join(reached)}")
    return Check(name, True, f"gateway only; {len(expected)} targets unreachable")


def _rule4(host: SandboxHost, client: Any, report: ProbeReport) -> Check:
    name = f"{_rule(host, 4)} no GPU"
    runtime = str(client.info().get("DefaultRuntime", ""))
    if any(g in runtime.lower() for g in GPU_RUNTIMES):
        return Check(name, False, f"the default runtime is a GPU runtime ({runtime})")
    if report.gpu_nodes:
        return Check(name, False, f"the probe sees {', '.join(report.gpu_nodes)}")
    return Check(name, True, "no device requests; default runtime " + runtime)


def _cpu_limit(cpu_max: str) -> float | None:
    parts = cpu_max.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        return int(parts[0]) / int(parts[1])
    except ValueError, ZeroDivisionError:
        return None


def _rule5(host: SandboxHost, report: ProbeReport) -> Check:
    name = f"{_rule(host, 5)} caps in the {host.cgroup_slice} slice"
    mem = report.memory_max
    if mem is None or not mem.isdigit():
        return Check(name, False, "the slice has no memory limit")
    if int(mem) > host.memory_gb * GB:
        return Check(name, False, f"the slice's memory limit is above {host.memory_gb} GB")
    cpu = _cpu_limit(report.cpu_max or "")
    if cpu is None:
        return Check(name, False, "the slice has no cpu limit")
    if cpu > host.cpus + 1e-6:
        return Check(name, False, f"the slice's cpu limit is above {host.cpus}")
    return Check(name, True, f"memory {int(mem) / GB:.1f} GB, cpu {cpu:g}")


def _rule6(host: SandboxHost, client: Any) -> Check:
    name = f"{_rule(host, 6)} disk budget"
    used_b = usage_bytes(client)
    used = used_b / GB
    note = f"{used:.1f} GB of {host.disk_budget_gb:g} GB (vigil-labelled images and containers)"
    if used <= host.disk_budget_gb:
        return Check(name, True, note)
    live = (used_b - reclaimable_bytes(client)) / GB
    if live > host.disk_budget_gb:
        return Check(
            name,
            False,
            f"over budget: {note}; live work alone is {live:.1f} GB, so new sessions are refused",
        )
    return Check(name, False, f"over budget: {note}; the next session prunes stopped work to fit")


def check_host(
    cfg: Config,
    host: SandboxHost,
    client: Any,
    *,
    endpoint: str,
    ssh: SshProbe,
    resolve: Callable[[str], list[str]],
    prober: Prober,
) -> list[Check]:
    out = [_safe(f"{_rule(host, 1)} ssh only, private, restricted key",
                 lambda: _rule1(host, endpoint, ssh, resolve))]  # fmt: skip
    out.append(
        _safe(f"{_rule(host, 2)} pinned engine with userns-remap", lambda: _rule2(host, client))
    )
    try:
        report = prober.probe(host)
    except Exception as e:  # report the kind of failure, never a value
        kind = type(e).__name__
        out.append(
            Check(f"{_rule(host, 3)} egress to the gateway only", False, f"probe failed: {kind}")
        )
        out.append(Check(f"{_rule(host, 4)} no GPU", False, f"probe failed: {kind}"))
        out.append(
            Check(
                f"{_rule(host, 5)} caps in the {host.cgroup_slice} slice",
                False,
                f"probe failed: {kind}",
            )
        )
    else:
        out.append(_rule3(cfg, host, report))
        out.append(_safe(f"{_rule(host, 4)} no GPU", lambda: _rule4(host, client, report)))
        out.append(_rule5(host, report))
    out.append(_safe(f"{_rule(host, 6)} disk budget", lambda: _rule6(host, client)))
    return out


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as e:
        return Check(name, False, type(e).__name__)


def _resolve(name: str) -> list[str]:
    try:
        return sorted({str(i[4][0]) for i in socket.getaddrinfo(name, None)})
    except OSError:
        return []


def check_hosts(
    cfg: Config,
    hosts: Hosts,
    *,
    ssh: SshProbe | None = None,
    resolve: Callable[[str], list[str]] = _resolve,
    prober_for: Callable[[SandboxHost, Any], Prober] | None = None,
    lookup: Callable[[str], str] | None = None,
) -> list[Check]:
    """Every rule for every configured remote host. The default `local` has
    no remote rules, so with none configured this touches nothing."""
    from vigild.executors.hosts import endpoint_of

    out: list[Check] = []
    for host in cfg.sandbox_hosts.remote_hosts():
        try:
            endpoint = (lookup or endpoint_of)(host.docker_context)
            client = hosts.client(host)
        except Exception as e:
            out.append(
                Check(
                    f"{_rule(host, 1)} ssh only, private, restricted key", False, type(e).__name__
                )
            )
            continue
        make = prober_for or (lambda h, c: DockerProber(cfg, c))
        out += check_host(
            cfg, host, client, endpoint=endpoint, ssh=ssh or SubprocessSsh(),
            resolve=resolve, prober=make(host, client),
        )  # fmt: skip
    return out


def require_hosts(cfg: Config, hosts: Hosts, **kw: Any) -> list[Check]:
    """vigild's start-up gate: a remote host that fails a rule stops it."""
    checks = check_hosts(cfg, hosts, **kw)
    bad = [c for c in checks if c.ok is False]
    if bad:
        raise HostRuleError(
            "a sandbox host fails its rules: " + "; ".join(f"{c.name} ({c.note})" for c in bad)
        )
    return checks


# --- the probe container -----------------------------------------------------------

PROBE_SCRIPT = r"""
import glob, json, os, socket, urllib.request
targets = json.loads(os.environ["PROBE_TARGETS"])
proxy = os.environ["HTTP_PROXY"].split("//", 1)[1]
ph, pp = proxy.rsplit(":", 1)
def direct(h, p):
    try:
        socket.create_connection((h, p), timeout=3).close(); return True
    except OSError:
        return False
def via_proxy(h, p):
    try:
        s = socket.create_connection((ph, int(pp)), timeout=5)
        s.sendall(f"CONNECT {h}:{p} HTTP/1.1\r\nHost: {h}:{p}\r\n\r\n".encode())
        first = s.recv(256).split(b"\r\n", 1)[0]; s.close()
        return b" 200" in first
    except OSError:
        return False
reached = {n: (direct(h, p) or via_proxy(h, p)) for n, (h, p) in targets.items()}
try:
    h = urllib.request.urlopen(os.environ["PROBE_HEALTH"], timeout=8)
    health = h.status == 200 and json.load(h).get("ok") is True
except Exception:
    health = False
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None
sl = os.environ["PROBE_SLICE"]
print(json.dumps({"gateway_health": health, "reached": reached,
    "gpu_nodes": sorted(glob.glob("/dev/nvidia*")),
    "memory_max": read(f"/sys/fs/cgroup/{sl}/memory.max"),
    "cpu_max": read(f"/sys/fs/cgroup/{sl}/cpu.max")}))
"""


class DockerProber:
    """The probe container (rules 3-5): a hardened container the same shape as a
    session, on its own internal network behind a smokescreen sidecar whose
    allowlist is exactly the host's gateway. It reads the slice's limits with
    the host's cgroup namespace (doctor only; read-only mount) and prints one
    JSON line. It sets no device request. The image must already be on the host
    (a build, never a pull)."""

    def __init__(self, cfg: Config, client: Any) -> None:
        self.cfg = cfg
        self.client = client

    def probe(self, host: SandboxHost) -> ProbeReport:
        import secrets

        from vigild.executors.base import SessionSpec
        from vigild.executors.egress import acl_yaml, allowlist, proxy_config_yaml
        from vigild.executors.runtime import (
            LABEL,
            _hardening,
            proxy_kwargs,
            require_image,
            tar_files,
        )
        from vigild.jobs.model import Role

        cfg, c = self.cfg, self.client
        sid = f"probe-{secrets.token_hex(4)}"
        image = cfg.vigild.images.claude_executor
        require_image(c, image)
        require_image(c, cfg.vigild.images.egress_proxy)
        spec = SessionSpec(
            session_id=sid, role=Role.COORD, job_id="", task_id=None, repo="", preset="none",
            model="", clone=cfg.vigild.data_path, session_dir=cfg.vigild.data_path / sid,
            max_turns=0, max_minutes=1, max_budget_usd=0.0,
        )  # fmt: skip
        net = c.networks.create(f"vigil-{sid}", driver="bridge", internal=True, labels={LABEL: sid})
        proxy = box = None
        try:
            pk = proxy_kwargs(cfg, spec, host)
            pk.pop("mounts")
            proxy = c.containers.create(**pk)
            proxy.put_archive(
                "/etc/smokescreen",
                tar_files(
                    {
                        "acl.yaml": acl_yaml(allowlist(cfg, Role.COORD, host)),
                        "config.yaml": proxy_config_yaml(cfg, host),
                    }
                ),
            )
            proxy.start()
            net.connect(proxy, aliases=[cfg.egress.proxy.alias])
            px = f"http://{cfg.egress.proxy.alias}:{cfg.egress.proxy.port}"
            uid = cfg.vigild.container.uid
            box = c.containers.create(
                image=image,
                name=f"vigil-{sid}",
                entrypoint=["/opt/vigil-venv/bin/python", "-c"],
                command=[PROBE_SCRIPT],
                user=f"{uid}:{uid}",
                network=f"vigil-{sid}",
                environment={
                    "HTTP_PROXY": px,
                    "HTTPS_PROXY": px,
                    "PROBE_TARGETS": json.dumps(egress_targets(cfg, host)),
                    "PROBE_HEALTH": host.gateway_url.rstrip("/") + "/health",
                    "PROBE_SLICE": host.cgroup_slice,
                },
                cgroupns="host",  # to read the slice's limits; the mount is read-only
                labels={LABEL: sid, "dev.vigil.role": "probe"},
                **_hardening(cfg, host),
            )
            box.start()
            box.wait(timeout=120)
            out = box.logs(stdout=True, stderr=False).decode(errors="replace").strip()
            data = json.loads(out.splitlines()[-1])
            return ProbeReport(
                gateway_health=bool(data["gateway_health"]),
                reached={str(k): bool(v) for k, v in data["reached"].items()},
                gpu_nodes=list(data["gpu_nodes"]),
                memory_max=data["memory_max"],
                cpu_max=data["cpu_max"],
            )
        finally:
            for ctr in (box, proxy):
                if ctr is not None:
                    with contextlib.suppress(Exception):
                        ctr.remove(force=True)
            with contextlib.suppress(Exception):
                net.remove()
