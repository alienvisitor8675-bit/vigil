"""Container lifecycle (B4, B5, B17, B18): one hardened container per session,
on its own internal network, with a smokescreen sidecar as its only way out
and the OTel collector attached while it runs.

`worker_kwargs` and `proxy_kwargs` are pure, so the hardening is unit-tested
without Docker; `DockerRuntime` runs them (tests/security/ runs it for real).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import secrets
import shlex
import shutil
import socket
import tarfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from vigild.config import Config, SandboxHost
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.executors.egress import acl_yaml, allowlist, proxy_config_yaml
from vigild.verifier import MAX_REPORT, REPORT_DIR, RunResult

LABEL = "dev.vigil.session"
OUTPUT_SCHEMA = "/vigil/output_schema.json"
RESUME = "/vigil/resume.jsonl"
HOOK_DIR = "/run/vigil"  # root-owned tmpfs: the Stop hook's inputs (2a round 2, A6/B12)
HARNESS_CAPS = ("SETUID", "SETGID", "KILL")


def _hardening(cfg: Config, host: SandboxHost | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if host is not None and host.remote:
        # every vigild container on a remote host runs inside its slice (ADR-0004 rule 5)
        extra["cgroup_parent"] = host.cgroup_slice
    return {
        **extra,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": cfg.vigild.container.pids_limit,
        "mem_limit": cfg.vigild.container.memory,
        "privileged": False,
    }


def _mount(source: str, target: str, read_only: bool) -> dict[str, Any]:
    return {"Target": target, "Source": source, "Type": "bind", "ReadOnly": read_only}


def worker_kwargs(
    cfg: Config, spec: SessionSpec, *, network: str, host: SandboxHost | None = None
) -> dict[str, Any]:
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
        "VIGIL_GATEWAY_URL": (
            host.gateway_url
            if host is not None and host.remote
            else f"http://{cfg.egress.gateway.host}:{cfg.egress.gateway.port}"
        ),
        "VIGIL_MAX_TURNS": str(spec.max_turns),
        "VIGIL_MAX_BUDGET_USD": str(spec.max_budget_usd),
        "VIGIL_REQUIRE_PLUGIN": "guard",
        "VIGIL_AGENT_UID": str(uid),
        "VIGIL_CHECKPOINT_SECONDS": str(cfg.vigild.scheduler.checkpoint_seconds),
        "VIGIL_HOOK_DIR": HOOK_DIR,
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
    if spec.hold_first_stop:  # tests only: see SessionSpec
        env["VIGIL_HOLD_FIRST_STOP"] = "1"
    mounts = [
        _mount(str(spec.clone), "/work", read_only=False),
        _mount(str(spec.session_dir / "packet.md"), "/vigil/packet.md", read_only=True),
        # The harness's result bundle (untrusted: vigild reads it with
        # safe_read_bytes and checks its tip against the head on the result line).
        _mount(str(spec.session_dir / "out"), "/vigil/out", read_only=False),
        *[_mount(m.host, m.container, m.read_only) for m in spec.mounts],
    ]
    if spec.resume is not None:
        # the transcript to resume (phase 2a): read by the harness's session store
        mounts.append(_mount(str(spec.session_dir / "resume.jsonl"), RESUME, read_only=True))
        env["VIGIL_RESUME_SESSION"] = spec.resume.session_id
        env["VIGIL_RESUME_FORK"] = "1" if spec.resume.fork else "0"
    if spec.output_schema is not None:
        # a reviewer's or auditor's answer schema (phase 2a): structured output
        mounts.append(
            _mount(str(spec.session_dir / "output_schema.json"), OUTPUT_SCHEMA, read_only=True)
        )
        env["VIGIL_OUTPUT_SCHEMA"] = OUTPUT_SCHEMA
    return {
        "image": spec.image or cfg.vigild.images.claude_executor,
        "name": f"vigil-{spec.session_id}",
        # PID 1 is the harness, as root with only these capabilities; the CLI,
        # test_cmd and git run as `uid` with none (review round 2, N1 and N11).
        "user": "0:0",
        "network": network,
        "environment": env,
        "mounts": mounts,
        "tmpfs": {
            "/tmp": f"rw,size=1g,uid={uid},gid={uid},mode=1777",  # noqa: S108
            # the Stop hook's inputs, the harness's to write (2a round 2, A6/B12)
            HOOK_DIR: "rw,size=1m,uid=0,gid=0,mode=0755",
        },
        "stdin_open": True,
        "labels": {LABEL: spec.session_id, "dev.vigil.role": spec.role.value},
        "working_dir": "/work",
        **_hardening(cfg, host),
        "cap_add": list(HARNESS_CAPS),
    }


def as_agent(cfg: Config, command: str) -> str:
    """A shell line that runs `command` as the agent uid under a root PID 1,
    the way the harness runs the agent (`exit` keeps sh from exec'ing)."""
    uid = cfg.vigild.container.uid
    drop = f"setpriv --reuid={uid} --regid={uid} --clear-groups --inh-caps=-all"
    return f"{drop} -- sh -c {shlex.quote(command)}; exit $?"


def proxy_kwargs(cfg: Config, spec: SessionSpec, host: SandboxHost | None = None) -> dict[str, Any]:
    return {
        "image": cfg.vigild.images.egress_proxy,
        "name": f"vigil-{spec.session_id}-egress",
        "command": ["--config-file=/etc/smokescreen/config.yaml"],
        "mounts": [_mount(str(spec.session_dir / "proxy"), "/etc/smokescreen", read_only=True)],
        "network": "bridge",
        "labels": {LABEL: spec.session_id, "dev.vigil.role": "egress"},
        **_hardening(cfg, host),
        "mem_limit": "128m",
        "pids_limit": 64,
    }


# The verifier's container script (rounds 2-5; phase 2a, item 0). It runs as
# root with only SETUID and SETGID. The per-run marker arrives on its stdin,
# which is closed before anything else runs, so the marker is in no argv,
# environment or file, and only in root's memory: the prepare hook and the
# test run as the agent uid (setpriv, no capabilities), which can't open
# /proc/1/mem. The dev image's vigil-prepare copies the checkout into /work
# with the dependencies it installed and starts what the suites need (a
# database on loopback); an image without one gets a plain copy. Exit 125
# means the tree couldn't be prepared: the outcome is `unknown`. test_cmd runs
# under pipefail and errexit, and then one trailer: BEGIN, each expected report
# (pytest.xml, vitest-N.xml, regular files only: root never follows a link the
# test planted) in base64, and END with the exit code. vigild accepts reports
# only from a trailer that ends the stream. An in-process rewrite of a report
# stays open (architecture B33), with the draft PR and review as the backstop.
AGENT = "setpriv --reuid={uid} --regid={uid} --clear-groups --inh-caps=-all --"


def prepare_script(uid: int) -> str:
    a = AGENT.format(uid=uid)
    return (
        "read -r m || exit 125; exec </dev/null; "
        f"{a} mkdir -p {REPORT_DIR} || exit 125; "
        "if [ -x /usr/local/bin/vigil-prepare ]; "
        f"then {a} /usr/local/bin/vigil-prepare || exit 125; "
        f"else {a} cp -a /src/. /work/ || exit 125; fi; "
        f'cd /work; {a} bash -o pipefail -o errexit -c "$0"; rc=$?; '
        'printf "\\n%s BEGIN\\n" "$m"; '
        f"for f in {REPORT_DIR}/pytest.xml {REPORT_DIR}/vitest-*.xml; do "
        '[ -f "$f" ] && [ ! -L "$f" ] || continue; printf "%s %s\\n" "$m" "$(basename "$f")"; '
        f'head -c {MAX_REPORT} "$f" | base64 -w0; printf "\\n"; done; '
        'printf "%s END %s\\n" "$m" "$rc"; exit $rc'
    )


PREPARE = prepare_script(10001)
VERIFIER_CAPS = ("SETUID", "SETGID")
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
    # every cache, home and temp path the test process uses is made here, as 10001 and
    # mode 0700: /tmp is world-writable, so a path left for the test to create is one
    # the subject (10002) could create first and plant code in
    "for d in /tmp/pycache /tmp/home /tmp/cache /tmp/t-{uid}; do "
    "mkdir -m 700 $d && chown {uid}:{uid} $d || exit 125; done; "
    # the subject: a copy owned by 10002, mode 0500/0400, in a tmpfs; the mount it
    # came from sits under a root-only directory. The test process (10001) can't read
    # either, so no import of subject code, by any syntax, can succeed in it.
    "cp -a /vigil-src/subject/. /subject/ && chown -R {subj}:{subj} /subject "
    "&& chmod -R u=rX,go= /subject || exit 125; "
    "VIGIL_TEST_UID={uid} sh /vigil/broker.sh & "
    "if [ -x /usr/local/bin/vigil-prepare ]; then $A /usr/local/bin/vigil-prepare || exit 125; "
    "else $A cp -a /src/. /work/ || exit 125; fi; "
    # what the prepare step started as the test uid (a database, say) must not outlive it:
    # the subject shares the loopback. Stop every process of that uid, then check.
    "$A sh -c 'kill -KILL -1' >/dev/null 2>&1; "
    "sh /vigil/guard.sh {uid} wait || exit 125; "
    # `cp -a` gave /work the checkout's mode: it holds the hidden tests, so 10001's only
    "$A chmod 700 /work || exit 125; "
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
    host: SandboxHost | None = None,
) -> dict[str, Any]:
    """The verifier's container (owner direction, round 2): the repo's dev
    image, vigild's checkout read-only at /src, no network at all, the script
    as root with only SETUID and SETGID, the hook and the test as the agent uid
    with no capabilities, and nothing else mounted. The suites write (build
    output, caches), so they run on a copy in the container's own tmpfs at
    /work; the checkout itself can't change."""
    uid = cfg.vigild.container.uid
    tmp = f"rw,exec,uid={uid},gid={uid}"
    return {
        "image": image,
        "entrypoint": ["sh", "-c"],
        "command": [
            PREPARE_SUBJECT.format(uid=uid, subj=SUBJECT_UID) if subject else prepare_script(uid),
            command,
        ],
        "user": "0:0",  # the script; the hook and the test run as `uid` (item 0)
        "network_mode": "none",
        "mounts": [
            _mount(str(source), "/src", read_only=True),
            # the code under test, for a hidden run: read-only, and run only as a subprocess
            *([_mount(str(subject), "/vigil-src/subject", read_only=True)] if subject else []),
            *([_mount(str(kit), "/vigil", read_only=True)] if subject and kit else []),
        ],
        "tmpfs": {
            "/tmp": f"{tmp},size=2g,mode=1777",  # noqa: S108
            # a hidden run's /work holds the hidden tests: the subject has no use for it
            "/work": f"{tmp},size=4g,mode={'0700' if subject else '0755'}",
            **(
                {
                    "/vigil-ipc": f"rw,uid={uid},gid={uid},mode=0700,size=64m",  # the queue
                    # make_fixture's base: the test's, 0711 (10002 traverses, never lists or writes)
                    "/vigil-fx": f"rw,uid={uid},gid={uid},mode=0711,size=64m",
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
            **(
                {
                    "VIGIL_SUBJECT": "/subject",
                    "VIGIL_BROKER": "/vigil-ipc",
                    "VIGIL_FIXTURES": "/vigil-fx",
                    "TMPDIR": f"/tmp/t-{uid}",  # noqa: S108  (made by PID 1, 10001's, 0700)
                }
                if subject
                else {}
            ),
            **(env or {}),
        },
        "stdin_open": True,  # the report marker, then closed
        "labels": {LABEL: f"verify-{source.parent.name}", "dev.vigil.role": "verifier"},
        **_hardening(cfg, host),
        "cap_add": list(SUBJECT_CAPS if subject else VERIFIER_CAPS),
        "mem_limit": cfg.vigild.container.verifier_memory,
        "pids_limit": max(cfg.vigild.container.pids_limit, 2048),
    }


def write_kit(dest: Path) -> Path:
    """vigild's broker and its client, copied next to the checkout (never mounted
    from the repo itself) for the container to mount read-only at /vigil."""
    src = Path(__file__).resolve().parents[1] / "hiddenkit"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("broker.sh", "run.sh", "guard.sh"):
        target = dest / name
        target.write_text((src / name).read_text())
        target.chmod(0o644)
    dest.chmod(0o755)
    return dest


def deps_kwargs(
    cfg: Config, image: str, work: Path, *, network: str, host: SandboxHost | None = None
) -> dict[str, Any]:
    """A deps job's build container (B39): the repo's dev image, its
    manifests and lockfiles at /work (nothing else of the repo, no .git), the
    agent uid with no capabilities, its own internal network whose proxy
    allows the registries only. No secret, no gateway token, no model."""
    uid = cfg.vigild.container.uid
    proxy = f"http://{cfg.egress.proxy.alias}:{cfg.egress.proxy.port}"
    return {
        "image": image,
        "entrypoint": ["sh", "-c"],
        "user": f"{uid}:{uid}",
        "network": network,
        "environment": {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "HOME": "/tmp/home",  # noqa: S108
            # a dev image installs offline and frozen; this one resolves
            "UV_OFFLINE": "false",
            "UV_FROZEN": "false",
            "UV_NO_CACHE": "false",
            "UV_CACHE_DIR": "/tmp/uv-cache",  # noqa: S108
            "UV_PYTHON_DOWNLOADS": "never",
            "npm_config_cache": "/tmp/npm-cache",  # noqa: S108
            "pnpm_config_store_dir": "/tmp/pnpm-store",  # noqa: S108
        },
        "mounts": [_mount(str(work), "/work", read_only=False)],
        "tmpfs": {"/tmp": f"rw,exec,size=2g,uid={uid},gid={uid},mode=1777"},  # noqa: S108
        "working_dir": "/work",
        "labels": {LABEL: f"deps-{work.name}", "dev.vigil.role": "deps"},
        **_hardening(cfg, host),
    }


class DockerTestRunner:
    """Runs one test_cmd in a fresh verifier container and reads its output
    stream itself: at most `max_output` bytes are kept, and the container is
    killed at `timeout_s`. The verdict is the exit code docker reports."""

    def __init__(self, cfg: Config, client: Any, host: SandboxHost | None = None) -> None:
        self.cfg = cfg
        self.client = client
        self.host = host or cfg.sandbox_hosts.local
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
        kw = verifier_kwargs(self.cfg, image, source, command, env, subject, kit, self.host)
        require_local_files(self.host, kw["mounts"])
        require_image(self.client, image)
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
            _close_attach(sock)
            timer.start()
            # closed when done: an open stream's response is finalised at exit,
            # after stderr closes ("I/O operation on closed file", item 0)
            with _closed(c.logs(stream=True, follow=True, stdout=True, stderr=True)) as stream:
                for chunk in stream:
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


def proxy_files(cfg: Config, spec: SessionSpec, host: SandboxHost | None) -> dict[str, str]:
    return {
        "acl.yaml": acl_yaml(allowlist(cfg, spec.role, host)),
        "config.yaml": proxy_config_yaml(cfg, host),
    }


def write_session_files(cfg: Config, spec: SessionSpec, host: SandboxHost | None = None) -> None:
    sd = spec.session_dir
    (sd / "proxy").mkdir(parents=True, exist_ok=True)
    if (sd / "out").exists():
        shutil.rmtree(sd / "out")
    (sd / "out").mkdir()
    (sd / "out").chmod(0o777)
    (sd / "packet.md").write_text(spec.packet)
    if spec.output_schema is not None:
        (sd / "output_schema.json").write_text(json.dumps(spec.output_schema))
        (sd / "output_schema.json").chmod(0o644)
    if spec.resume is not None:
        (sd / "resume.jsonl").write_text("".join(json.dumps(e) + "\n" for e in spec.resume.entries))
        (sd / "resume.jsonl").chmod(0o644)
    (sd / "proxy" / "acl.yaml").write_text(acl_yaml(allowlist(cfg, spec.role, host)))
    (sd / "proxy" / "config.yaml").write_text(proxy_config_yaml(cfg, host))
    for p in (sd, sd / "proxy"):
        p.chmod(0o755)
    for f in (sd / "packet.md", sd / "proxy" / "acl.yaml", sd / "proxy" / "config.yaml"):
        f.chmod(0o644)


def tar_files(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _close_attach(sock: Any) -> None:
    """Close an attach socket and the HTTP response docker-py keeps on it;
    left to the garbage collector, the response flushes a closed file."""
    response = getattr(sock, "_response", None)
    with suppress(Exception):
        sock.close()
    if response is not None:
        with suppress(Exception):
            response.close()


@contextmanager
def _closed(stream: Any) -> Iterator[Any]:
    """A docker log stream, closed when done: an open one's response is
    finalised at exit, after stderr closes, and prints "I/O operation on
    closed file" (phase 2a, item 0)."""
    try:
        yield stream
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            with suppress(Exception):
                close()


def _docker_mounts(raw: list[dict[str, Any]]) -> list[Any]:
    import docker.types

    return [
        docker.types.Mount(m["Target"], m["Source"], type="bind", read_only=m["ReadOnly"])
        for m in raw
    ]


def require_image(client: Any, image: str) -> None:
    """A session or grader never builds or pulls: a missing image refuses the
    run and names the build to run first (ADR-0004 §5)."""
    import docker.errors

    from vigild.executors.hosts import HostRefused

    try:
        client.images.get(image)
    except docker.errors.ImageNotFound as e:
        raise HostRefused(f"build {image} first") from e


def require_local_files(host: SandboxHost, mounts: list[dict[str, Any]]) -> None:
    """A remote host has none of vigild's files. Files reach it only through
    `docker cp` or a tar stream over the Docker API (ADR-0004), which is not
    built yet, so a spec that needs a bind mount is refused there."""
    if host.remote and mounts:
        from vigild.executors.hosts import HostRefused

        raise HostRefused(
            f"{host.name}: files reach a remote host over the Docker API, which is not built yet"
        )


class DockerRuntime:
    def __init__(
        self,
        cfg: Config,
        client: Any = None,
        host: SandboxHost | None = None,
        prune_ok: Callable[[str], bool] | None = None,
    ) -> None:
        import docker

        self.prune_ok = prune_ok or (lambda session: False)  # unknown: prune nothing
        self.cfg = cfg
        self.client = client or docker.from_env()
        self.host = host or cfg.sandbox_hosts.local

    @contextmanager
    def _network(self, spec: SessionSpec, on_event: EventSink) -> Iterator[str]:
        """The session's internal network, its egress sidecar and the collector."""
        import docker.errors

        write_session_files(self.cfg, spec, self.host)
        net_name = f"vigil-{spec.session_id}"
        net = self.client.networks.create(
            net_name, driver="bridge", internal=True, labels={LABEL: spec.session_id}
        )
        proxy = collector = None
        try:
            require_image(self.client, self.cfg.vigild.images.egress_proxy)
            pk = proxy_kwargs(self.cfg, spec, self.host)
            if self.host.remote:
                # the remote host has none of vigild's files: the ACL goes in over the API
                pk.pop("mounts")
                proxy = self.client.containers.create(**pk)
                proxy.put_archive(
                    "/etc/smokescreen", tar_files(proxy_files(self.cfg, spec, self.host))
                )
                proxy.start()
            else:
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

        self._prepare(spec)
        with self._network(spec, on_event) as net_name:
            wk = worker_kwargs(self.cfg, spec, network=net_name, host=self.host)
            require_local_files(self.host, wk["mounts"])
            wk["mounts"] = _docker_mounts(wk["mounts"])
            worker = self.client.containers.create(**wk)
            try:
                sock = worker.attach_socket(params={"stdin": 1, "stream": 1})
                worker.start()
                raw_sock = getattr(sock, "_sock", sock)
                raw_sock.sendall((json.dumps({"gateway_token": gateway_token}) + "\n").encode())
                with suppress(OSError):
                    raw_sock.shutdown(socket.SHUT_WR)
                _close_attach(sock)
                return self._follow(worker, spec, on_event)
            finally:
                with suppress(docker.errors.APIError):
                    worker.remove(force=True)

    def _prepare(self, spec: SessionSpec) -> None:
        if self.host.remote:  # the disk budget (ADR-0004 rule 6): vigil's own labels only
            from vigild.executors.disk import enforce_budget

            enforce_budget(
                self.client, int(self.host.disk_budget_gb * (1 << 30)), may_prune=self.prune_ok
            )
        require_image(self.client, spec.image or self.cfg.vigild.images.claude_executor)

    def probe(self, spec: SessionSpec, command: str, timeout: int = 120) -> tuple[int, str]:
        """Run `command` as the agent uid in the session's exact container,
        network and proxy, under a root PID 1 standing in for the harness. The
        security tests and `vigil doctor` use it."""
        import docker.errors

        self._prepare(spec)
        with self._network(spec, lambda e: None) as net_name:
            wk = worker_kwargs(self.cfg, spec, network=net_name, host=self.host)
            require_local_files(self.host, wk["mounts"])
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

    def run_deps(
        self, image: str, work: Path, command: str, timeout_s: int = 900
    ) -> tuple[int, str]:
        """A deps job's package-manager run (B39): `deps_kwargs` on its own
        internal network behind a registry-only proxy. Returns the exit code
        and the output's tail."""
        import docker.errors

        from vigild.jobs.model import Role

        sid = f"deps-{secrets.token_hex(5)}"
        spec = SessionSpec(
            session_id=sid, role=Role.DEPS, job_id="", task_id=None, repo="", preset="none",
            model="", clone=work, session_dir=self.cfg.vigild.data_path / "sessions" / sid,
            max_turns=0, max_minutes=timeout_s // 60, max_budget_usd=0.0,
        )  # fmt: skip
        with self._network(spec, lambda e: None) as net_name:
            require_image(self.client, image)
            kw = deps_kwargs(self.cfg, image, work, network=net_name, host=self.host)
            require_local_files(self.host, kw["mounts"])
            kw["mounts"] = _docker_mounts(kw["mounts"])
            c = self.client.containers.create(command=[command], **kw)
            try:
                c.start()
                try:
                    code = int(c.wait(timeout=timeout_s).get("StatusCode", -1))
                except Exception:
                    with suppress(docker.errors.APIError):
                        c.kill()
                    return -1, f"the package manager took over {timeout_s} s"
                out = c.logs(stdout=True, stderr=True).decode(errors="replace")
                return code, out[-20000:]
            finally:
                with suppress(docker.errors.APIError):
                    c.remove(force=True)

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
            with _closed(
                worker.logs(stream=True, follow=True, stdout=True, stderr=False)
            ) as stream:
                for chunk in stream:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "vigil_event" in msg:
                            on_event(msg["vigil_event"])
                        elif "vigil_transcript" in msg:
                            on_event({"type": "transcript", "data": msg["vigil_transcript"]})
                        elif "vigil_checkpoint" in msg:
                            on_event({"type": "checkpoint", "data": msg["vigil_checkpoint"]})
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
