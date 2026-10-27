"""2a round 2 in the runner (review B1-B7, B10; A11; owner ruling 3; coord
decisions 1-5): budget refusals page the job, every block has a page with
actions, reviews survive a restart, a resume is queued, WIP never pauses a fix
round and is checked atomically, breakers move only where the call is made,
and a deps PR merged however it happened resumes the parent."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import (
    CLEAN_REVIEW,
    REPO,
    World,
    _approve,
    _request,
)
from tests.jobs.test_coord_loop import world as world
from tests.review.test_review_stage import CHANGES
from tests.scheduler.test_fleet import _doc_work, plan_n, spec_n
from vigild.breakers import GLOBAL, Breakers, Kind
from vigild.executors.base import SessionSpec
from vigild.jobs.model import JobState, Role


def _inbox(w: World) -> dict[str, Any]:
    return w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()  # type: ignore[no-any-return]


def _pages(w: World, job: str | None = None) -> list[dict[str, Any]]:
    return [q for q in _inbox(w)["questions"] if job is None or q["job_id"] == job]


def _breaker_pages(w: World) -> list[dict[str, Any]]:
    return [q for q in _pages(w) if q["body"].get("kind") == "breaker"]


def _uncapped_plan(job: str, n: int) -> str:
    """n workers with no max_cost_usd: each would take the $2 per-task cap."""
    text = plan_n(job, n)
    return text.replace('"max_cost_usd": 1.0,', "").replace(',\n  "max_cost_usd": 1.0', "")


def _run_n(w: World, n: int, before_approve: Any = None) -> str:
    w.standin.coord_drafts = [lambda j: (spec_n(n), _uncapped_plan(j, n))]
    w.standin.worker_edit = _doc_work()
    job = _request(w)
    (item,) = _inbox(w)["specs"]
    if before_approve is not None:
        before_approve()
    r = w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert r.status_code == 200, r.text
    return job


def assert_actionable(w: World, job: str) -> dict[str, Any]:
    """The invariant (coord decision 2): a blocked job has an open page that
    names it and offers at least `abandon`, or it is held by a breaker whose
    page lists it and offers `reset`."""
    j = w.store.get_job(job)
    assert j.state is JobState.BLOCKED, j.state
    mine = [p for p in _pages(w, job) if "abandon" in (p["body"].get("actions") or [])]
    if mine:
        return mine[-1]
    held = [
        p
        for p in _breaker_pages(w)
        if job in Breakers(w.store, w.runner.cfg).get(int(p["body"]["breaker"])).held
        and "reset" in p["body"]["actions"]
    ]
    assert held, f"{job} is blocked with no page to act on: {_pages(w)}"
    return held[-1]


# --- B1 / coord 1: budget refusals --------------------------------------------------------------


# Rebased onto main's budget queue (#15): a wave the day has no room for no longer
# splits the day into shares (2a review B1) or pages the job; its workers wait their turn
# (tests/jobs/test_coord_loop.py: test_a_third_worker_waits_for_the_day_then_runs,
# test_a_full_day_queues_the_workers_and_they_run_when_it_frees). A day whose metered
# spend has reached the cap is never waited for: the daily breaker below.


def test_the_daily_breaker_trips_only_when_metered_spend_reaches_the_cap(world: World) -> None:
    w = world

    def spend_the_day() -> None:
        w.store.x(
            "INSERT INTO ledger (task_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
            ("elsewhere", 5.0, w.budget._day(), w.budget._month(), "x"),
        )

    job = _run_n(w, 3, spend_the_day)
    (page,) = _breaker_pages(w)
    assert page["body"]["breaker_kind"] == "daily_cap" and page["body"]["scope"] == GLOBAL
    assert job in Breakers(w.store, w.runner.cfg).get(page["body"]["breaker"]).held
    assert_actionable(w, job)


def test_a_review_refused_by_the_budget_pages_the_job_not_the_fleet(world: World) -> None:
    w = world
    real = w.budget.reserve

    def refuse_reviews(**kw: Any) -> int:
        if kw.get("purpose") == "review_stage":  # the job's one review reservation (round 5)
            from vigild.budget import BudgetExceeded

            raise BudgetExceeded("monthly", "a test says the month is held")  # a day would queue
        return real(**kw)

    w.budget.reserve = refuse_reviews  # type: ignore[method-assign]
    job = _approve(w)
    page = assert_actionable(w, job)
    assert page["body"]["kind"] == "budget" and "retry" in page["body"]["actions"]
    assert _breaker_pages(w) == []


def test_the_coord_prompt_says_how_to_size_caps() -> None:
    from pathlib import Path

    text = (Path(__file__).parents[2] / "prompts" / "coord.md").read_text()
    assert "max_cost_usd" in text and "$5" in text  # a day with no room is waited for (#15)


# --- B2-B4 / coord 2: every block has a page with actions ---------------------------------------


def test_a_task_left_in_fix_is_dispatched_when_the_review_is_retried(world: World) -> None:
    """B2: w1 goes to a fix round while w2's review is invalid; the retry used
    to review only w2 and strand w1 (IN_REVIEW -> READY refused)."""
    w = world
    seen: dict[str, int] = {}

    def review(spec: SessionSpec) -> Any:
        tid = str(spec.task_id)[-2:]
        seen[tid] = seen.get(tid, 0) + 1
        if tid == "w1" and seen[tid] == 1:
            return CHANGES
        if tid == "w2" and seen[tid] == 1:
            return {}  # invalid: the stage gate fails
        return CLEAN_REVIEW

    w.standin.review = review
    job = _approve(w)
    page = assert_actionable(w, job)
    assert page["body"]["kind"] == "review_failed"
    assert w.store.get_task(f"{job}-w1").state == "fix"
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state.value == "ready"
    w1 = [s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id).endswith("w1")]
    assert len(w1) == 2  # the fix round ran


def test_a_vigild_error_page_offers_retry_and_abandon(world: World) -> None:
    w = world
    real = w.runner.review
    calls = {"n": 0}

    def flaky(job_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a bug")
        real(job_id)

    w.runner.review = flaky  # type: ignore[method-assign]
    job = _approve(w)
    page = assert_actionable(w, job)
    assert page["title"].startswith("vigild error") and page["body"]["actions"] == [
        "retry", "abandon"]  # fmt: skip
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state.value == "ready"


def test_a_task_that_cant_run_offers_retry_replan_and_abandon(world: World) -> None:
    w = world
    job = _request(w)
    (item,) = _inbox(w)["specs"]
    w.runner.cfg.repos.repos[REPO].dev_image_tools = ["git"]  # no pytest now: it can't run
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    page = assert_actionable(w, job)
    assert page["body"]["actions"] == ["retry", "replan", "abandon"]


def test_reconcile_requeues_a_review_a_restart_cut_off(world: World) -> None:
    """B3: a job left in_review used to sit there with no page."""
    w = world
    real = w.runner.review
    w.runner.review = lambda j: None  # type: ignore[method-assign]  # vigild dies mid-review
    job = _approve(w)
    assert w.store.get_job(job).state.value == "in_review"
    s = w.store.create_session(job, Role.REVIEWER, task_id=f"{job}-w1")
    w.store.update_session(s.id, status="running")
    w.runner.review = real  # type: ignore[method-assign]
    out = w.runner.reconcile()
    assert out["requeued"] >= 1
    assert w.store.get_session(s.id).status == "failed"
    assert w.runner.start_deferred() == 1
    assert w.store.get_job(job).state.value == "ready"


def test_a_resume_while_the_jobs_dispatch_runs_is_queued_and_runs_after(world: World) -> None:
    """B4: _spawn used to drop it, and the reset API said `resumed`."""
    w = world
    w.runner.background = True
    gate, ran = threading.Event(), []

    def first(j: str) -> None:
        gate.wait(5)
        ran.append(("first", j))

    def second(j: str) -> None:
        ran.append(("second", j))

    assert w.runner._spawn(first, "job-x") == "started"
    assert w.runner._spawn(second, "job-x") == "queued"
    gate.set()
    for _ in range(100):
        if len(ran) == 2:
            break
        time.sleep(0.02)
    assert ran == [("first", "job-x"), ("second", "job-x")]


def test_a_breaker_reset_reports_a_running_job_as_queued(world: World) -> None:
    w = world
    w.runner.cfg.budgets.wip_per_repo = 2
    first = _approve(w)
    held = _approve(w)  # its two PRs would pass the limit: held by the WIP breaker
    assert w.store.get_job(held).state.value == "blocked"
    (page,) = _breaker_pages(w)
    for t in w.store.list_tasks(first):
        w.store.update_task(t.id, state="merged")
    with w.runner._running_lock:
        w.runner._running.add(held)  # its last dispatch is still finishing
    w.runner.background = True
    r = w.client.post(f"/api/inbox/{page['id']}/reset", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert r.json()["resumed"] == [] and r.json()["queued"] == [held]


# --- B5, B10 / coord 3: WIP ---------------------------------------------------------------------


def test_wip_never_pauses_a_fix_round(world: World) -> None:
    w = world
    w.runner.cfg.budgets.wip_per_repo = 2

    def review(spec: SessionSpec) -> Any:
        if str(spec.task_id).endswith("w1") and not w.runner.breakers.open_breakers():
            # the stack is full: the WIP breaker is open while w1's fix round waits
            w.runner.breakers.trip(Kind.WIP, f"repo:{REPO}", {"limit": 2})
            return CHANGES
        return CLEAN_REVIEW

    w.standin.review = review
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    w1 = [s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id).endswith("w1")]
    assert len(w1) == 2  # the fix round ran under the open WIP breaker


def test_two_jobs_competing_for_one_wip_slot_get_one_pr(world: World) -> None:
    """B10: the room check and the move to running were not atomic across jobs."""
    from vigild.scheduler import wip

    w = world
    w.runner.cfg.budgets.wip_per_repo = 1
    w.standin.coord_drafts = [lambda j: (spec_n(1), plan_n(j, 1))] * 2
    w.standin.worker_edit = _doc_work()
    real_dispatch = w.runner.dispatch
    w.runner.dispatch = lambda j: None  # type: ignore[method-assign]
    a, b = _request(w), _request(w)
    for item in _inbox(w)["specs"]:
        w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert {w.store.get_job(j).state.value for j in (a, b)} == {"building"}
    w.runner.dispatch = real_dispatch  # type: ignore[method-assign]
    real_room = wip.room

    def slow_room(store: Any, repo: str, limit: int) -> int:
        n = real_room(store, repo, limit)
        time.sleep(0.2)  # both jobs read the room before either takes it
        return n

    wip.room = slow_room  # type: ignore[assignment]
    try:
        threads = [threading.Thread(target=w.runner.dispatch, args=(j,)) for j in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
    finally:
        wip.room = real_room  # type: ignore[assignment]
    assert len(w.publisher.prs) == 1
    states = sorted(w.store.get_job(j).state.value for j in (a, b))
    assert states == ["blocked", "ready"]


# --- B6, A11 / coord 4: breakers move only where the call is made --------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 23, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_reading_a_cooled_breaker_never_moves_it(cfg: Any, store: Any) -> None:
    clock = Clock()
    b = Breakers(store, cfg, clock=clock)
    scope = "backend:claude-sonnet"
    b.trip(Kind.BACKEND, scope, {"preset": "claude-sonnet"})
    clock.now += timedelta(seconds=cfg.vigild.breakers.backend_cooldown_seconds + 1)
    for _ in range(3):
        assert b.is_open(Kind.BACKEND, scope)
        b.blocking(repo=REPO)
    row = b._row(Kind.BACKEND, scope)
    assert row is not None and row.state == "open"
    assert b.probe_ready(Kind.BACKEND, scope)


def test_one_probe_is_claimed_and_released_when_no_call_was_made(cfg: Any, store: Any) -> None:
    clock = Clock()
    b = Breakers(store, cfg, clock=clock)
    scope = "backend:claude-sonnet"
    b.trip(Kind.BACKEND, scope, {"preset": "claude-sonnet"})
    assert not b.claim_probe(Kind.BACKEND, scope)  # still cooling down
    clock.now += timedelta(seconds=cfg.vigild.breakers.backend_cooldown_seconds + 1)
    assert b.claim_probe(Kind.BACKEND, scope)
    assert not b.claim_probe(Kind.BACKEND, scope)  # one probe at a time
    b.release_probe(Kind.BACKEND, scope)  # dispatch stopped before upstream
    assert b.claim_probe(Kind.BACKEND, scope)


def test_a_successful_probe_closes_the_breaker_and_resumes_the_jobs_it_held(
    world: World,
) -> None:
    w = world
    clock = Clock()
    br = w.runner.breakers
    br.clock = clock
    scope = "backend:claude-sonnet"
    w.runner.cfg.routing.fallbacks["claude-sonnet"] = []
    b = br.trip(Kind.BACKEND, scope, {"preset": "claude-sonnet"})
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    assert job in br.get(b.id).held
    clock.now += timedelta(seconds=w.runner.cfg.vigild.breakers.backend_cooldown_seconds + 1)
    assert br.claim_probe(Kind.BACKEND, scope)  # the gateway's first call after cool-down
    br.success(Kind.BACKEND, scope)  # ...answered
    assert br.get(b.id).state == "closed"
    assert w.store.get_job(job).state.value == "ready"


def test_the_gateway_claims_the_probe_at_the_call(cfg: Any, store: Any) -> None:
    import httpx2

    from vigild.budget import Budget
    from vigild.config import Secrets
    from vigild.gateway.service import Gateway, GatewayError

    clock = Clock()
    calls: list[int] = []

    def upstream(req: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(200, json={
            "model": "claude-haiku-5-5", "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{"type": "text", "text": "{}"}]})  # fmt: skip

    gw = Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({"ANTHROPIC_API_KEY": "sk"}),
                 transport=httpx2.MockTransport(upstream))  # fmt: skip
    gw.breakers = Breakers(store, cfg, clock=clock)
    scope = "backend:claude-haiku"
    gw.breakers.trip(Kind.BACKEND, scope, {"preset": "claude-haiku"})
    body = {"max_tokens": 16, "messages": [{"role": "user", "content": "x"}]}
    vigil = "thomasbardhi01/vigil"
    with pytest.raises(GatewayError):  # open, cooling down: refused, nothing sent
        gw.call(repo=vigil, preset="claude-haiku", body=body, purpose="decider", job_id=None,
                private=False)  # fmt: skip
    assert calls == []
    clock.now += timedelta(seconds=cfg.vigild.breakers.backend_cooldown_seconds + 1)
    gw.call(repo=vigil, preset="claude-haiku", body=body, purpose="decider", job_id=None,
            private=False)  # fmt: skip
    assert calls == [1]
    row = gw.breakers._row(Kind.BACKEND, scope)
    assert row is not None and row.state == "closed"


# --- B7 / coord 5 / owner 3: deps PRs -------------------------------------------------------------


def test_a_job_whose_prs_the_owner_merged_during_review_moves_to_merged(world: World) -> None:
    """B7(a): every PR merged while the job was in review (or blocked)."""
    w = world
    real = w.runner.review
    w.runner.review = lambda j: None  # type: ignore[method-assign]
    job = _approve(w)
    w.runner.review = real  # type: ignore[method-assign]
    assert w.store.get_job(job).state.value == "in_review"
    w.runner.pr_state = lambda repo, url: "merged"
    assert w.runner.poll_prs() == [job]
    assert w.store.get_job(job).state.value == "merged"


def test_a_blocked_job_whose_prs_were_merged_by_hand_moves_to_merged(world: World) -> None:
    """B7(b): a page held the job; the owner merged its PRs anyway."""
    w = world
    w.standin.review = lambda spec: {}
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    w.runner.pr_state = lambda repo, url: "merged"
    assert w.runner.poll_prs() == [job]
    assert w.store.get_job(job).state.value == "merged"
    # its page is resolved: nothing is left to act on
    assert [p for p in _pages(w, job) if p["status"] == "open"] == []


def test_every_open_page_on_a_merged_job_is_resolved(world: World) -> None:
    w = world
    w.standin.review = lambda spec: {}
    job = _approve(w)
    w.runner.pr_state = lambda repo, url: "merged"
    w.runner.poll_prs()
    assert [i for i in w.store.list_inbox() if i.job_id == job] == []
    assert [i for i in w.store.list_inbox("resolved") if i.job_id == job]


# --- coord decision 2: one walk over every blocked or held path -----------------------------------


def _blocked_by(w: World, how: str) -> str:
    """Drive a fresh job into one blocked or held path; return its id."""
    from tests.jobs.test_coord_loop import _flaky
    from tests.review.test_review_stage import _rounds
    from vigild.repos import set_visibility_source

    if how == "worker_failed":
        w.standin.worker_edit = _flaky({f"w{i}" for i in range(1, 9)})
        return _approve(w)
    if how == "review_failed":
        w.standin.review = lambda spec: {}
        return _approve(w)
    if how == "review_rounds":
        w.standin.review = _rounds({"w1": [CHANGES, CHANGES, CHANGES]})
        return _approve(w)
    if how == "budget":  # the month can't fit it: a cap that waiting never frees (#15)
        return _run_n(w, 3, lambda: w.store.x(
            "INSERT INTO ledger (task_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
            ("elsewhere", 99.5, "2000-01-01", w.budget._month(), "x")))  # fmt: skip
    if how == "daily_breaker":
        return _run_n(w, 3, lambda: w.store.x(
            "INSERT INTO ledger (task_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
            ("elsewhere", 5.0, w.budget._day(), w.budget._month(), "x")))  # fmt: skip
    if how == "vigild_error":
        w.runner.review = lambda j: (_ for _ in ()).throw(RuntimeError("bug"))  # type: ignore[method-assign]
        return _approve(w)
    if how == "cant_run":
        job = _request(w)
        (item,) = _inbox(w)["specs"]
        w.runner.cfg.repos.repos[REPO].dev_image_tools = ["git"]
        w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
        return job
    if how == "repo_policy":
        real = w.runner.dispatch

        def flip(j: str) -> None:
            set_visibility_source(lambda r: r == REPO)
            real(j)

        w.runner.dispatch = flip  # type: ignore[method-assign]
        return _approve(w)
    if how == "wip_breaker":
        w.runner.cfg.budgets.wip_per_repo = 2
        _approve(w)
        return _approve(w)
    if how == "restart":
        from vigild.jobs.fsm import Actor, transition

        job = _request(w)
        (item,) = _inbox(w)["specs"]
        w.runner.build = lambda j: None  # type: ignore[method-assign]
        w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
        transition(w.store, job, JobState.PLANNING, Actor.VIGILD)
        w.runner.reconcile()
        return job
    raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    ["worker_failed", "review_failed", "review_rounds", "budget", "daily_breaker",
     "vigild_error", "cant_run", "repo_policy", "wip_breaker", "restart"],
)  # fmt: skip
def test_every_blocked_path_leaves_a_page_to_act_on(world: World, how: str) -> None:
    """needs_deps and the deps job's second failed review are walked in
    tests/jobs/test_deps.py, with the same helper."""
    job = _blocked_by(world, how)
    assert_actionable(world, job)
