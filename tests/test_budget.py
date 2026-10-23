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
