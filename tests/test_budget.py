"""Budgets are hard stops: daily $5, per task $2, monthly $100, with reservations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config
from vigild.db import Store
from vigild.metering.calls import CallUsage, record_call
from vigild.metering.prices import PriceTable


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def budget(cfg: Config, store: Store, clock: Clock) -> Budget:
    return Budget(store, cfg.budgets, clock=clock)


def _spend(store: Store, cfg: Config, session: str, dollars: float) -> None:
    # Sonnet output is $10/M tokens, so 100k output tokens is $1.
    record_call(
        store,
        PriceTable(cfg.prices),
        session_id=session,
        task_id=None,
        job_id=None,
        purpose="worker",
        provider="anthropic",
        backend_preset="claude-sonnet",
        model="claude-sonnet-5-5",
        usage=CallUsage(tokens_out=int(dollars * 100_000)),
    )


def test_reservation_counts_against_the_day(budget: Budget) -> None:
    budget.reserve(session_id="s1", task_id="t1", amount=2.0, purpose="worker")
    budget.reserve(session_id="s2", task_id="t2", amount=2.0, purpose="worker")
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id="s3", task_id="t3", amount=1.5, purpose="worker")
    assert e.value.cap == "daily"
    budget.reserve(session_id="s3", task_id="t3", amount=1.0, purpose="worker")  # exactly $5


def test_per_task_cap(budget: Budget) -> None:
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id="s1", task_id="t1", amount=2.5, purpose="worker")
    assert e.value.cap == "per_task"
    budget.reserve(session_id="s1", task_id="t1", amount=1.5, purpose="worker")
    with pytest.raises(BudgetExceeded):  # second attempt of the same task
        budget.reserve(session_id="s2", task_id="t1", amount=1.0, purpose="worker")


def test_settle_turns_the_reservation_into_metered_spend(
    budget: Budget, store: Store, cfg: Config
) -> None:
    rid = budget.reserve(session_id="s1", task_id="t1", amount=2.0, purpose="worker")
    _spend(store, cfg, "s1", 0.5)
    assert budget.settle(rid) == pytest.approx(0.5)
    assert budget.spent_today() == pytest.approx(0.5)
    assert budget.open_today() == 0
    # the $1.50 not spent is free again
    budget.reserve(session_id="s2", task_id="t2", amount=2.0, purpose="worker")
    budget.reserve(session_id="s3", task_id="t3", amount=2.0, purpose="worker")


def test_daily_cap_resets_at_utc_midnight(budget: Budget, clock: Clock) -> None:
    for i in range(2):
        budget.reserve(session_id=f"s{i}", task_id=f"t{i}", amount=2.0, purpose="worker")
    with pytest.raises(BudgetExceeded):
        budget.reserve(session_id="x", task_id="tx", amount=2.0, purpose="worker")
    clock.now += timedelta(days=1)
    # yesterday's open reservations still count until settled, so settle them first
    for rid in budget.open_reservation_ids():
        budget.settle(rid)
    budget.reserve(session_id="x", task_id="tx", amount=2.0, purpose="worker")


def test_monthly_cap(budget: Budget, store: Store, cfg: Config, clock: Clock) -> None:
    cfg.budgets.monthly_usd = 6.0
    for day in range(3):
        rid = budget.reserve(session_id=f"s{day}", task_id=f"t{day}", amount=2.0, purpose="worker")
        _spend(store, cfg, f"s{day}", 2.0)
        budget.settle(rid)
        clock.now += timedelta(days=1)
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id="s9", task_id="t9", amount=0.5, purpose="worker")
    assert e.value.cap == "monthly"


def test_session_headroom(budget: Budget, store: Store, cfg: Config) -> None:
    budget.reserve(session_id="s1", task_id="t1", amount=1.0, purpose="worker")
    _spend(store, cfg, "s1", 0.25)
    assert budget.session_headroom("s1") == pytest.approx(0.75)
    assert budget.session_headroom("nobody") == 0


def test_the_overshoot_bound_is_one_call_per_session(budget: Budget) -> None:
    """B14's written bound: the day can't exceed its cap by more than the
    in-flight calls, and the gateway's pre-call check makes even that zero
    for any call whose worst case it can price."""
    assert budget.cfg.daily_usd == 5.0 and budget.cfg.per_task_usd == 2.0
    assert budget.cfg.monthly_usd == 100.0


def test_open_reservations_count_against_their_own_day_only(budget: Budget, clock: Clock) -> None:
    """Review r1, item 7: a reservation left open yesterday (vigild died) no
    longer blocks today; the startup reconcile settles it."""
    for i in range(2):
        budget.reserve(session_id=f"s{i}", task_id=f"t{i}", amount=2.0, purpose="worker")
    clock.now += timedelta(days=1)
    assert budget.open_today() == 0
    budget.reserve(session_id="x", task_id="tx", amount=2.0, purpose="worker")


def test_review_and_audit_spend_count_toward_the_day_and_the_month(
    budget: Budget, store: Store, cfg: Config
) -> None:
    """Owner ruling 2: reviews and audits sit outside the task's $2 cap
    (reserved with no task) but count toward the daily and monthly caps."""
    w = budget.reserve(session_id="w", task_id="t1", amount=2.0, purpose="worker")
    rv = budget.reserve(session_id="rv", task_id=None, amount=0.5, purpose="reviewer")
    au = budget.reserve(session_id="au", task_id=None, amount=1.0, purpose="auditor")
    assert budget.task_total("t1") == pytest.approx(2.0)  # outside the task's cap
    assert budget.open_today() == pytest.approx(3.5) and budget.open_month() == pytest.approx(3.5)
    with pytest.raises(BudgetExceeded) as e:  # $3.50 of $5 held: a second audit doesn't fit
        budget.reserve(session_id="au2", task_id=None, amount=1.6, purpose="auditor")
    assert e.value.cap == "daily"
    for rid, sess, usd in ((w, "w", 0.2), (rv, "rv", 0.3), (au, "au", 0.9)):
        _spend(store, cfg, sess, usd)
        budget.settle(rid)
    assert budget.spent_today() == pytest.approx(1.4) and budget.spent_month() == pytest.approx(1.4)
    cfg.budgets.monthly_usd = 1.5
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id="rv2", task_id=None, amount=0.5, purpose="reviewer")
    assert e.value.cap == "monthly"


# --- 2a round 6: a draw counts against the day and month it happens in ------------------------


def _stage_then_draw(budget: Budget, clock: Clock, store: Store, *, before: datetime,
                     after: datetime) -> int:  # fmt: skip
    from vigild.jobs.model import Role

    clock.now = before
    stage = budget.reserve(session_id=None, task_id=None, amount=1.2, purpose="review_stage")
    s = store.create_session(None, Role.AUDITOR, backend_preset="claude-opus")
    clock.now = after
    budget.draw(stage, session_id=s.id, amount=0.80, purpose="auditor")
    return stage


def test_a_draw_after_midnight_counts_against_the_new_day(
    budget: Budget, clock: Clock, store: Store
) -> None:
    """The final review's probe: a stage reserved at 23:50 on day D, an
    auditor drawing $0.80 at 00:05 on D+1; a coord's $5 must not fit then."""
    d = datetime(2026, 10, 23, 23, 50, tzinfo=UTC)
    _stage_then_draw(budget, clock, store, before=d, after=d + timedelta(minutes=15))
    assert budget.open_today() == pytest.approx(0.80)
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id=None, task_id=None, amount=5.0, purpose="coord")
    assert e.value.cap == "daily"
    clock.now = d  # day D: the drawn part isn't counted twice
    assert budget.open_today() == pytest.approx(0.40)


