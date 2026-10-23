"""The container a session runs in, as data: hardening, mounts, env, network.
These run without Docker; tests/security/ runs the same spec for real."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vigild.config import Config
from vigild.executors.base import SessionSpec
from vigild.executors.egress import acl_yaml, allowlist, proxy_config_yaml
from vigild.executors.runtime import proxy_kwargs, worker_kwargs
from vigild.jobs.model import Role
from vigild.workspace import Workspace


@pytest.fixture
def spec(tmp_path: Path, fixture_repo: dict[str, Path], cfg: Config) -> SessionSpec:
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    clone = ws.clone("o/fixture", "fx-0001-w1", branch="vigil/fx-0001-w1", base="main")
    return SessionSpec(
        session_id="s-abc123",
        role=Role.WORKER,
        job_id="fx-0001",
        task_id="fx-0001-w1",
        repo="thomasbardhi01/vigil",
        preset="claude-sonnet",
        model="claude-sonnet-5-5",
        clone=clone,
        session_dir=tmp_path / "sess",
        max_turns=30,
        max_minutes=20,
        max_budget_usd=1.0,
        mounts=ws.overlays(clone),
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    )


def test_hardening_flags(cfg: Config, spec: SessionSpec) -> None:
    kw = worker_kwargs(cfg, spec, network="vigil-s-abc123")
    # PID 1 is the root harness with only setuid/setgid/kill; everything else
    # runs as the agent uid (review round 2, N1 and N11)
    assert kw["user"] == "0:0"
    assert kw["environment"]["VIGIL_AGENT_UID"] == "10001"
    assert kw["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert kw["read_only"] is True
    assert kw["cap_drop"] == ["ALL"]
    assert sorted(kw["cap_add"]) == ["KILL", "SETGID", "SETUID"]
    assert "no-new-privileges" in kw["security_opt"]
    assert kw["pids_limit"] == 512
    assert kw["mem_limit"] == "4g"
    assert set(kw["tmpfs"]) == {"/tmp"}
    assert kw["network"] == "vigil-s-abc123"
    assert kw["stdin_open"] is True and kw["privileged"] is False


def test_only_the_clone_packet_and_read_only_overlays_are_mounted(
    cfg: Config, spec: SessionSpec
) -> None:
    kw = worker_kwargs(cfg, spec, network="n")
    binds = {m["Target"]: m for m in kw["mounts"]}
    assert binds["/work"]["Source"] == str(spec.clone) and not binds["/work"]["ReadOnly"]
    assert binds["/vigil/packet.md"]["ReadOnly"]
    for target in (
        "/work/.github",
        "/work/.claude",
        "/work/.opencode",
        "/work/.git/hooks",
        "/work/.git/config",
    ):
        assert binds[target]["ReadOnly"], target
    assert set(binds) == {
        "/work",
        "/vigil/packet.md",
        "/vigil/out",
        "/work/.github",
        "/work/.claude",
        "/work/.opencode",
        "/work/.git/hooks",
        "/work/.git/config",
    }
    assert binds["/vigil/out"]["Source"] == str(spec.session_dir / "out")
    assert not binds["/vigil/out"]["ReadOnly"]  # the harness writes its bundle there
    assert not any(
        "docker.sock" in m["Source"] or ".config/vigil" in m["Source"] for m in kw["mounts"]
    )


def test_no_secret_in_the_environment(cfg: Config, spec: SessionSpec) -> None:
    env = worker_kwargs(cfg, spec, network="n")["environment"]
    for name in env:
        assert "KEY" not in name and "TOKEN" not in name and "SECRET" not in name, name
    assert env["VIGIL_GATEWAY_URL"] == "http://host.docker.internal:8788"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://egress:4750"
    assert "otel-collector" in env["NO_PROXY"]
    assert env["TRACEPARENT"] == spec.traceparent
    assert "vigil.task_id=fx-0001-w1" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert env["VIGIL_REQUIRE_PLUGIN"] == "guard"


def test_worker_allowlist_has_registries_but_no_github(cfg: Config) -> None:
    hosts = allowlist(cfg, Role.WORKER)
    assert "pypi.org" in hosts and "host.docker.internal" in hosts
    assert "github.com" not in hosts and "api.github.com" not in hosts
    # review r1, item 12: npm accepts PUTs on the same host as reads, and
    # smokescreen can't filter methods, so workers get no npm at all
    assert "registry.npmjs.org" not in hosts
    assert not any("anthropic" in h or "openrouter" in h for h in hosts)


def test_coord_allowlist_is_the_gateway_only(cfg: Config) -> None:
    assert allowlist(cfg, Role.COORD) == ["host.docker.internal"]


def test_acl_and_proxy_config(cfg: Config) -> None:
    acl = yaml.safe_load(acl_yaml(["pypi.org", "host.docker.internal"]))
    assert acl["default"]["action"] == "enforce"
    assert acl["default"]["allowed_domains"] == ["pypi.org", "host.docker.internal"]
    conf = yaml.safe_load(proxy_config_yaml(cfg))
    assert conf["allow_addresses"] == ["192.168.65.254:8788"]
    assert conf["allow_missing_role"] is True
    assert "allow_ranges" not in conf and "unsafe_allow_private_ranges" not in conf


def test_proxy_container(cfg: Config, spec: SessionSpec) -> None:
    kw = proxy_kwargs(cfg, spec)
    assert kw["read_only"] is True and kw["cap_drop"] == ["ALL"]
    assert kw["image"] == cfg.vigild.images.egress_proxy
    assert kw["mounts"][0]["Target"] == "/etc/smokescreen" and kw["mounts"][0]["ReadOnly"]


def test_the_verifier_container_has_no_network_and_a_read_only_checkout(
    cfg: Config, tmp_path: Path
) -> None:  # review round 2
    from vigild.executors.runtime import PREPARE, verifier_kwargs

    src = tmp_path / "verify" / "t-after" / "src"
    kw = verifier_kwargs(cfg, "vigil-dev/x:1", src, "pytest -q")
    assert kw["network_mode"] == "none" and "network" not in kw
    assert kw["mounts"] == [
        {"Target": "/src", "Source": str(src), "Type": "bind", "ReadOnly": True}
    ]
    assert set(kw["tmpfs"]) == {"/tmp", "/work"} and kw["read_only"] is True
    assert kw["user"] == "10001:10001" and kw["cap_drop"] == ["ALL"] and "cap_add" not in kw
    # test_cmd is an argument, never spliced; the report marker comes on stdin
    assert kw["command"] == [PREPARE, "pytest -q"] and kw["stdin_open"] is True
    assert PREPARE.startswith("read -r m || exit 125; exec </dev/null;")
    assert "bash -o pipefail -o errexit" in PREPARE
    assert kw["environment"]["PYTEST_ADDOPTS"].startswith("--junitxml=/tmp/vigil-report/")
    assert kw["mem_limit"] == cfg.vigild.container.verifier_memory


def test_reports_come_only_from_the_final_trailer() -> None:  # round 4, item 3
    import base64

    from vigild.executors.runtime import split_reports

    m = "VIGIL-REPORT-abc"
    xml = b"<testsuite><testcase classname='t' name='a'/></testsuite>"
    b64 = base64.b64encode(xml).decode()
    real = f"line 1\nline 2\n\n{m} BEGIN\n{m} pytest.xml\n{b64}\n{m} zz.xml\n{b64}\n{m} END 0\n"
    out, reports = split_reports(real, m)
    assert out == "line 1\nline 2" and reports == {"pytest.xml": xml}  # zz.xml dropped
    # a block printed mid-output, before more output: plain output
    fake = f"{m} BEGIN\n{m} pytest.xml\n{b64}\n{m} END 0\nmore output\n"
    assert split_reports(fake, m)[1] == {}
    # a test that prints a fake trailer early doesn't displace the real one
    both = f"{m} BEGIN\n{m} vitest-1.xml\n{b64}\n{m} END 0\n" + real
    assert split_reports(both, m)[1] == {"pytest.xml": xml}
    # another run's marker, or none at all: no reports
    assert split_reports(real, "VIGIL-REPORT-other")[1] == {}


def test_verifier_containers_run_one_at_a_time(cfg: Config, tmp_path: Path) -> None:
    """Round 4, item 12: two ParkAgent verifiers (6 GiB each, plus tmpfs) don't
    fit the 11.7 GiB Docker VM; vigild runs one at a time unless configured."""
    import threading
    import time

    from vigild.executors.runtime import DockerTestRunner

    live, peak, lock = [0], [0], threading.Lock()

    class Sock:
        def sendall(self, data: bytes) -> None: ...

        def shutdown(self, how: int) -> None: ...

        def close(self) -> None: ...

    class Container:
        def attach_socket(self, **kw: object) -> Sock:
            return Sock()

        def start(self) -> None:
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])

        def logs(self, **kw: object) -> list[bytes]:
            time.sleep(0.2)
            return [b"ok\n"]

        def wait(self, **kw: object) -> dict[str, int]:
            with lock:
                live[0] -= 1
            return {"StatusCode": 0}

        def kill(self) -> None: ...

        def remove(self, **kw: object) -> None: ...

    class Containers:
        def create(self, **kw: object) -> Container:
            return Container()

    class Client:
        containers = Containers()

    assert cfg.vigild.container.verifier_concurrency == 1
    runner = DockerTestRunner(cfg, Client())
    src = tmp_path / "verify" / "x" / "src"
    src.mkdir(parents=True)
    threads = [
        threading.Thread(
            target=runner.run,
            args=("img", src, "true"),
            kwargs={"timeout_s": 30, "max_output": 1000},
        )
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_the_output_cap_reaches_the_container_env(cfg: Config, spec: SessionSpec) -> None:
    assert "VIGIL_MAX_OUTPUT_TOKENS" not in worker_kwargs(cfg, spec, network="n")["environment"]
    spec.max_output_tokens = 16384
    env = worker_kwargs(cfg, spec, network="n")["environment"]
    assert env["VIGIL_MAX_OUTPUT_TOKENS"] == "16384"
