"""Container lifecycle (B4, B5, B17, B18): one hardened container per session,
on its own internal network, with a smokescreen sidecar as its only way out
and the OTel collector attached while it runs.

`worker_kwargs` and `proxy_kwargs` are pure, so the hardening is unit-tested
without Docker; `DockerRuntime` runs them (tests/security/ runs it for real).
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import shlex
import shutil
import socket
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from vigild.config import Config
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.executors.egress import acl_yaml, allowlist, proxy_config_yaml
from vigild.verifier import MAX_REPORT, REPORT_DIR, RunResult

LABEL = "dev.vigil.session"
HARNESS_CAPS = ("SETUID", "SETGID", "KILL")


def _hardening(cfg: Config) -> dict[str, Any]:
    return {
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": cfg.vigild.container.pids_limit,
        "mem_limit": cfg.vigild.container.memory,
        "privileged": False,
    }


def _mount(source: str, target: str, read_only: bool) -> dict[str, Any]:
    return {"Target": target, "Source": source, "Type": "bind", "ReadOnly": read_only}


def worker_kwargs(cfg: Config, spec: SessionSpec, *, network: str) -> dict[str, Any]:
    uid = cfg.vigild.container.uid
    col = cfg.egress.collector
    proxy = f"http://{cfg.egress.proxy.alias}:{cfg.egress.proxy.port}"
    attrs = {
        "vigil.session_id": spec.session_id,
        "vigil.job_id": spec.job_id,
        "vigil.task_id": spec.task_id or "",
        "vigil.role": spec.role.value,
        "vigil.repo": spec.repo,
        "vigil.backend": spec.preset,
        "openinference.project.name": "vigil",
    }
    env = {
        "VIGIL_ROLE": spec.role.value,
        "VIGIL_MODEL": spec.model,
        "VIGIL_GATEWAY_URL": f"http://{cfg.egress.gateway.host}:{cfg.egress.gateway.port}",
        "VIGIL_MAX_TURNS": str(spec.max_turns),
        "VIGIL_MAX_BUDGET_USD": str(spec.max_budget_usd),
        "VIGIL_REQUIRE_PLUGIN": "guard",
        "VIGIL_AGENT_UID": str(uid),
        "PYTHONDONTWRITEBYTECODE": "1",  # no __pycache__ in the worker's tree (round 4)
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "NO_PROXY": f"{col.host},localhost,127.0.0.1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{col.host}:{col.port}",
        "OTEL_SERVICE_NAME": f"vigil-{spec.role.value}",
        "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{k}={v}" for k, v in attrs.items()),
    }
    if spec.traceparent:
        env["TRACEPARENT"] = spec.traceparent
    if spec.max_output_tokens:
        env["VIGIL_MAX_OUTPUT_TOKENS"] = str(spec.max_output_tokens)
    mounts = [
        _mount(str(spec.clone), "/work", read_only=False),
        _mount(str(spec.session_dir / "packet.md"), "/vigil/packet.md", read_only=True),
        # The harness's result bundle (untrusted: vigild reads it with
        # safe_read_bytes and checks its tip against the head on the result line).
        _mount(str(spec.session_dir / "out"), "/vigil/out", read_only=False),
        *[_mount(m.host, m.container, m.read_only) for m in spec.mounts],
    ]
    return {
        "image": spec.image or cfg.vigild.images.claude_executor,
        "name": f"vigil-{spec.session_id}",
        # PID 1 is the harness, as root with only these capabilities; the CLI,
        # test_cmd and git run as `uid` with none (review round 2, N1 and N11).
        "user": "0:0",
        "network": network,
        "environment": env,
        "mounts": mounts,
        "tmpfs": {"/tmp": f"rw,size=1g,uid={uid},gid={uid},mode=1777"},  # noqa: S108
        "stdin_open": True,
        "labels": {LABEL: spec.session_id, "dev.vigil.role": spec.role.value},
        "working_dir": "/work",
        **_hardening(cfg),
        "cap_add": list(HARNESS_CAPS),
    }


def as_agent(cfg: Config, command: str) -> str:
    """A shell line that runs `command` as the agent uid under a root PID 1,
    the way the harness runs the agent (`exit` keeps sh from exec'ing)."""
    uid = cfg.vigild.container.uid
    drop = f"setpriv --reuid={uid} --regid={uid} --clear-groups --inh-caps=-all"
    return f"{drop} -- sh -c {shlex.quote(command)}; exit $?"


def proxy_kwargs(cfg: Config, spec: SessionSpec) -> dict[str, Any]:
    return {
        "image": cfg.vigild.images.egress_proxy,
        "name": f"vigil-{spec.session_id}-egress",
        "command": ["--config-file=/etc/smokescreen/config.yaml"],
        "mounts": [_mount(str(spec.session_dir / "proxy"), "/etc/smokescreen", read_only=True)],
        "network": "bridge",
        "labels": {LABEL: spec.session_id, "dev.vigil.role": "egress"},
        **_hardening(cfg),
        "mem_limit": "128m",
        "pids_limit": 64,
    }


# The verifier's first step. The dev image's vigil-prepare copies the checkout
# into /work with the dependencies it installed, and starts what the suites
# need (a database on loopback); an image without one gets a plain copy.
# Exit 125 means the tree couldn't be prepared: the outcome is `unknown`.
# The verifier's container script (rounds 2-5). The per-run marker arrives on
# stdin and stdin is closed before anything else runs, so it's in no argv,
# environment or file. It isn't secret from the test, though: the script runs
# as the same uid, so a test can read it from /proc/1/mem (the reviewer did).
# It keeps accidental output from looking like a trailer; like an in-process
# rewrite of the report, a deliberate forgery stays open (architecture B33),
# with the draft PR and review as the backstop. The checkout is prepared (exit 125
# means it couldn't be: `unknown`), test_cmd runs under pipefail and errexit,
# and then one trailer: BEGIN, each expected report (pytest.xml, vitest-N.xml)
# in base64, and END with the exit code. vigild accepts reports only from a
# trailer that ends the stream.
PREPARE = (
    "read -r m || exit 125; exec </dev/null; "
    "if [ -x /usr/local/bin/vigil-prepare ]; then /usr/local/bin/vigil-prepare || exit 125; "
    "else cp -a /src/. /work/ || exit 125; fi; "
    f"mkdir -p {REPORT_DIR}; cd /work; "
    'bash -o pipefail -o errexit -c "$0"; rc=$?; '
    'printf "\\n%s BEGIN\\n" "$m"; '
    f"for f in {REPORT_DIR}/pytest.xml {REPORT_DIR}/vitest-*.xml; do "
    '[ -f "$f" ] || continue; printf "%s %s\\n" "$m" "$(basename "$f")"; '
    f'head -c {MAX_REPORT} "$f" | base64 -w0; printf "\\n"; done; '
    'printf "%s END %s\\n" "$m" "$rc"; exit $rc'
)
# The verifier's script for a hidden run (hotfix 5, round 2). PID 1 is root with
# only the capabilities below; it starts vigild's subject broker (as root), then
# runs everything else as the agent uid with no capabilities and no new
# privileges. The report dir and the broker's FIFO are the agent's, mode 0700, so
# the subject, which the broker runs as another uid (10002), can reach neither.
# No setpriv in the image means exit 125 (unknown), never a run as root.
PREPARE_SUBJECT = (
    "read -r m || exit 125; exec </dev/null; "
    "command -v setpriv >/dev/null || exit 125; "
    "A='setpriv --reuid={uid} --regid={uid} --clear-groups --inh-caps=-all "
    "--bounding-set=-all --no-new-privs --'; "
    f"mkdir -m 700 {REPORT_DIR} && chown {{uid}}:{{uid}} {REPORT_DIR} || exit 125; "
    "mkfifo -m 600 /vigil-ipc/queue && chown {uid}:{uid} /vigil-ipc/queue || exit 125; "
    # the subject: a copy owned by 10002, mode 0500/0400, in a tmpfs; the mount it
    # came from sits under a root-only directory. The test process (10001) can't read
    # either, so no import of subject code, by any syntax, can succeed in it.
    "cp -a /vigil-src/subject/. /subject/ && chown -R {subj}:{subj} /subject "
    "&& chmod -R u=rX,go= /subject || exit 125; "
    "sh /vigil/broker.sh & "
    "if [ -x /usr/local/bin/vigil-prepare ]; then $A /usr/local/bin/vigil-prepare || exit 125; "
    "else $A cp -a /src/. /work/ || exit 125; fi; "
    "cd /work; "
    '$A bash -o pipefail -o errexit -c "$0"; rc=$?; '
    'printf "\\n%s BEGIN\\n" "$m"; '
    f"for f in {REPORT_DIR}/pytest.xml {REPORT_DIR}/vitest-*.xml; do "
    '[ -f "$f" ] || continue; printf "%s %s\\n" "$m" "$(basename "$f")"; '
    f'head -c {MAX_REPORT} "$f" | base64 -w0; printf "\\n"; done; '
    'printf "%s END %s\\n" "$m" "$rc"; exit $rc'
)
SUBJECT_UID = 10002  # the uid every subject runs as (hiddenkit/broker.sh)
SUBJECT_CAPS = ("SETUID", "SETGID", "SETPCAP", "KILL", "CHOWN", "DAC_OVERRIDE", "FOWNER")
REPORT_NAME = re.compile(r"^(pytest|vitest-[0-9]{1,3})\.xml$")


def split_reports(text: str, marker: str) -> tuple[str, dict[str, bytes]]:
    """The test output, and the reports from the trailer that ends it (round
    4, item 3). A marker block anywhere else, a trailer that isn't last, or a
    report name the script doesn't write, counts for nothing."""
    import base64
    import binascii

    lines = text.rstrip("\n").split("\n")
    if not lines or not lines[-1].startswith(f"{marker} END "):
        return text, {}
    begin = f"{marker} BEGIN"
    starts = [i for i, line in enumerate(lines) if line == begin]
    if not starts:
        return text, {}
    k = starts[-1]
    body = lines[k + 1 : -1]
    reports: dict[str, bytes] = {}
    if len(body) % 2:
        return text, {}
    for name_line, data in zip(body[0::2], body[1::2], strict=True):
        if not name_line.startswith(marker + " "):
            return text, {}
        name = name_line[len(marker) + 1 :].strip()
        if REPORT_NAME.match(name):
            with contextlib.suppress(binascii.Error, ValueError):
                reports[name] = base64.b64decode(data, validate=True)
    out = "\n".join(lines[:k])
    return (out[:-1] if out.endswith("\n") else out), reports


def verifier_kwargs(
    cfg: Config,
    image: str,
    source: Path,
    command: str,
    env: Mapping[str, str] | None = None,
    subject: Path | None = None,
    kit: Path | None = None,
) -> dict[str, Any]:
    """The verifier's container (owner direction, round 2): the repo's dev
    image, vigild's checkout read-only at /src, no network at all, the agent
    uid with no capabilities, and nothing else mounted. The suites write (build
    output, caches), so they run on a copy in the container's own tmpfs at
    /work; the checkout itself can't change."""
    uid = cfg.vigild.container.uid
    tmp = f"rw,exec,uid={uid},gid={uid}"
    return {
        "image": image,
        "entrypoint": ["sh", "-c"],
        "command": [
            PREPARE_SUBJECT.format(uid=uid, subj=SUBJECT_UID) if subject else PREPARE,
            command,
        ],
        "user": "0:0" if subject else f"{uid}:{uid}",
        "network_mode": "none",
        "mounts": [
            _mount(str(source), "/src", read_only=True),
            # the code under test, for a hidden run: read-only, and run only as a subprocess
            *([_mount(str(subject), "/vigil-src/subject", read_only=True)] if subject else []),
            *([_mount(str(kit), "/vigil", read_only=True)] if subject and kit else []),
        ],
        "tmpfs": {
            "/tmp": f"{tmp},size=2g,mode=1777",  # noqa: S108
            "/work": f"{tmp},size=4g,mode=0755",
            **(
                {
                    "/vigil-ipc": f"rw,uid={uid},gid={uid},mode=0700,size=64m",  # the queue
                    "/vigil-src": "rw,uid=0,gid=0,mode=0700,size=1m",  # root-only
                    "/subject": f"rw,exec,uid={SUBJECT_UID},gid={SUBJECT_UID},mode=0700,size=2g",
                }
                if subject
                else {}
            ),
        },
        "working_dir": "/work",
        "environment": {  # caches on the container's own tmpfs, never the source
            "HOME": "/tmp/home",  # noqa: S108
            "XDG_CACHE_HOME": "/tmp/cache",  # noqa: S108
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/tmp/pycache",  # noqa: S108
            "NO_COLOR": "1",
            "PYTEST_ADDOPTS": f"--junitxml={REPORT_DIR}/pytest.xml -p no:cacheprovider",
            "pnpm_config_verify_deps_before_run": "false",
            **({"VIGIL_SUBJECT": "/subject", "VIGIL_BROKER": "/vigil-ipc"} if subject else {}),
            **(env or {}),
        },
        "stdin_open": True,  # the report marker, then closed
        "labels": {LABEL: f"verify-{source.parent.name}", "dev.vigil.role": "verifier"},
        **_hardening(cfg),
        **({"cap_add": list(SUBJECT_CAPS)} if subject else {}),
        "mem_limit": cfg.vigild.container.verifier_memory,
        "pids_limit": max(cfg.vigild.container.pids_limit, 2048),
    }


def write_kit(dest: Path) -> Path:
    """vigild's broker and its client, copied next to the checkout (never mounted
    from the repo itself) for the container to mount read-only at /vigil."""
    src = Path(__file__).resolve().parents[1] / "hiddenkit"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("broker.sh", "run.sh"):
        target = dest / name
        target.write_text((src / name).read_text())
        target.chmod(0o644)
    dest.chmod(0o755)
    return dest


class DockerTestRunner:
    """Runs one test_cmd in a fresh verifier container and reads its output
    stream itself: at most `max_output` bytes are kept, and the container is
    killed at `timeout_s`. The verdict is the exit code docker reports."""

    def __init__(self, cfg: Config, client: Any) -> None:
        self.cfg = cfg
        self.client = client
        # one runner per vigild, shared by the verifier and dev-image validation
        self._slots = threading.BoundedSemaphore(cfg.vigild.container.verifier_concurrency)

    def run(
        self,
        image: str,
        source: Path,
        command: str,
        *,
        timeout_s: int,
        max_output: int,
        env: Mapping[str, str] | None = None,
        subject: Path | None = None,
    ) -> RunResult:
        with self._slots:  # round 4, item 12: verifier containers one at a time
            return self._run(
                image,
                source,
                command,
                timeout_s=timeout_s,
                max_output=max_output,
                env=env,
                subject=subject,
            )

    def _run(
        self,
        image: str,
        source: Path,
        command: str,
        *,
        timeout_s: int,
        max_output: int,
        env: Mapping[str, str] | None = None,
        subject: Path | None = None,
    ) -> RunResult:
        import docker.errors

        marker = f"VIGIL-REPORT-{secrets.token_hex(16)}"
        kit = write_kit(source.parent / "kit") if subject else None
        kw = verifier_kwargs(self.cfg, image, source, command, env, subject, kit)
        kw["mounts"] = _docker_mounts(kw["mounts"])
        c = self.client.containers.create(**kw)
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            with contextlib.suppress(Exception):
                c.kill()

        timer = threading.Timer(timeout_s, _kill)
        buf = bytearray()
        seen = 0
        try:
            sock = c.attach_socket(params={"stdin": 1, "stream": 1})
            c.start()
            raw = getattr(sock, "_sock", sock)
            raw.sendall((marker + "\n").encode())
            with suppress(OSError):
                raw.shutdown(socket.SHUT_WR)
            sock.close()
            timer.start()
            for chunk in c.logs(stream=True, follow=True, stdout=True, stderr=True):
                seen += len(chunk)
                buf += chunk
                if len(buf) > max_output:
                    del buf[: len(buf) - max_output]  # keep the tail
            code = int(c.wait().get("StatusCode", -1))
        finally:
            timer.cancel()
            with suppress(docker.errors.APIError):
                c.remove(force=True)
        text, reports = split_reports(bytes(buf).decode(errors="replace"), marker)
        if timed_out.is_set():
            return RunResult(None, text, timed_out=True, truncated=seen > max_output)
        return RunResult(code, text, truncated=seen > max_output, reports=reports)


def write_session_files(cfg: Config, spec: SessionSpec) -> None:
    sd = spec.session_dir
    (sd / "proxy").mkdir(parents=True, exist_ok=True)
    if (sd / "out").exists():
        shutil.rmtree(sd / "out")
    (sd / "out").mkdir()
    (sd / "out").chmod(0o777)
    (sd / "packet.md").write_text(spec.packet)
    (sd / "proxy" / "acl.yaml").write_text(acl_yaml(allowlist(cfg, spec.role)))
    (sd / "proxy" / "config.yaml").write_text(proxy_config_yaml(cfg))
    for p in (sd, sd / "proxy"):
        p.chmod(0o755)
    for f in (sd / "packet.md", sd / "proxy" / "acl.yaml", sd / "proxy" / "config.yaml"):
        f.chmod(0o644)


def _docker_mounts(raw: list[dict[str, Any]]) -> list[Any]:
    import docker.types

    return [
        docker.types.Mount(m["Target"], m["Source"], type="bind", read_only=m["ReadOnly"])
        for m in raw
    ]


class DockerRuntime:
    def __init__(self, cfg: Config, client: Any = None) -> None:
        import docker

        self.cfg = cfg
        self.client = client or docker.from_env()

    @contextmanager
    def _network(self, spec: SessionSpec, on_event: EventSink) -> Iterator[str]:
        """The session's internal network, its egress sidecar and the collector."""
        import docker.errors

        write_session_files(self.cfg, spec)
        net_name = f"vigil-{spec.session_id}"
        net = self.client.networks.create(
            net_name, driver="bridge", internal=True, labels={LABEL: spec.session_id}
        )
        proxy = collector = None
        try:
            pk = proxy_kwargs(self.cfg, spec)
            pk["mounts"] = _docker_mounts(pk["mounts"])
            proxy = self.client.containers.run(detach=True, **pk)
            net.connect(proxy, aliases=[self.cfg.egress.proxy.alias])
            try:
                collector = self.client.containers.get(self.cfg.vigild.collector.container)
                net.connect(collector, aliases=[self.cfg.egress.collector.host])
            except docker.errors.NotFound:
                collector = None
                on_event({"type": "state", "data": {"collector": "not running; no traces"}})
            yield net_name
        finally:
            if proxy is not None:
                with suppress(docker.errors.APIError):
                    proxy.remove(force=True)
            if collector is not None:
                with suppress(docker.errors.APIError):
                    net.disconnect(collector, force=True)
            with suppress(docker.errors.APIError):
                net.remove()

    def run(self, spec: SessionSpec, gateway_token: str, on_event: EventSink) -> ExecOutcome:
        import docker.errors

        with self._network(spec, on_event) as net_name:
            wk = worker_kwargs(self.cfg, spec, network=net_name)
            wk["mounts"] = _docker_mounts(wk["mounts"])
            worker = self.client.containers.create(**wk)
            try:
                sock = worker.attach_socket(params={"stdin": 1, "stream": 1})
                worker.start()
                raw_sock = getattr(sock, "_sock", sock)
                raw_sock.sendall((json.dumps({"gateway_token": gateway_token}) + "\n").encode())
                with suppress(OSError):
                    raw_sock.shutdown(socket.SHUT_WR)
                sock.close()
                return self._follow(worker, spec, on_event)
            finally:
                with suppress(docker.errors.APIError):
                    worker.remove(force=True)

    def probe(self, spec: SessionSpec, command: str, timeout: int = 120) -> tuple[int, str]:
        """Run `command` as the agent uid in the session's exact container,
        network and proxy, under a root PID 1 standing in for the harness. The
        security tests and `vigil doctor` use it."""
        import docker.errors

        with self._network(spec, lambda e: None) as net_name:
            wk = worker_kwargs(self.cfg, spec, network=net_name)
            wk["mounts"] = _docker_mounts(wk["mounts"])
            wk.update(
                entrypoint=["sh", "-c"],
                command=[as_agent(self.cfg, command)],
                stdin_open=False,
            )
            worker = self.client.containers.create(**wk)
            try:
                worker.start()
                code = int(worker.wait(timeout=timeout).get("StatusCode", -1))
                out = worker.logs(stdout=True, stderr=True).decode(errors="replace")
                return code, out
            finally:
                with suppress(docker.errors.APIError):
                    worker.remove(force=True)

    def stop(self, session_id: str) -> None:
        """Kill a live session's containers (the gateway calls this when
        metering fails); `run` then sees the worker exit and cleans up."""
        for c in self.client.containers.list(filters={"label": f"{LABEL}={session_id}"}):
            with contextlib.suppress(Exception):
                c.kill()

    def reap(self) -> int:
        """At startup: remove every container and network a session left behind."""
        n = 0
        for c in self.client.containers.list(all=True, filters={"label": LABEL}):
            with contextlib.suppress(Exception):
                c.remove(force=True)
                n += 1
        for net in self.client.networks.list(filters={"label": LABEL}):
            with contextlib.suppress(Exception):
                net.remove()
        return n

    def _follow(self, worker: Any, spec: SessionSpec, on_event: EventSink) -> ExecOutcome:
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            with contextlib.suppress(Exception):  # it may already have exited
                worker.kill()

        timer = threading.Timer(spec.max_minutes * 60, _kill)
        timer.start()
        result: dict[str, Any] = {}
        buf = b""
        try:
            for chunk in worker.logs(stream=True, follow=True, stdout=True, stderr=False):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "vigil_event" in msg:
                        on_event(msg["vigil_event"])
                    elif "vigil_result" in msg:
                        result = msg["vigil_result"]
            code = int(worker.wait().get("StatusCode", -1))
        finally:
            timer.cancel()
        stderr = worker.logs(stdout=False, stderr=True, tail=40).decode(errors="replace")
        if timed_out.is_set():
            return ExecOutcome("timeout", code, result, f"over {spec.max_minutes} min")
        if code != 0 or result.get("status") != "done":
            return ExecOutcome("failed", code, result, result.get("error") or stderr[-2000:])
        return ExecOutcome("done", code, result)


def session_dir(cfg: Config, session_id: str) -> Path:
    return cfg.vigild.data_path / "sessions" / session_id
