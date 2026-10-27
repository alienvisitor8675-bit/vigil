"""The budget queue (main's #15), as 2a rebased it: first in, first out, and a
waiter re-checks its job on every poll (abandoned or restarted while waiting)."""

from __future__ import annotations

import threading
import time

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import (
    World,
    _events,
    _no_failure_pages,
    _request,
)
from tests.jobs.test_coord_loop import world as world
from vigild.jobs.fsm import Actor, transition
from vigild.jobs.model import JobState
from vigild.jobs.runner import BUILD_WAIT


def _fill_the_day(w: World) -> list[int]:
    return [
        w.budget.reserve(session_id=None, task_id=f"other-{i}", amount=a, purpose="worker")
        for i, a in enumerate((2.0, 2.0, 0.98))
    ]


def _abandon(w: World, job: str) -> None:
    transition(
        w.store, job, JobState.ABANDONED, Actor.OWNER,
        channel="inbox", evidence={"action": "abandon"},
    )  # fmt: skip


def _queue_is_empty(w: World) -> bool:
    return not w.runner._queue


def test_a_coord_waiting_for_the_day_stops_when_its_job_is_abandoned(world: World) -> None:
    w = world
    held = _fill_the_day(w)
    polls: list[float] = []

    def sleep(s: float) -> None:
        polls.append(s)
        _abandon(w, w.store.list_jobs()[0].id)  # the owner gave up

    w.runner.sleep = sleep  # type: ignore[method-assign]
    job = _request(w)
    assert len(polls) == 1  # it looked once, found the job gone, and stopped
    assert w.standin.calls == []  # no coord ran
    assert w.store.get_job(job).state is JobState.ABANDONED
    (q,) = _events(w, "queued")
    (d,) = _events(w, "dequeued")
    assert q["for"] == "coord" and d.get("cancelled") is True
    assert _no_failure_pages(w)
    assert _queue_is_empty(w)
    for rid in held:
        w.budget.settle(rid, 0.0)
    assert w.budget.open_today() == 0  # nothing leaked


def test_a_worker_waiting_for_the_day_stops_when_its_job_is_abandoned(world: World) -> None:
    w = world
    job = _request(w)
    _fill_the_day(w)
    gone: list[bool] = []

    def sleep(s: float) -> None:
        if not gone:  # the first poll: the job is blocked, and the owner abandons it
            gone.append(True)
            w.runner._block(job, "test")
            _abandon(w, job)

    w.runner.sleep = sleep  # type: ignore[method-assign]
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert not [c for c in w.standin.calls if c[0] == "worker"]
    assert w.store.get_job(job).state is JobState.ABANDONED
    assert len(_events(w, "queued")) == 2
    assert all(e.get("cancelled") for e in _events(w, "dequeued"))
    assert w.publisher.prs == []
    assert _queue_is_empty(w)
    assert "failed" not in {t.state for t in w.store.list_tasks(job)}


def test_a_restart_while_a_worker_waits_holds_the_job_and_the_stale_waiter_stops(
    world: World,
) -> None:
    """vigild restarts while a worker waits: the reconcile blocks the mid-flight job
    with a page, and the old waiter (in a real restart it is gone with the process)
    runs no worker when it next looks."""
    w = world
    job = _request(w)
    held = _fill_the_day(w)
    restarted: list[bool] = []

    def sleep(s: float) -> None:
        if not restarted:
            restarted.append(True)
            w.runner.reconcile()  # the new process starts

    w.runner.sleep = sleep  # type: ignore[method-assign]
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert not [c for c in w.standin.calls if c[0] == "worker"]
    assert w.store.get_job(job).state is JobState.BLOCKED
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (page,) = [p for p in pages if p["job_id"] == job]
    assert "restarted" in page["title"] and "retry" in page["body"]["actions"]
    assert _queue_is_empty(w)
    # the day frees up and the owner retries: the work runs, once
    for rid in held:
        w.budget.settle(rid, 0.0)
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is JobState.READY
    assert len(w.publisher.prs) == 2


def test_the_queue_is_first_in_first_out(world: World) -> None:
    """Two workers wait for a day too full for either: when room for one frees up, the
    older gets it, though both are polling; a request that arrives meanwhile waits
    behind them rather than taking it."""
    w = world
    job = _request(w)
    held = _fill_the_day(w)  # $4.98 of $5 held
    order: list[str] = []
    real = w.budget.reserve

    def reserve(**kw: object) -> int:
        rid = real(**kw)  # type: ignore[arg-type]
        if kw.get("purpose") == "worker" and kw.get("task_id", "").startswith(job):  # type: ignore[union-attr]
            order.append(str(kw["task_id"]))
        return rid

    w.budget.reserve = reserve  # type: ignore[method-assign]
    state = {"ticks": 0, "freed_one": False, "freed_rest": False}
    lock = threading.Lock()

    def sleep(s: float) -> None:
        with lock:
            state["ticks"] += 1
            if state["ticks"] > 4000:
                pytest.fail("the queue never drained")
            if not state["freed_one"]:
                state["freed_one"] = True
                w.budget.settle(held[0], 0.0)  # room for exactly one $2 worker
                # a newcomer would fit that room too, and must not jump the line
                assert not w.runner._queue_turn(("late", "late-w1", "worker"))
            elif order and not state["freed_rest"]:
                state["freed_rest"] = True  # the older has been served; now the rest
                for rid in held[1:]:
                    w.budget.settle(rid, 0.0)
        time.sleep(0.001)

    w.runner.sleep = sleep  # type: ignore[method-assign]
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert order == [f"{job}-w1", f"{job}-w2"]  # arrival order
    assert w.store.get_job(job).state is JobState.READY
    assert _queue_is_empty(w)
    assert frozenset({JobState.BUILDING}) == BUILD_WAIT
