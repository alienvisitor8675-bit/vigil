"""2a round 8: the owner's two rulings (a page closing is not dispatch
resuming; review passes of at most 4 tasks), the leak paths that a
reservation open forever would now turn into a blocked day, and a dropped
move that leaves nothing behind."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import World, _approve, _flaky
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages, _run_n
from tests.jobs.test_round3_runner import _building, _scope
from tests.review.test_review_stage import CHANGES, _rounds
from vigild.breakers import Kind
from vigild.jobs.model import JobState, Role


def _breaker_page(w: World, breaker_id: int) -> list[dict[str, Any]]:
    return [p for p in _pages(w) if p["body"].get("breaker") == breaker_id]


def _abandon(w: World, page: dict[str, Any], job: str) -> None:
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                      json={"job_id": job})  # fmt: skip
    assert r.status_code == 200, r.text


# --- O1: a page closing is not dispatch resuming ------------------------------------------------


@pytest.mark.parametrize("kind", list(Kind))
def test_abandoning_the_only_held_job_resolves_the_page_not_the_breaker(
    world: World, kind: Kind
) -> None:
    w = world
    job = _building(w)
    scope = _scope(kind, job)
    b = w.runner.breakers.trip(kind, scope, {"why": "a test"}, job_id=job)
    w.runner._hold_for(job, b)
    (page,) = _breaker_page(w, b.id)
    _abandon(w, page, job)
    assert _breaker_page(w, b.id) == []  # the page is resolved
    assert w.runner.breakers.get(b.id).state == "open"  # dispatch stays paused
    assert w.runner.breakers.is_open(kind, scope)


def test_a_full_day_keeps_dispatch_paused_after_its_page_closes(world: World) -> None:
    w = world
    job, later = _building(w), _building(w)  # drafted before the day fills
    w.store.x(
        "INSERT INTO ledger (task_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
        ("elsewhere", 5.0, w.budget._day(), w.budget._month(), "x"),
    )
    b = w.runner._spend_check()
    assert b is not None
    w.runner._hold_for(job, b)
    (page,) = _breaker_page(w, b.id)
    _abandon(w, page, job)
    assert w.runner.breakers.blocking(repo="o/fixture") is not None
    # a later job is held again, with one fresh page, and no churn after it
    w.runner.dispatch(later)
    w.runner.dispatch(later)
    assert w.store.get_job(later).state is JobState.BLOCKED
    assert len(_breaker_page(w, b.id)) == 1
    assert not [s for s in w.standin.specs if s.job_id == later and s.role is Role.WORKER]


def test_a_backend_breaker_still_needs_its_probe_after_its_page_closes(world: World) -> None:
    w = world
    job = _building(w)
    br = w.runner.breakers
    b = br.trip(Kind.BACKEND, "backend:claude-sonnet", {"preset": "claude-sonnet"})
    w.runner._hold_for(job, b)
    (page,) = _breaker_page(w, b.id)
    _abandon(w, page, job)
    assert br.is_open(Kind.BACKEND, "backend:claude-sonnet")
    assert not br.probe_ready(Kind.BACKEND, "backend:claude-sonnet")  # cooling down still


def test_a_repo_failures_count_survives_its_page_closing(world: World) -> None:
    w = world
    job = _building(w)
    br = w.runner.breakers
    k = w.runner.cfg.vigild.breakers.repo_failures
    b = None
    for i in range(k):
        b = br.failure(Kind.REPO_FAILURES, "repo:o/fixture", k, {"job": f"j{i}"})
    assert b is not None
    w.runner._hold_for(job, b)
    (page,) = _breaker_page(w, b.id)
    _abandon(w, page, job)
    assert br.get(b.id).state == "open" and br.get(b.id).count >= k


# --- O2: review passes of at most 4 tasks ------------------------------------------------------


def test_a_six_task_job_is_reviewed_in_two_passes_and_never_refused(world: World) -> None:
    w = world
    stages: list[tuple[float, int]] = []
    real = w.budget.reserve

    def spy(**kw: Any) -> int:
        if kw.get("purpose") == "review_stage":
            still = w.store.q(
                "SELECT COUNT(*) n FROM reservations WHERE purpose='review_stage' AND status='open'"
            )[0]["n"]
            stages.append((float(kw["amount"]), int(still)))
        return real(**kw)

    w.budget.reserve = spy  # type: ignore[method-assign]
    w.runner.cfg.budgets.daily_usd = 20.0  # six build caps; the review passes are the point
    w.runner.cfg.budgets.wip_per_repo = 6
    job = _run_n(w, 6)
    assert w.store.get_job(job).state is JobState.READY
    b = w.runner.cfg.budgets
    per = b.review_session_usd + b.audit_session_usd
    assert [a for a, _ in stages] == [pytest.approx(4 * per), pytest.approx(2 * per)]
    assert [n for _, n in stages] == [0, 0]  # the second only after the first was released
    assert not [p for p in _pages(w, job)]


def test_a_fix_round_inside_a_pass_gets_a_fresh_reserve(world: World) -> None:
    w = world
    stages: list[float] = []
    real = w.budget.reserve

    def spy(**kw: Any) -> int:
        if kw.get("purpose") == "review_stage":
            stages.append(float(kw["amount"]))
        return real(**kw)

    w.budget.reserve = spy  # type: ignore[method-assign]
    w.runner.cfg.budgets.daily_usd = 20.0
    w.runner.cfg.budgets.wip_per_repo = 6
    w.standin.review = _rounds({"w1": [CHANGES]})
    job = _run_n(w, 6)
    assert w.store.get_job(job).state is JobState.READY
    per = w.runner.cfg.budgets.review_session_usd + w.runner.cfg.budgets.audit_session_usd
    # w1-w4, then (after w1's fix round) w1 again with w5 and w6: a fresh reserve
    assert stages == [pytest.approx(4 * per), pytest.approx(3 * per)]


def test_the_coord_prompt_says_more_than_4_tasks_review_in_waves() -> None:
    from pathlib import Path

    text = (Path(__file__).parents[2] / "prompts" / "coord.md").read_text()
    assert "more than 4" in text.lower() and "waves" in text


# --- R1: the leak paths -----------------------------------------------------------------------


def _no_open(w: World) -> None:
    assert w.budget.open_reservation_ids() == []


def test_a_routing_error_on_a_retry_releases_the_waves_reservations(world: World) -> None:
    from vigild.router.rules import RoutingError

    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    (page,) = _pages(w, job)

    def refuse(*a: Any, **kw: Any) -> None:
        raise RoutingError("no route for the retry")

    w.runner._reroute = refuse  # type: ignore[method-assign]
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    _no_open(w)
    assert w.store.get_job(job).state is JobState.BLOCKED
    assert not [t for t in w.store.list_tasks(job) if t.state == "running"]
    (again,) = _pages(w, job)
    assert "abandon" in again["body"]["actions"]


def test_a_failure_right_after_the_stage_is_reserved_releases_it(world: World) -> None:
    w = world
    real = w.store.add_event

    def fails(type: str, *a: Any, **kw: Any) -> int:
        if type == "review_stage_reserved":
            raise RuntimeError("the disk is full")
        return real(type, *a, **kw)

    w.store.add_event = fails  # type: ignore[method-assign]
    _approve(w)
    _no_open(w)


def test_a_review_session_that_never_starts_releases_its_draw(world: World) -> None:
    w = world
    real = w.runner.gateway.issue_token

    def refuse(session_id: str, repo: str, preset: str, **kw: Any) -> str:
        if preset in ("claude-sonnet", "claude-opus") and w.store.get_session(session_id).role in (
            Role.REVIEWER,
            Role.AUDITOR,
        ):
            raise RuntimeError("the repo just went private")
        return real(session_id, repo, preset, **kw)

    w.runner.gateway.issue_token = refuse  # type: ignore[method-assign]
    _approve(w)
    _no_open(w)


def test_the_sweep_settles_what_nothing_live_holds(world: World) -> None:
    w = world
    dead = w.store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    w.store.update_session(dead.id, status="failed")
    leaked = w.budget.reserve(session_id=dead.id, task_id=None, amount=2.0, purpose="worker")
    live = w.store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    w.store.update_session(live.id, status="running")
    kept = w.budget.reserve(session_id=live.id, task_id=None, amount=1.0, purpose="worker")
    assert w.runner.sweep_reservations(grace_seconds=0) == [leaked]
    assert w.budget.open_reservation_ids() == [kept]
    assert [e for e in w.store.events() if e.type == "reservation_swept"]


def test_a_leak_no_longer_blocks_every_later_day(world: World) -> None:
    """The reviewer's `test_q_leak_is_sticky`, with the sweep."""
    from datetime import timedelta

    w = world
    w.budget.reserve(session_id=None, task_id="t", amount=2.0, purpose="worker")  # leaked
    real = w.budget.clock
    later = real() + timedelta(days=40)
    w.budget.clock = lambda: later  # type: ignore[method-assign]
    w.runner.sweep_reservations(grace_seconds=0)
    w.budget.reserve(session_id=None, task_id=None, amount=3.5, purpose="coord")


