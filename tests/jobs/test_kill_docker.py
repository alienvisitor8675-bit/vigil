"""`vigil kill` with real containers (pytest -m docker; no model call, no gateway
listener, so no port): a job whose workers are running in real session containers, each
behind its own egress sidecar on its own network, is killed. Afterwards no container or
network carries the job's labels and no reservation of the job is open; a container that
carries someone else's label is untouched.

The workers run `sleep` in the executor image (the harness would call a model); the
network, the sidecar, the labels and the runner are the real ones."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tests.jobs.test_coord_loop import World, _request, auth, world  # noqa: F401
from tests.jobs.test_kill import _kill, _open_reservations, _spec_item
from vigild.executors import runtime as rt
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.executors.runtime import LABEL, DockerRuntime
from vigild.jobs.model import JobState, Role

pytestmark = pytest.mark.docker


class Sleepers:
    """Coord and the rest by the stand-in; a worker in a real session container that
    sleeps. The kill hooks are the real DockerRuntime's."""

    def __init__(self, inner: Any, runtime: DockerRuntime) -> None:
        self.inner, self.runtime = inner, runtime
        self.stop_job = runtime.stop_job
        self.purge_job = runtime.purge_job

    def run(self, spec: SessionSpec, token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is not Role.WORKER:
            return self.inner.run(spec, token, on_event)  # type: ignore[no-any-return]
        spec.image = self.runtime.cfg.vigild.images.claude_executor
        return self.runtime.run(spec, token, on_event)


def _labelled(client: Any, sid: str) -> tuple[list[Any], list[Any]]:
    flt = {"label": f"{LABEL}={sid}"}
    return (
        client.containers.list(all=True, filters=flt),
        client.networks.list(filters=flt),
    )


def test_a_running_job_leaves_no_container_network_or_reservation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    real = rt.worker_kwargs

    def sleeping(cfg: Any, spec: SessionSpec, *, network: str) -> dict[str, Any]:
        return {**real(cfg, spec, network=network), "entrypoint": ["sleep", "600"], "command": []}

    monkeypatch.setattr(rt, "worker_kwargs", sleeping)
    runtime = DockerRuntime(w.runner.cfg)
    client = runtime.client
    bystander = client.containers.run(
        w.runner.cfg.vigild.images.claude_executor,
        entrypoint=["sleep", "600"],
        detach=True,
        labels={LABEL: "s-someone-elses-job"},
    )
    try:
        w.runner.executor = Sleepers(w.standin, runtime)
        job = _request(w)
        item = _spec_item(w)
        t = threading.Thread(
            target=lambda: w.client.post(
                f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={}
            ),
            daemon=True,
        )
        t.start()
        workers: list[Any] = []
        for _ in range(300):  # both workers up, each with its sidecar and network
            workers = client.containers.list(filters={"label": "dev.vigil.role=worker"})
            if len(workers) >= 2:
                break
            time.sleep(0.1)
        assert len(workers) >= 2, "the workers should be running"
        sids = sorted(s.id for s in w.store.list_sessions(job) if s.role is Role.WORKER)
        assert len(sids) == 2
        for sid in sids:
            boxes, nets = _labelled(client, sid)
            assert len(boxes) == 2 and len(nets) == 1  # the worker and its egress sidecar
        assert _open_reservations(w)

        r = _kill(w, job, channel="cli")
        t.join(60)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "abandoned" and body["killed"] is True
        assert not t.is_alive()

        assert w.store.get_job(job).state is JobState.ABANDONED
        for sid in sids:
            assert _labelled(client, sid) == ([], [])  # nothing of the job is left
        assert _open_reservations(w) == [] and body["reservations_open"] == []
        # by label only: the bystander is still running
        bystander.reload()
        assert bystander.status == "running"
    finally:
        bystander.remove(force=True)
        # nothing of this test may outlive it; by this test's own sessions, never reap()
        # (that removes every vigil container, production's included)
        mine = [s.id for s in w.store.list_sessions()]
        runtime.stop_job(job, mine)
        runtime.purge_job(job, mine)
