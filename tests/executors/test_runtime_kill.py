"""DockerRuntime.stop_job / purge_job pick a job's containers and networks by vigil's
labels only (issue #52): its sessions' and their egress sidecars' (by session label), its
verifier's, hidden-test probes' and deps containers' (by the job label); never another
job's, never an unlabelled one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vigild.config import Config
from vigild.executors.base import SessionSpec
from vigild.executors.runtime import (
    JOB_LABEL,
    LABEL,
    DockerRuntime,
    deps_kwargs,
    job_scope,
    proxy_kwargs,
    verifier_kwargs,
)
from vigild.jobs.model import Role


class Box:
    def __init__(self, labels: dict[str, str], log: list[str], name: str) -> None:
        self.labels = labels
        self.attrs: dict[str, Any] = {"Labels": labels, "Containers": {"collector-id": {}}}
        self.name = name
        self.log = log

    def kill(self) -> None:
        self.log.append(f"kill {self.name}")

    def remove(self, force: bool = False) -> None:
        self.log.append(f"rm {self.name}")

    def reload(self) -> None:
        pass

    def disconnect(self, cid: str, force: bool = False) -> None:
        self.log.append(f"disconnect {self.name} {cid}")


class Listing:
    def __init__(self, boxes: list[Box]) -> None:
        self.boxes = boxes

    def list(self, **kw: Any) -> list[Box]:
        key = kw["filters"]["label"]
        assert key == LABEL, "list by the label's key, then match its value ourselves"
        return [b for b in self.boxes if LABEL in b.labels]


class Client:
    def __init__(self, containers: list[Box], networks: list[Box]) -> None:
        self.containers = Listing(containers)
        self.networks = Listing(networks)


def _world(cfg: Config) -> tuple[DockerRuntime, list[str]]:
    log: list[str] = []

    def box(name: str, value: str | None, job: str | None = None) -> Box:
        labels = {LABEL: value} if value else {"other": "x"}
        if job:
            labels[JOB_LABEL] = job
        return Box(labels, log, name)

    containers = [
        box("worker", "s-mine"), box("sidecar", "s-mine"),
        box("verifier", "verify-pa-1-w1-base-ab12", "job-mine"),
        box("probe", "verify-probe-ab12cd34-xy", "job-mine"),  # a hidden-test author's probe
        box("deps", "deps-0123456789ab", "job-mine"),
        box("deps-sidecar", "deps-0123456789ab", "job-mine"),
        box("other-worker", "s-theirs"),
        box("other-verifier", "verify-pa-2-w1-base-cd34", "job-theirs"),
        box("other-probe", "verify-probe-ab12cd34-zz", "job-theirs"),  # same name, other job
        box("other-deps", "deps-ba9876543210", "job-theirs"),
        box("no-job-label", "verify-pa-1-w1-after-ef56"),  # named for the job's task: not ours
        box("unlabelled", None),
    ]  # fmt: skip
    networks = [
        box("net-mine", "s-mine"), box("net-deps", "deps-0123456789ab", "job-mine"),
        box("net-theirs", "s-theirs"), box("net-deps-theirs", "deps-ba9876543210", "job-theirs"),
        box("net-none", None),
    ]  # fmt: skip
    return DockerRuntime(cfg, Client(containers, networks)), log


def test_stop_job_kills_only_the_jobs_containers(cfg: Config) -> None:
    rt, log = _world(cfg)
    n = rt.stop_job("job-mine", ["s-mine"])
    assert sorted(log) == [
        "kill deps", "kill deps-sidecar", "kill probe", "kill sidecar", "kill verifier",
        "kill worker",
    ]  # fmt: skip
    assert n == 6


def test_purge_job_removes_only_the_jobs_containers_and_networks(cfg: Config) -> None:
    rt, log = _world(cfg)
    out = rt.purge_job("job-mine", ["s-mine"])
    assert out == {"containers": 6, "networks": 2}
    assert sorted(x for x in log if x.startswith("rm ")) == [
        "rm deps", "rm deps-sidecar", "rm net-deps", "rm net-mine", "rm probe", "rm sidecar",
        "rm verifier", "rm worker",
    ]  # fmt: skip
    # the shared collector is let go of before the network goes
    assert "disconnect net-mine collector-id" in log


def test_a_job_with_nothing_matches_nothing(cfg: Config) -> None:
    rt, log = _world(cfg)
    assert rt.stop_job("job-none", []) == 0
    assert rt.purge_job("job-none", []) == {"containers": 0, "networks": 0}
    assert log == []


def test_a_deps_container_is_stopped_by_its_jobs_kill_and_no_other_jobs(cfg: Config) -> None:
    rt, log = _world(cfg)
    rt.stop_job("job-theirs", [])
    assert "kill deps" not in log and "kill other-deps" in log
    assert "kill probe" not in log and "kill other-probe" in log


def test_the_containers_a_job_starts_carry_its_label(cfg: Config, tmp_path: Path) -> None:
    """The label the kill selects by is put on every container vigild starts for a job
    outside a session: verifier checkouts, hidden-test probes (both are `verifier_kwargs`)
    and deps builds, and on a session's sidecar; with no job in scope there is none."""
    plain = verifier_kwargs(cfg, "img", tmp_path, "true", None, None, None)
    assert JOB_LABEL not in plain["labels"]
    with job_scope("job-7"):
        v = verifier_kwargs(cfg, "img", tmp_path, "true", None, None, None)
        d = deps_kwargs(cfg, "img", tmp_path, network="n")
    assert v["labels"][JOB_LABEL] == "job-7" and d["labels"][JOB_LABEL] == "job-7"
    assert (
        JOB_LABEL not in verifier_kwargs(cfg, "img", tmp_path, "true", None, None, None)["labels"]
    )
    spec = SessionSpec(
        session_id="s-1", role=Role.WORKER, job_id="job-7", task_id=None, repo="o/r",
        preset="p", model="m", clone=tmp_path, session_dir=tmp_path, max_turns=1,
        max_minutes=1, max_budget_usd=0.1,
    )  # fmt: skip
    assert proxy_kwargs(cfg, spec)["labels"][JOB_LABEL] == "job-7"
