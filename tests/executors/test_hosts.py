"""ADR-0004 part 2: host selection by image arch and use; each host's Docker
client from its context; the grader on the session's host."""

from __future__ import annotations

from typing import Any

import pytest

from tests.executors.hostfakes import FakeClient, with_remote
from vigild.config import Config
from vigild.executors.hosts import (
    HostRefused,
    Hosts,
    client_for,
    image_arch,
    normalize_arch,
    pick_host,
)


def test_normal_jobs_go_to_local(cfg: Config) -> None:
    with_remote(cfg, uses=["jobs", "bench"])  # even a remote that lists jobs: local first
    p = pick_host(cfg, use="jobs", image_arch="arm64")
    assert p.host.name == "local" and p.emulated is False


def test_a_jobs_image_no_host_can_run_is_refused(cfg: Config) -> None:
    with pytest.raises(HostRefused, match="amd64"):
        pick_host(cfg, use="jobs", image_arch="amd64")


def test_a_host_without_jobs_in_uses_takes_no_normal_job(cfg: Config) -> None:
    with_remote(cfg, uses=["bench"])
    with pytest.raises(HostRefused):
        pick_host(cfg, use="jobs", image_arch="amd64")


def test_bench_goes_to_the_amd64_bench_host(cfg: Config) -> None:
    with_remote(cfg)
    p = pick_host(cfg, use="bench", image_arch="amd64")
    assert p.host.name == "box-a" and p.emulated is False and p.label == "native"


def test_bench_without_an_x86_host_runs_emulated_on_local(cfg: Config) -> None:
    p = pick_host(cfg, use="bench", image_arch="amd64")
    assert p.host.name == "local" and p.emulated is True and p.label == "emulated"


def test_bench_without_an_x86_host_and_no_emulation_is_refused_with_a_reason(cfg: Config) -> None:
    cfg.sandbox_hosts.bench.allow_emulation = False
    with pytest.raises(HostRefused, match="allow_emulation"):
        pick_host(cfg, use="bench", image_arch="amd64")


def test_a_remote_without_bench_in_uses_is_not_a_bench_host(cfg: Config) -> None:
    with_remote(cfg, uses=["jobs"])
    assert pick_host(cfg, use="bench", image_arch="amd64").emulated is True


def test_a_native_arm64_bench_task_stays_local(cfg: Config) -> None:
    with_remote(cfg)
    p = pick_host(cfg, use="bench", image_arch="arm64")
    assert p.host.name == "local" and p.emulated is False


def test_arch_names_are_normalised() -> None:
    assert normalize_arch("x86_64") == "amd64" and normalize_arch("aarch64") == "arm64"
    assert normalize_arch("arm64") == "arm64"
    with pytest.raises(HostRefused):
        normalize_arch("riscv64")


def test_image_arch_is_read_from_the_image() -> None:
    c = FakeClient(images={"img:x": "amd64"})
    assert image_arch(c, "img:x") == "amd64"
    with pytest.raises(HostRefused, match="build img:y first"):
        image_arch(c, "img:y")
    assert c.calls == []  # never a pull or a build


def test_local_uses_the_default_docker(cfg: Config) -> None:
    made: list[Any] = []
    client_for(cfg.sandbox_hosts.local, from_env=lambda: made.append("env") or "E")
    assert made == ["env"]


def test_a_remote_client_comes_from_the_contexts_ssh_endpoint(cfg: Config) -> None:
    with_remote(cfg)
    host = cfg.sandbox_hosts.remote_hosts()[0]
    seen: dict[str, Any] = {}

    def make(**kw: Any) -> str:
        seen.update(kw)
        return "C"

    got = client_for(
        host, lookup=lambda ctx: f"ssh://u@{ctx}.example", make=make, from_env=lambda: "E"
    )
    assert got == "C"
    assert seen["base_url"] == "ssh://u@sandbox-a.example" and seen["use_ssh_client"] is True


@pytest.mark.parametrize("endpoint", ["tcp://10.1.2.4:2375", "unix:///var/run/docker.sock", ""])
def test_a_remote_context_that_is_not_ssh_is_refused(cfg: Config, endpoint: str) -> None:
    with_remote(cfg)
    with pytest.raises(HostRefused, match="ssh://"):
        client_for(cfg.sandbox_hosts.remote_hosts()[0], lookup=lambda c: endpoint)


def test_hosts_caches_one_client_per_host_and_grader_shares_the_sessions_host(cfg: Config) -> None:
    with_remote(cfg)
    made: dict[str, FakeClient] = {}

    def make(host: Any) -> FakeClient:
        return made.setdefault(host.name, FakeClient(host.name))

    hosts = Hosts(cfg, make_client=make)
    p = pick_host(cfg, use="bench", image_arch="amd64")
    runtime = hosts.runtime(p)
    grader = hosts.test_runner(p)
    assert runtime.client is grader.client is made["box-a"]
    assert hosts.client(p.host) is hosts.client(p.host)
    assert runtime.host is p.host and grader.host is p.host
    assert hosts.test_runner(p) is grader  # one runner per host: its slots are shared
    local = pick_host(cfg, use="jobs", image_arch="arm64")
    assert hosts.runtime(local).client is made["local"]
    assert set(made) == {"box-a", "local"}


def test_place_reads_the_image_arch_from_the_local_clients_manifest(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vigild.executors.hosts.FILE_TRANSFER_BUILT", True)
    with_remote(cfg)
    local = FakeClient("local", images={"vigil-bench/t1:abc": "amd64", "vigil-dev/x:1": "arm64"})
    hosts = Hosts(cfg, make_client=lambda h: local)
    assert hosts.place("bench", "vigil-bench/t1:abc").host.name == "box-a"
    assert hosts.place("jobs", "vigil-dev/x:1").host.name == "local"
    with pytest.raises(HostRefused, match="build vigil-dev/y:1 first"):
        hosts.place("jobs", "vigil-dev/y:1")


def test_a_remote_host_is_refused_for_sessions_and_grading_until_file_transfer_is_built(
    cfg: Config,
) -> None:
    with_remote(cfg)
    local = FakeClient("local", images={"vigil-bench/t1:abc": "amd64", "vigil-dev/x:1": "arm64"})
    hosts = Hosts(cfg, make_client=lambda h: local)
    with pytest.raises(HostRefused, match="not built"):
        hosts.place("bench", "vigil-bench/t1:abc")
    with pytest.raises(HostRefused, match="not built"):
        hosts.bench("vigil-bench/t1:abc")
    # local is unaffected, including the emulated fallback
    assert hosts.place("jobs", "vigil-dev/x:1").host.name == "local"
    cfg.sandbox_hosts.hosts[:] = [cfg.sandbox_hosts.local]
    p = hosts.place("bench", "vigil-bench/t1:abc")
    assert p.host.name == "local" and p.emulated is True