# --- R2: a dropped move leaves nothing behind ----------------------------------------------------


def test_a_deps_job_abandoned_during_its_review_keeps_no_queued_task(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    from tests.jobs.test_coord_loop import CLEAN_REVIEW
    from tests.jobs.test_deps import _approve_child, _deps_child

    w = world
    _parent, child, _pm = _deps_child(w, fixture_repo)
    w.standin.review = lambda spec: CHANGES if spec.job_id == child else CLEAN_REVIEW
    real = w.runner._paused
    calls: dict[str, int] = {}

    def abandon_after_review(job: Any, task: str | None = None, *, building: bool = False) -> Any:
        out = real(job, task, building=building)
        if task is None and job.id == child:
            calls[child] = calls.get(child, 0) + 1
            if calls[child] == 2:  # after the review's sessions: the owner gives it up
                w.runner._block(child, "the owner stops it")
                w.runner._page(child, "stop", {"actions": ["abandon"]})
                (pg,) = [p for p in _pages(w, child) if p["title"] == "stop"]
                w.client.post(f"/api/inbox/{pg['id']}/abandon", headers=auth(w.tokens.admin))
        return out

    w.runner._paused = abandon_after_review  # type: ignore[method-assign]
    _approve_child(w, child)
    assert w.store.get_job(child).state is JobState.ABANDONED
    left = {t.state for t in w.store.list_tasks(child)}
    assert not left & {"fix", "queued", "running"}, left
    # 2a round 9: the deps guard on its own: no PR of the dead job is closed
    assert not [e for e in w.store.events() if e.type == "deps_review_closed"]


# --- R4(a): a call that outlives the drain wait still lands in the ledger -----------------------


def test_a_call_still_in_flight_after_the_wait_settles_when_it_is_booked(world: World) -> None:
    from vigild.executors.base import SessionSpec
    from vigild.gateway.service import Hold

    w = world
    gw = w.runner.gateway
    late: dict[str, Any] = {}

    def a_slow_call(spec: SessionSpec) -> None:
        if spec.role is Role.WORKER and not late:
            late["sid"] = spec.session_id
            late["hold"] = Hold(spec.session_id, gw._hold(spec.session_id, 0.01),
                                gw.cfg.preset("claude-sonnet"), {})  # fmt: skip

    w.standin.on_session = a_slow_call
    gw.drain = lambda sid, timeout=60.0: sid not in gw._inflight  # type: ignore[method-assign]
    _approve(w)
    sid = late["sid"]
    open_rows = w.store.q(
        "SELECT id FROM reservations WHERE session_id=? AND status='open'", (sid,)
    )
    assert open_rows  # not settled while its call is in flight: still counted
    gw.release(late["hold"])  # the call ends and is booked
    assert not w.store.q("SELECT id FROM reservations WHERE session_id=? AND status='open'", (sid,))


def test_vigild_sweeps_reservations_every_ten_minutes(tmp_path: Any) -> None:
    import threading
    import time

    from vigild import server

    swept: list[int] = []

    class Runner:
        pr_state = None

        def sweep_reservations(self) -> list[int]:
            swept.append(1)
            return []

    v = server.build(data_dir=tmp_path / "d")
    stop = server.start_pollers(v, None, Runner())  # type: ignore[arg-type]
    try:
        for _ in range(50):
            if swept:
                break
            time.sleep(0.02)
        assert swept  # run at once, then every RESERVATION_SWEEP_SECONDS
        assert server.RESERVATION_SWEEP_SECONDS == 600
        assert any(t.name == "vigil-sweep" for t in threading.enumerate())
    finally:
        stop.set()
