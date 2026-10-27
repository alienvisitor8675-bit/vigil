"""2a round 5, the owner's ruling on review and audit caps (2026-10-23):

1. Build waves reserve only build caps. The review stage reserves once per
   job, when the job enters review: (reviewer cap + auditor cap) x the tasks
   under review in that pass. Sessions draw from it; the rest is released
   when the pass ends; a fix round's re-review reserves again.
2. Reviewer and auditor caps are per-session limits, checked call by call
   (spend so far plus the next call's worst case); nothing reserves them at
   dispatch.
3. A review or audit that would cross its session cap completes: the
   overage is charged to the day (still a hard stop), recorded on the span,
   and a shape's second overage pages once."""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.jobs.test_coord_loop import World, _approve
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages, _run_n
from tests.review.test_review_stage import CHANGES, _rounds
from vigild.executors.base import SessionSpec
from vigild.jobs.model import JobState, Role


def _reserves(w: World) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []
    real = w.budget.reserve

    def spy(**kw: Any) -> int:
        rid = real(**kw)
        calls.append((str(kw["purpose"]), float(kw["amount"])))
        return rid

    w.budget.reserve = spy  # type: ignore[method-assign]
    return calls


def test_the_starting_caps_are_in_budgets_yaml(world: World) -> None:
    b = world.runner.cfg.budgets
    assert (b.review_session_usd, b.audit_session_usd) == (0.40, 0.80)


def test_a_build_wave_reserves_only_build_caps(world: World) -> None:
    """A wave of 3 at $5/day (main's queue, #15): each worker reserves its own cap
    ($2), the third waits for the day, and none of it is a review cap."""
    w = world
    w.runner.sleep = lambda s: time.sleep(0.01)  # type: ignore[method-assign]
    job = _run_n(w, 3)
    assert w.store.get_job(job).state is JobState.READY
    held = [r["amount_usd"] for r in
            w.store.q("SELECT amount_usd FROM reservations WHERE purpose='worker'")]  # fmt: skip
    assert held == [2.0, 2.0, 2.0]


def test_nothing_reserves_review_caps_while_workers_build(world: World) -> None:
    w = world
    seen: list[int] = []

    def during_build(spec: SessionSpec) -> None:
        if spec.role is Role.WORKER:
            rows = w.store.q(
                "SELECT COUNT(*) n FROM reservations WHERE status='open'"
                " AND purpose IN ('review_stage','reviewer','auditor')"
            )
            seen.append(int(rows[0]["n"]))

    w.standin.on_session = during_build
    _approve(w)
    assert seen and set(seen) == {0}


def test_the_review_stage_reserves_once_per_job_by_task_count_and_releases(
    world: World,
) -> None:
    w = world
    calls = _reserves(w)
    job = _approve(w)  # two tasks
    assert w.store.get_job(job).state is JobState.READY
    b = w.runner.cfg.budgets
    stage = [a for p, a in calls if p == "review_stage"]
    assert stage == [pytest.approx(2 * (b.review_session_usd + b.audit_session_usd))]
    assert not [p for p, _ in calls if p in ("reviewer", "auditor")]  # drawn, not reserved
    assert w.budget.open_today() == 0  # what the pass didn't use went back at its end


def test_a_fix_rounds_re_review_reserves_again(world: World) -> None:
    w = world
    calls = _reserves(w)
    w.standin.review = _rounds({"w1": [CHANGES]})
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.READY
    b = w.runner.cfg.budgets
    per = b.review_session_usd + b.audit_session_usd
    assert [a for p, a in calls if p == "review_stage"] == [
        pytest.approx(2 * per), pytest.approx(1 * per)]  # fmt: skip
    assert w.budget.open_today() == 0


def test_a_refused_review_stage_reservation_pages_the_job(world: World) -> None:
    from vigild.budget import BudgetExceeded

    w = world
    real = w.budget.reserve

    def full(**kw: Any) -> int:
        if kw.get("purpose") == "review_stage":
            raise BudgetExceeded("monthly", "a test says the month is held")  # a day would queue
        return real(**kw)

    w.budget.reserve = full  # type: ignore[method-assign]
    job = _approve(w)
    (page,) = _pages(w, job)
    assert page["body"]["kind"] == "budget"
    assert {"retry", "abandon"} <= set(page["body"]["actions"])


# --- (3) over-cap reviews complete -------------------------------------------------------------


def _spend_reviews(w: World, usd: float) -> None:
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    prices = PriceTable(w.runner.cfg.prices)
    sonnet_out = 10.0 / 1e6  # $10/M output

    def spend(spec: SessionSpec) -> None:
        if spec.role is Role.REVIEWER:
            record_call(w.store, prices, session_id=spec.session_id, task_id=spec.task_id,
                        job_id=spec.job_id, purpose="session", provider="anthropic",
                        backend_preset="claude-sonnet", model="claude-sonnet-5-5",
                        usage=CallUsage(tokens_out=round(usd / sonnet_out)))  # fmt: skip

    w.standin.on_session = spend


