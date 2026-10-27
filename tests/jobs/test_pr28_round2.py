"""PR #28, round 2: where #25's in_review invariant meets 2a, the review reserve in
the budget queue, and two money-booking gaps (classes of issue, not their sites)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import vigild.gateway.service as gsvc
from tests.jobs.test_coord_loop import CLEAN_REVIEW, World, _approve, _events
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages
from tests.jobs.test_round9 import PA, Up, _setup
from vigild.breakers import GLOBAL, Kind
from vigild.executors.base import SessionSpec
from vigild.jobs.model import JobState, Role
from vigild.metering.calls import CallUsage, record_call
from vigild.metering.prices import PriceTable

S = JobState


def _gap_pages(w: World, job: str) -> list[dict[str, Any]]:
    return [p for p in _pages(w, job) if p["body"].get("kind") == "plan_without_pr"]


def _in_review(w: World) -> str:
    w.runner.review = lambda j: None  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state is S.IN_REVIEW
    return job


# --- 1: a resume the invariant refuses is paged, not stranded --------------------------


def test_a_breaker_reset_after_a_pr_closed_pages_the_job(world: World) -> None:
    w = world
    job = _in_review(w)
    b = w.runner.breakers.trip(Kind.DAILY, GLOBAL, {"why": "a test"}, job_id=job)
    w.runner._hold_for(job, b)
    assert w.store.get_job(job).state is S.BLOCKED
    w.store.update_task(f"{job}-w1", state="closed")  # the PR poll saw it closed
    w.runner.on_breaker_reset(b.id, 0)
    assert w.store.get_job(job).state is S.BLOCKED
    (page,) = _gap_pages(w, job)
    assert page["body"]["actions"] == ["replan", "abandon"] and f"{job}-w1" in page["body"]["tasks"]


def test_a_probe_closing_after_a_pr_closed_pages_the_job(world: World) -> None:
    w = world
    job = _in_review(w)
    b = w.runner.breakers.trip(Kind.DAILY, GLOBAL, {"why": "a test"}, job_id=job)
    w.runner._hold_for(job, b)
    w.store.update_task(f"{job}-w1", state="closed")
    w.runner._probe_closed([job], 0)
    assert len(_gap_pages(w, job)) == 1


# --- 2 and 6: a closed PR never lets a job reach ready ---------------------------------


def _close_w1_during_w2_review(w: World, how: str) -> list[str]:
    job_box: list[str] = []

    def review(spec: SessionSpec) -> dict[str, Any]:
        job = str(spec.job_id)
        job_box.append(job)
        if str(spec.task_id).endswith("w2"):
            if how == "poll":
                url = w.runner._task_pr(f"{job}-w1")
                w.runner.pr_state = lambda repo, u: "closed" if u == url else "open"
                w.runner.poll_prs()
            else:
                w.store.update_task(f"{job}-w1", state="closed")
        return CLEAN_REVIEW

    w.standin.review = review
    _approve(w)
    return job_box


@pytest.mark.parametrize("how", ["poll", "direct"])
def test_a_pr_closing_mid_pass_blocks_the_job_with_the_gap_page(world: World, how: str) -> None:
    w = world
    (job, *_) = _close_w1_during_w2_review(w, how)
    assert w.store.get_job(job).state is S.BLOCKED  # never ready
    (page,) = _gap_pages(w, job)  # not a vigild_error with a retry that can't work
    assert page["body"]["actions"] == ["replan", "abandon"]
    assert not [p for p in _pages(w, job) if p["body"].get("kind") == "vigild_error"]


def test_the_poll_pages_a_ready_job_whose_pr_was_closed(world: World) -> None:
    w = world
    job = _approve(w)
    assert w.store.get_job(job).state is S.READY
    url = w.runner._task_pr(f"{job}-w1")
    w.runner.pr_state = lambda repo, u: "closed" if u == url else "open"
    w.runner.poll_prs()
    assert w.store.get_job(job).state is S.BLOCKED
    assert len(_gap_pages(w, job)) == 1


def test_a_merged_pr_beside_reviewed_ones_still_reaches_ready(world: World) -> None:
    w = world
    job_box: list[str] = []

    def review(spec: SessionSpec) -> dict[str, Any]:
        if str(spec.task_id).endswith("w2"):
            w.store.update_task(f"{spec.job_id}-w1", state="merged")  # the owner merged one
        job_box.append(str(spec.job_id))
        return CLEAN_REVIEW

    w.standin.review = review
    _approve(w)
    assert w.store.get_job(job_box[0]).state is S.READY


# --- 3: the review reserve queues like a worker ----------------------------------------


def _fill(w: World) -> list[int]:
    return [
        w.budget.reserve(session_id=None, task_id=f"other-{i}", amount=a, purpose="worker")
        for i, a in enumerate((2.0, 2.0, 0.98))
    ]


def _wrap_stage(w: World, before: Any) -> None:
    real = w.runner._review_stage

    def stage(*a: Any, **k: Any) -> Any:
        before()
        return real(*a, **k)

    w.runner._review_stage = stage  # type: ignore[method-assign]


def test_a_full_day_makes_the_review_reserve_wait_without_a_page(world: World) -> None:
    w = world
    held: list[int] = []
    _wrap_stage(w, lambda: held.extend(_fill(w)))

    def sleep(s: float) -> None:
        for rid in held:
            w.budget.settle(rid, 0.0)

    w.runner.sleep = sleep  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state is S.READY
    assert [e["for"] for e in _events(w, "queued")] == ["review_stage"]
    assert [e["for"] for e in _events(w, "dequeued")] == ["review_stage"]
    assert not [p for p in _pages(w, job) if p["body"].get("kind") == "budget"]
    assert w.budget.open_today() == 0


def test_a_review_reserve_waits_behind_an_older_queued_worker(world: World) -> None:
    w = world
    old = ("other-job", "other-job-w1", "worker")
    _wrap_stage(w, lambda: w.runner._queue_join(old))
    polls: list[int] = []

    def sleep(s: float) -> None:
        polls.append(1)
        w.runner._queue_leave(old)  # the older waiter is served (or gone)

    w.runner.sleep = sleep  # type: ignore[method-assign]
    job = _approve(w)
    assert polls, "the review reserve took the day ahead of an older waiter"
    (q,) = _events(w, "queued")
    assert q["for"] == "review_stage" and "older" in q["detail"]
    assert w.store.get_job(job).state is S.READY


# --- 4: a fail-closed settle books the session once ------------------------------------


def test_a_failed_settle_books_the_whole_session_once(world: World) -> None:
    w = world
    s = w.store.create_session(None, Role.REVIEWER, backend_preset="claude-sonnet")
    rid = w.budget.reserve(session_id=s.id, task_id=None, amount=0.40, purpose="reviewer")
    w.budget.reserve(session_id=s.id, task_id=None, amount=0.10, purpose="review_over_cap")
    record_call(w.store, PriceTable(w.runner.cfg.prices), session_id=s.id, task_id=None,
                job_id=None, purpose="session", provider="anthropic",
                backend_preset="claude-sonnet", model="claude-sonnet-5-5",
                usage=CallUsage(tokens_out=50_000))  # $0.50  # fmt: skip
    real = w.budget.settle

    def settle(reservation_id: int, amount: float | None = None) -> float:
        if amount is None:
            raise RuntimeError("ledger write failed")
        return real(reservation_id, amount)

    w.budget.settle = settle  # type: ignore[method-assign]
    spec = SimpleNamespace(job_id=None, task_id=None)
    w.runner._settle_fail_closed(s, spec, rid, None)  # type: ignore[arg-type]
    w.budget.settle = real  # type: ignore[method-assign]
    assert w.budget.open_reservation_ids() == []  # nothing left for the sweep to book again
    rows = w.store.q("SELECT SUM(amount_usd) n FROM ledger WHERE session_id=?", (s.id,))
    assert rows[0]["n"] == pytest.approx(0.50)


# --- 5: vigild's own calls fail closed too ---------------------------------------------


def _own_metered(gw: Any, store: Any, **kw: Any) -> None:
    gw.metered(repo="thomasbardhi01/vigil", route="jev-cloudflare", provider="cloudflare",
               model="typesafe/jev", purpose="decider", job_id=None, private=False,
               run=lambda: {"usage": {"input_tokens": 1000, "output_tokens": 10}})  # fmt: skip


def test_a_metering_failure_on_vigilds_own_call_books_the_worst_case_and_pages(
    cfg: Any, monkeypatch: Any
) -> None:
    store, budget, gw, _sweep, _clock = _setup(cfg, monkeypatch)

    def boom(*a: Any, **k: Any) -> float:
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(gsvc, "record_call", boom)
    _own_metered(gw, store)
    booked = store.q("SELECT SUM(amount_usd) n FROM ledger")[0]["n"]
    assert booked and booked > 0  # unknown is the worst case, never $0
    assert [i for i in store.list_inbox() if "metering failed" in i.title]
    assert budget.open_reservation_ids() == []


def test_another_calls_row_landing_mid_failure_is_never_booked_for_this_call(
    cfg: Any, monkeypatch: Any
) -> None:
    """The failed call books exactly its own reservation and pages, whatever other
    calls wrote meanwhile; the page says the call may be counted twice."""
    store, _budget, gw, _sweep, _clock = _setup(cfg, monkeypatch)
    real = gsvc.record_call

    def other_lands_then_fail(*a: Any, **k: Any) -> float:
        real(store, a[1], session_id=None, task_id=None, job_id=None, purpose="other",
             provider="anthropic", backend_preset="claude-sonnet", model="claude-sonnet-5-5",
             usage=CallUsage(tokens_out=90_000))  # another call's $0.90 row  # fmt: skip
        raise RuntimeError("late failure")

    monkeypatch.setattr(gsvc, "record_call", other_lands_then_fail)
    _own_metered(gw, store)
    reserved = store.q("SELECT amount_usd a FROM reservations")[0]["a"]
    booked = store.q("SELECT SUM(amount_usd) n FROM ledger")[0]["n"]
    assert booked == pytest.approx(reserved)
    (page,) = [i for i in store.list_inbox() if "metering failed" in i.title]
    assert "counted twice" in page.body["note"]


def test_haiku_decider_failure_is_fail_closed_too(cfg: Any, monkeypatch: Any) -> None:
    store, _budget, gw, _sweep, _clock = _setup(cfg, monkeypatch, Up())

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(gw, "_record", boom)
    gw.call(repo=PA, preset="claude-haiku", purpose="decider", job_id=None, private=False,
            body={"max_tokens": 100, "messages": [{"role": "user", "content": "q"}]})  # fmt: skip
    assert (store.q("SELECT SUM(amount_usd) n FROM ledger")[0]["n"] or 0) > 0
    assert [i for i in store.list_inbox() if "metering failed" in i.title]
