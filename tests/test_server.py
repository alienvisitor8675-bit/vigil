"""vigild's startup order (review round 2, N11): builds that reconcile queued
start only after every listener, the gateway's included, is up."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from vigild import server


class FakeServer:
    instances: ClassVar[list[FakeServer]] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.started = False
        self.should_exit = False
        FakeServer.instances.append(self)

    async def serve(self) -> None:
        await asyncio.sleep(0.1 * len(FakeServer.instances))
        self.started = True
        await asyncio.sleep(0.3)


def test_on_ready_runs_after_every_listener_started(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    FakeServer.instances = []
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "create_app", lambda *a, **k: object())
    monkeypatch.setattr(server, "create_gateway_app", lambda *a, **k: object())
    seen: list[list[bool]] = []

    class V:
        pass

    v: Any = V()
    v.cfg = cfg
    v.services = object()
    v.gateway = object()
    asyncio.run(
        server.serve(v, on_ready=lambda: seen.append([s.started for s in FakeServer.instances]))
    )
    assert seen == [[True] * len(FakeServer.instances)]
    assert len(FakeServer.instances) >= 2  # the API and the gateway


def test_vigil_data_dir_points_vigild_at_a_scratch_dir(tmp_path: Any, monkeypatch: Any) -> None:
    from vigild.server import build

    monkeypatch.setenv("VIGIL_DATA_DIR", str(tmp_path / "scratch"))
    v = build(env_file=tmp_path / "no-env")
    assert v.cfg.vigild.data_path == tmp_path / "scratch"
    assert (tmp_path / "scratch" / "vigil.db").exists()
    v.store.conn.close()


def test_vigild_refuses_to_start_on_a_failing_remote_host() -> None:
    import pytest

    from tests.executors.hostfakes import FakeHostClient, FakeSsh, with_remote
    from vigild import config
    from vigild.executors.hosts import Hosts
    from vigild.server import check_sandbox_hosts

    cfg = with_remote(config.load())
    hosts = Hosts(cfg, make_client=lambda h: FakeHostClient(version="0.0.1"))
    with pytest.raises(SystemExit, match="rule 2"):
        check_sandbox_hosts(
            cfg, hosts, ssh=FakeSsh(), resolve=lambda n: ["10.1.2.4"],
            prober_for=lambda h, c: None, lookup=lambda c: "ssh://u@h",
        )  # fmt: skip
    # only `local` configured: nothing is reached and it starts
    local = config.load()
    check_sandbox_hosts(local, Hosts(local, make_client=lambda h: 1 / 0))
