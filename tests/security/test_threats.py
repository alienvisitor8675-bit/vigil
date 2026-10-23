"""One test per threat row (docs/architecture.md "Security, as built on the
Mac"). Each starts the real worker container on its real network and proxy and
asserts the action fails. The guard test (merge/approve, layer 3) is in
test_guard_in_container.py because it needs a live model turn."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vigild.executors.base import SessionSpec
from vigild.executors.runtime import DockerRuntime
from vigild.jobs.model import Role

pytestmark = pytest.mark.docker

FETCH = (
    '/opt/vigil-venv/bin/python -c "import sys,urllib.request as u\n'
    "try:\n print(u.urlopen(sys.argv[1], timeout=10).status)\n"
    "except Exception as e:\n print('BLOCKED', type(e).__name__, e)\" "
)


# --- Merge or approve: layer 2 is "no token in the container" (P2) ---------------------


def test_merge_or_approve_no_github_token_and_no_github_route(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(
        make_spec(),
        "env; ls /run 2>&1; command -v gh || echo NO-GH; "
        + FETCH
        + "https://api.github.com/ ; "
        + FETCH
        + "https://github.com/",
    )
    assert "GH_TOKEN" not in out and "GITHUB_TOKEN" not in out and "ghs_" not in out
    assert "NO-GH" in out
    assert out.count("BLOCKED") == 2, out


# --- Push to main --------------------------------------------------------------------


def test_push_is_refused_by_the_hook_and_git_config_is_read_only(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(
        make_spec(),
        "git init -q --bare /tmp/r.git && git push /tmp/r.git HEAD:main; echo PUSH=$?; "
        "git config core.hooksPath /tmp/hooks; echo CFG=$?; "
        "echo x > /work/.git/hooks/pre-push; echo HOOK=$?",
    )
    assert "vigil: pushing from a worker container is not allowed" in out
    assert "PUSH=0" not in out and "CFG=0" not in out and "HOOK=0" not in out, out


# --- Deploy / prod: egress ---------------------------------------------------------------


def test_egress_only_reaches_the_allowlist(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(
        make_spec(),
        FETCH
        + "https://example.com/ ; "
        + FETCH
        + "https://api.fly.io/ ; "
        + FETCH
        + "https://api.anthropic.com/ ; "
        + FETCH
        + "https://pypi.org/simple/ ; "
        + FETCH
        + "https://registry.npmjs.org/ ; "
        "/opt/vigil-venv/bin/python -c \"import socket\ntry:\n socket.create_connection(('1.1.1.1',443),timeout=5); print('DIRECT-OPEN')\n"
        "except Exception as e:\n print('DIRECT-BLOCKED', e)\"",
    )
    lines = out.strip().splitlines()
    assert lines[0].startswith("BLOCKED"), out  # example.com
    assert lines[1].startswith("BLOCKED"), out  # fly
    assert lines[2].startswith("BLOCKED"), out  # no direct model host: the gateway only
    assert lines[3] == "200", out  # PyPI, for workers
    assert lines[4].startswith("BLOCKED"), out  # npm: no publishing path out (item 12)
    assert "DIRECT-BLOCKED" in out


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"gateway")

    def log_message(self, *a: object) -> None:
        pass


@pytest.fixture
def fake_gateway() -> Iterator[None]:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8788)) == 0:
            pytest.skip("port 8788 is in use (vigild running?)")
    srv = HTTPServer(("127.0.0.1", 8788), _Hello)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield
    srv.shutdown()


def test_gateway_port_is_reachable_but_no_other_host_port(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec], fake_gateway: None
) -> None:
    _, out = runtime.probe(
        make_spec(),
        FETCH
        + "http://host.docker.internal:8788/ ; "
        + FETCH
        + "http://host.docker.internal:8787/",
    )
    lines = out.strip().splitlines()
    assert lines[0] == "200", out
    assert lines[1].startswith("BLOCKED"), out


def test_coord_cannot_reach_package_registries(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(make_spec(Role.COORD), FETCH + "https://pypi.org/simple/")
    assert out.strip().startswith("BLOCKED"), out


# --- Read secrets ---------------------------------------------------------------------------


def test_no_secret_in_env_or_on_disk(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(
        make_spec(),
        "env | cut -d= -f1; ls /run/secrets 2>&1; ls -a $HOME 2>&1; "
        "test -e /var/run/docker.sock && echo SOCK || echo NOSOCK; "
        "test -f /etc/claude-code/managed-settings.json && echo MANAGED",
    )
    names = [ln for ln in out.splitlines() if ln.isupper() or "_" in ln]
    for n in names:
        assert not any(w in n for w in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")), n
    assert "No such file" in out  # /run/secrets
    assert "NOSOCK" in out and "MANAGED" in out


# --- Escape the worktree --------------------------------------------------------------------


def test_non_root_no_caps_read_only_root_and_limits(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(
        make_spec(),
        "id -u; grep CapEff /proc/self/status; "
        "touch /etc/x 2>/dev/null; echo ROOTFS=$?; touch /opt/x 2>/dev/null; echo OPT=$?; "
        "touch /tmp/x; echo TMP=$?; touch /work/x; echo WORK=$?; "
        "cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.max; "
        "ls /Users 2>&1 | head -1",
    )
    assert out.splitlines()[0] == "10001"
    assert "CapEff:\t0000000000000000" in out
    assert "ROOTFS=1" in out and "OPT=1" in out
    assert "TMP=0" in out and "WORK=0" in out
    assert "512" in out and "4294967296" in out
    assert "No such file" in out


# --- Change its own rules ---------------------------------------------------------------------


def test_github_claude_and_opencode_dirs_are_read_only(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    _, out = runtime.probe(
        make_spec(),
        "mkdir -p /work/.github/workflows 2>/dev/null; echo GH=$?; "
        "echo '{}' > /work/.claude/settings.json 2>/dev/null; echo CL=$?; "
        "echo '{}' > /work/.opencode/opencode.json 2>/dev/null; echo OC=$?; "
        "echo '{}' > /etc/claude-code/managed-settings.json 2>/dev/null; echo MS=$?",
    )
    for k in ("GH", "CL", "OC", "MS"):
        assert f"{k}=0" not in out, out


# --- Forge the harness's lines or its result (review round 2, N1 and N11) -------------------


def test_the_agent_uid_cannot_reach_pid_1(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec]
) -> None:
    """PID 1 is root (the harness); the agent uid can't write its stdout, so it
    can't forge a vigil_event or vigil_result line, read its environment, or
    signal it."""
    _, out = runtime.probe(
        make_spec(),
        "echo OWNER=$(stat -c %U /proc/1); "
        'echo \'{"vigil_result": {"status": "done"}}\' > /proc/1/fd/1 2>/dev/null; echo FD1=$?; '
        "cat /proc/1/environ >/dev/null 2>&1; echo ENV=$?; "
        "kill -0 1 2>/dev/null; echo KILL=$?; id -u",
    )
    assert "FD1=0" not in out and "ENV=0" not in out and "KILL=0" not in out, out
    assert '"vigil_result"' not in out
    assert "OWNER=root" in out and "10001" in out.splitlines(), out