def test_an_over_cap_review_completes_is_charged_to_the_day_and_on_the_span(
    world: World,
) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    w = world
    exp = InMemorySpanExporter()
    w.runner.telemetry.provider.add_span_processor(SimpleSpanProcessor(exp))
    _spend_reviews(w, 0.50)  # over the $0.40 reviewer cap
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.READY  # completed, not failed
    over = [e.data for e in w.store.events(job_id=job) if e.type == "review_over_cap"]
    assert len(over) == 2 and over[0]["overage_usd"] == pytest.approx(0.10)
    assert w.budget.spent_today() >= 2 * 0.50  # the whole cost, overage included
    spans = [s for s in exp.get_finished_spans()
             if s.attributes.get("vigil.role") == "reviewer"
             and "vigil.review.over_cap" in s.attributes]  # fmt: skip
    assert len(spans) == 2  # one per reviewer session
    assert all(s.attributes["vigil.review.over_cap"] is True for s in spans)
    assert all(s.attributes["vigil.review.overage_usd"] == pytest.approx(0.10) for s in spans)


def test_a_shapes_second_overage_pages_once_and_a_third_does_not(world: World) -> None:
    w = world
    for _ in range(3):
        w.runner._note_over_cap("docs", Role.REVIEWER, None, None, cap=0.40, cost=0.55)
    pages = [p for p in _pages(w) if p["body"].get("kind") == "review_caps"]
    assert len(pages) == 1
    assert pages[0]["title"] == (
        "Review caps too low for shape docs: 2 overages; consider re-sizing"
    )


# --- the gateway: over-cap calls go on; the day stays a hard stop ---------------------------------


@pytest.fixture
def gw(cfg: Any, store: Any) -> Any:
    from vigild.budget import Budget
    from vigild.config import Secrets
    from vigild.gateway.service import Gateway

    return Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({"ANTHROPIC_API_KEY": "sk"}))


def _review_session(store: Any, gw: Any, role: Role, cap: float) -> str:
    s = store.create_session(None, role, backend_preset="claude-sonnet")
    gw.budget.reserve(session_id=s.id, task_id=None, amount=cap, purpose=role.value)
    return str(s.id)


MODEL = "claude-sonnet-5-5"


def _hold(gw: Any, sid: str, usd: float) -> float:
    """Hold a call whose worst case is at least `usd`: the input sized to it, with
    the output at the session cap (the gateway clamps max_tokens to that)."""
    body = {"model": MODEL, "max_tokens": 16384}
    n = 0
    while gw.worst_case(MODEL, body, n) < usd:
        n += 1000
    amount, _body, _raw = gw._hold_clamped(sid, MODEL, body, b"x" * n)
    return float(amount)


def test_a_review_call_past_its_cap_is_held_against_the_day(gw: Any, store: Any) -> None:
    sid = _review_session(store, gw, Role.REVIEWER, 0.40)
    held = _hold(gw, sid, 0.60)  # worst case past the $0.40 cap: allowed, the day has room
    assert 0.60 <= held < 0.62
    assert gw.budget.session_headroom(sid) >= held  # its reservation grew, against the day
    worker = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    gw.budget.reserve(session_id=worker.id, task_id="t1", amount=0.03, purpose="worker")
    from vigild.gateway.service import GatewayError

    with pytest.raises(GatewayError):  # a worker's cap is still its cap
        _hold(gw, worker.id, 0.20)


def test_a_review_call_that_would_cross_the_day_is_refused(gw: Any, store: Any) -> None:
    from vigild.gateway.service import GatewayError

    sid = _review_session(store, gw, Role.AUDITOR, 0.80)
    gw.budget.reserve(session_id=None, task_id=None, amount=4.15, purpose="coord")  # the day
    with pytest.raises(GatewayError, match="daily"):
        _hold(gw, sid, 0.90)


def test_settling_an_over_cap_session_books_its_cost_once(gw: Any, store: Any, cfg: Any) -> None:
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    sid = _review_session(store, gw, Role.REVIEWER, 0.40)
    _hold(gw, sid, 0.60)
    record_call(store, PriceTable(cfg.prices), session_id=sid, task_id=None, job_id=None,
                purpose="session", provider="anthropic", backend_preset="claude-sonnet",
                model="claude-sonnet-5-5", usage=CallUsage(tokens_out=55_000))  # $0.55  # fmt: skip
    assert gw.budget.settle_session(sid) == pytest.approx(0.55)
    assert gw.budget.spent_today() == pytest.approx(0.55) and gw.budget.open_today() == 0
