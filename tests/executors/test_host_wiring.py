"""ADR-0004 round 2: host selection is wired in. The runner's session runtime
and the verifier's test runner both come from the chosen host
(Hosts.place / runtime / test_runner). A normal job resolves to `local`, the
grader runs on the session's host, and the bench entry point is ready (no
caller until bench 1b)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.executors.hostfakes import FakeClient, with_remote
from vigild.config import Config
from vigild.executors.base import SessionSpec
from vigild.executors.hosts import (
    HostExecutor,
    HostRefused,
    Hosts,
    HostTestRunner,
)
from vigild.jobs.model import Role

IMAGES = {
    "vigil-session/x:1": "arm64",
    "vigil-dev/x:1": "arm64",
    "vigil-bench/t1:abc": "amd64",
}


class Rec:
    """A runtime or grader that records which host it was built for."""

    def __init__(self, kind: str, host: str, log: list[tuple[str, str, str]]) -> None:
        self.kind, self.host, self.log = kind, host, log

    def run(self, *a: Any, **kw: Any) -> Any:
        self.log.append((self.kind, self.host, str(a[0].image if self.kind == "session" else a[0])))
        return "ok"

    def run_deps(self, image: str, work: Path, command: str, timeout_s: int = 900) -> Any:
        self.log.append(("deps", self.host, image))
        return 0, ""

    def stop(self, session_id: str) -> None:
        self.log.append(("stop", self.host, session_id))

    def reap(self) -> int:
        self.log.append(("reap", self.host, ""))
        return 1


def fake_hosts(cfg: Config) -> tuple[Hosts, list[tuple[str, str, str]]]:
    log: list[tuple[str, str, str]] = []
    local = FakeClient("local", images=IMAGES)
    hosts = Hosts(cfg, make_client=lambda h: local)
    hosts.runtime = lambda p: Rec("session", p.host.name, log)  # type: ignore[method-assign]
    hosts.test_runner = lambda p: Rec("grader", p.host.name, log)  # type: ignore[method-assign]
    return hosts, log


def spec(image: str) -> SessionSpec:
    return SessionSpec(
        session_id="s1", role=Role.WORKER, job_id="j", task_id="t", repo="o/r", preset="p",
        model="m", clone=Path("/x"), session_dir=Path("/y"), max_turns=1, max_minutes=1,
        max_budget_usd=0.0, image=image,
    )  # fmt: skip


def grade(g: HostTestRunner, image: str) -> Any:
    return g.run(image, Path("/src"), "true", timeout_s=1, max_output=1)


def test_a_normal_jobs_session_and_its_grader_both_run_on_local(cfg: Config) -> None:
    hosts, log = fake_hosts(cfg)
    HostExecutor(hosts).run(spec("vigil-session/x:1"), "tok", lambda e: None)
    grade(HostTestRunner(hosts), "vigil-dev/x:1")
    assert log == [("session", "local", "vigil-session/x:1"), ("grader", "local", "vigil-dev/x:1")]


def test_a_normal_job_stays_local_even_when_a_remote_host_is_configured(cfg: Config) -> None:
    with_remote(cfg, uses=["jobs", "bench"])
    hosts, log = fake_hosts(cfg)
    HostExecutor(hosts).run(spec("vigil-session/x:1"), "tok", lambda e: None)
    grade(HostTestRunner(hosts), "vigil-dev/x:1")
    assert {h for _, h, _ in log} == {"local"}


def test_the_grader_is_on_the_same_host_as_the_session_with_two_hosts(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "vigild.executors.hosts.FILE_TRANSFER_BUILT", True
    )  # the property, once built
    with_remote(cfg)  # local + box-a (amd64, bench)
    hosts, log = fake_hosts(cfg)
    HostExecutor(hosts, use="bench").run(spec("vigil-bench/t1:abc"), "tok", lambda e: None)
    grade(HostTestRunner(hosts, use="bench"), "vigil-bench/t1:abc")
    assert [(k, h) for k, h, _ in log] == [("session", "box-a"), ("grader", "box-a")]
    # a normal job in the same process goes to the other host
    HostExecutor(hosts).run(spec("vigil-session/x:1"), "tok", lambda e: None)
    grade(HostTestRunner(hosts), "vigil-dev/x:1")
    assert [(k, h) for k, h, _ in log[2:]] == [("session", "local"), ("grader", "local")]


def test_the_bench_entry_point_labels_an_emulated_placement(cfg: Config) -> None:
    assert cfg.sandbox_hosts.bench.allow_emulation is True
    hosts, log = fake_hosts(cfg)  # only `local`: an amd64 image runs under emulation
    p, runtime, grader = hosts.bench("vigil-bench/t1:abc")
    assert p.host.name == "local" and p.emulated is True and p.label == "emulated"
    assert runtime.host == grader.host == "local"
    HostExecutor(hosts, use="bench").run(spec("vigil-bench/t1:abc"), "tok", lambda e: None)
    assert log == [("session", "local", "vigil-bench/t1:abc")]


def test_a_native_bench_placement_is_labelled_native(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vigild.executors.hosts.FILE_TRANSFER_BUILT", True)
    with_remote(cfg)
    hosts, _ = fake_hosts(cfg)
    p, _r, _g = hosts.bench("vigil-bench/t1:abc")
    assert p.host.name == "box-a" and p.label == "native"


def test_bench_without_a_host_or_emulation_is_refused(cfg: Config) -> None:
    cfg.sandbox_hosts.bench.allow_emulation = False
    hosts, log = fake_hosts(cfg)
    with pytest.raises(HostRefused, match="allow_emulation"):
        HostExecutor(hosts, use="bench").run(spec("vigil-bench/t1:abc"), "t", lambda e: None)
    assert log == []


def test_a_missing_image_is_refused_naming_the_build(cfg: Config) -> None:
    hosts, log = fake_hosts(cfg)
    with pytest.raises(HostRefused, match="build vigil-session/gone:1 first"):
        HostExecutor(hosts).run(spec("vigil-session/gone:1"), "t", lambda e: None)
    with pytest.raises(HostRefused, match="build vigil-dev/gone:1 first"):
        grade(HostTestRunner(hosts), "vigil-dev/gone:1")
    assert log == []


def test_deps_stop_and_reap_follow_the_same_selection(cfg: Config) -> None:
    hosts, log = fake_hosts(cfg)
    ex = HostExecutor(hosts)
    assert ex.run_deps("vigil-dev/x:1", Path("/w"), "uv lock") == (0, "")
    ex.stop("s-unknown")  # not running: harmless
    assert ex.reap() == 1
    assert ("deps", "local", "vigil-dev/x:1") in log and ("reap", "local", "") in log


def test_the_server_builds_its_executor_and_grader_from_the_hosts(cfg: Config) -> None:
    from vigild.server import build_executors

    client = FakeClient("local", images=IMAGES)
    from vigild.db import Store

    hosts, runtime, tests = build_executors(cfg, client, Store.open(":memory:"))
    assert isinstance(runtime, HostExecutor) and isinstance(tests, HostTestRunner)
    assert hosts.client(cfg.sandbox_hosts.local) is client  # one local client, shared