def test_a_draw_after_a_month_boundary_counts_against_the_new_month(
    budget: Budget, clock: Clock, store: Store, cfg: Config
) -> None:
    cfg.budgets.daily_usd, cfg.budgets.monthly_usd = 50.0, 6.0
    d = datetime(2026, 10, 31, 23, 50, tzinfo=UTC)
    _stage_then_draw(budget, clock, store, before=d, after=d + timedelta(minutes=15))
    assert budget.open_month() == pytest.approx(0.80)
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id=None, task_id=None, amount=5.5, purpose="coord")
    assert e.value.cap == "monthly"


def test_a_draws_top_up_that_doesnt_fit_leaves_nothing_behind(budget: Budget, store: Store) -> None:
    from vigild.jobs.model import Role

    budget.reserve(session_id=None, task_id=None, amount=4.6, purpose="coord")
    stage = budget.reserve(session_id=None, task_id=None, amount=0.4, purpose="review_stage")
    s = store.create_session(None, Role.AUDITOR, backend_preset="claude-opus")
    with pytest.raises(BudgetExceeded):
        budget.draw(stage, session_id=s.id, amount=0.80, purpose="auditor")  # $0.40 short
    assert store.q("SELECT COUNT(*) n FROM reservations WHERE session_id=? AND status='open'",
                   (s.id,))[0]["n"] == 0  # fmt: skip
    (row,) = store.q("SELECT amount_usd, status FROM reservations WHERE id=?", (stage,))
    assert (row["amount_usd"], row["status"]) == (pytest.approx(0.4), "open")
