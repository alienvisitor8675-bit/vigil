"""ADR-0004 rule 3-5's probe container and a remote host's sidecar, against a
fake Docker daemon: what is created, with which flags, and what is cleaned up."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.executors.hostfakes import BIND, GB, FakeClient, remote, with_remote
from vigild.config import Config
from vigild.executors.base import SessionSpec
from vigild.executors.hostcheck import DockerProber, egress_targets
from vigild.executors.hosts import HostRefused
from vigild.executors.runtime import DockerRuntime
from vigild.jobs.model import Role


class Ctr:
    def __init__(self, outer: Any, kw: dict[str, Any]) -> None:
        self.outer, self.kw, self.archives = outer, kw, []
        self.started = self.removed = False

    def put_archive(self, path: str, data: bytes) -> None:
        self.archives.append((path, data))

    def start(self) -> None:
        self.started = True

    def wait(self, **kw: Any) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, **kw: Any) -> bytes:
        return json.dumps(self.outer.report).encode()

    def remove(self, **kw: Any) -> None:
        self.removed = True


class Net:
    def __init__(self) -> None:
        self.connected: list[Any] = []
        self.removed = False

    def connect(self, c: Any, **kw: Any) -> None:
        self.connected.append((c, kw))

    def disconnect(self, c: Any, **kw: Any) -> None: ...

    def remove(self) -> None:
        self.removed = True


class Daemon(FakeClient):
    def __init__(self, cfg: Config) -> None:
        img = cfg.vigild.images
        super().__init__(images={img.claude_executor: "amd64", img.egress_proxy: "amd64"})
        self.made: list[Ctr] = []
        self.nets: list[tuple[str, dict[str, Any], Net]] = []
        host = remote(cfg)
        self.report = {
            "gateway_health": True, "reached": {n: False for n in egress_targets(cfg, host)},
            "gpu_nodes": [], "memory_max": str(int(host.memory_gb * GB)), "cpu_max": "800000 100000",
        }  # fmt: skip
        outer = self

        class Containers:
            def create(self, **kw: Any) -> Ctr:
                c = Ctr(outer, kw)
                outer.made.append(c)
                return c

            def list(self, **kw: Any) -> list[Any]:
                return []

            def get(self, name: str) -> Any:
                import docker.errors

                raise docker.errors.NotFound(name)

        class Networks:
            def create(self, name: str, **kw: Any) -> Net:
                n = Net()
                outer.nets.append((name, kw, n))
                return n

        self.containers = Containers()
        self.networks = Networks()


@pytest.fixture
def rcfg(cfg: Config) -> Config:
    return with_remote(cfg)


def test_the_probe_runs_hardened_in_the_slice_on_an_internal_network(rcfg: Config) -> None:
    d = Daemon(rcfg)
    report = DockerProber(rcfg, d).probe(remote(rcfg))
    assert report.gateway_health and not any(report.reached.values())
    (_, netkw, net), (proxy, box) = d.nets[0], d.made
    assert netkw["internal"] is True and net.removed
    for c in (proxy, box):
        kw = c.kw
        assert kw["cgroup_parent"] == "vigil.slice" and kw["read_only"] is True
        assert kw["cap_drop"] == ["ALL"] and "no-new-privileges" in kw["security_opt"]
        assert "device_requests" not in kw and "devices" not in kw and "runtime" not in kw
        assert "mounts" not in kw and "volumes" not in kw  # nothing bind-mounted
        assert c.removed
    assert box.kw["user"] == "10001:10001"
    env = box.kw["environment"]
    assert env["PROBE_HEALTH"] == f"http://{BIND}:8788/health"
    assert json.loads(env["PROBE_TARGETS"]).keys() == egress_targets(rcfg, remote(rcfg)).keys()


def test_the_sidecar_gets_exactly_the_hosts_gateway_over_the_api_not_a_mount(rcfg: Config) -> None:
    d = Daemon(rcfg)
    DockerProber(rcfg, d).probe(remote(rcfg))
    proxy = d.made[0]
    ((path, data),) = proxy.archives
    assert path == "/etc/smokescreen" and proxy.started
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        files = {m.name: tf.extractfile(m).read().decode() for m in tf.getmembers()}  # type: ignore[union-attr]
    assert yaml.safe_load(files["acl.yaml"])["default"]["allowed_domains"] == [BIND]
    assert yaml.safe_load(files["config.yaml"])["allow_addresses"] == [f"{BIND}:8788"]


def test_the_probe_needs_its_images_already_on_the_host(rcfg: Config) -> None:
    d = Daemon(rcfg)
    d.arch_of.clear()
    with pytest.raises(HostRefused, match=r"build .* first"):
        DockerProber(rcfg, d).probe(remote(rcfg))
    assert d.calls == [] and not d.made


def test_a_remote_runtime_puts_its_sidecar_files_over_the_api(rcfg: Config, tmp_path: Path) -> None:
    d = Daemon(rcfg)
    rt = DockerRuntime(rcfg, d, host=remote(rcfg))
    spec = SessionSpec(
        session_id="s-1", role=Role.WORKER, job_id="j", task_id="t", repo="o/r", preset="p",
        model="m", clone=tmp_path, session_dir=tmp_path / "s", max_turns=1, max_minutes=1,
        max_budget_usd=1.0,
    )  # fmt: skip
    with pytest.raises(HostRefused, match="Docker API"):  # the worker's own files: not built yet
        rt.run(spec, "tok", lambda e: None)
    proxy = d.made[0]
    assert (
        proxy.archives and "mounts" not in proxy.kw and proxy.kw["cgroup_parent"] == "vigil.slice"
    )
    assert proxy.removed and d.nets[0][2].removed  # cleaned up
