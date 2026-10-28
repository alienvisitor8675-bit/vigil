"""Bench 1a: the experiment ledger. Bench reservations draw from
experiment_monthly_usd, never from the $5 day, and count toward the $100 month."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config
from vigild.db import Store
from vigild.jobs.model import Role


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def budget(cfg: Config, store: Store, clock: Clock) -> Budget:
    return Budget(store, cfg.budgets, clock=clock)


def _bench_session(store: Store) -> str:
    job = store.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    return store.create_session(job.id, Role.WORKER).id


def _ops_session(store: Store) -> str:
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    return store.create_session(job.id, Role.WORKER).id


def test_config_has_the_experiment_line(cfg: Config) -> None:
    assert cfg.budgets.experiment_monthly_usd == 30.00
    assert cfg.budgets.bench_task_usd == 1.50


def test_bench_reservations_never_touch_the_day(budget: Budget, store: Store) -> None:
    ops = _ops_session(store)
    budget.reserve(session_id=ops, task_id=None, amount=5.0, purpose="worker")  # the day is full
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id=ops, task_id=None, amount=0.5, purpose="worker")
    assert e.value.cap == "daily"
    bench = _bench_session(store)
    rid = budget.reserve(session_id=bench, task_id=None, amount=1.5, purpose="worker")
    assert budget.open_today() == 5.0  # the bench hold is not the day's
    budget.settle(rid, 1.0)
    assert budget.spent_today() == 0.0
    assert budget.spent_experiment_month() == 1.0


def test_bench_spend_counts_toward_the_month(cfg: Config, budget: Budget, store: Store) -> None:
    cfg.budgets.daily_usd = 100.0  # isolate the month
    cfg.budgets.monthly_usd = 3.0
    bench, ops = _bench_session(store), _ops_session(store)
    rid = budget.reserve(session_id=bench, task_id=None, amount=1.5, purpose="worker")
    budget.settle(rid, 1.5)
    assert budget.spent_month() == 1.5
    budget.reserve(session_id=ops, task_id=None, amount=1.0, purpose="worker")
    with pytest.raises(BudgetExceeded) as e:  # 1.5 bench + 1.0 held + 1.0 > 3.0
        budget.reserve(session_id=ops, task_id=None, amount=1.0, purpose="worker")
    assert e.value.cap == "monthly"
    with pytest.raises(BudgetExceeded) as e2:  # the experiment line has room; the month hasn't
        budget.reserve(session_id=bench, task_id=None, amount=0.6, purpose="worker")
    assert e2.value.cap == "monthly"


def test_a_full_experiment_line_refuses(budget: Budget, store: Store) -> None:
    for _ in range(20):  # 20 x 1.5 = 30.00
        budget.reserve(session_id=_bench_session(store), task_id=None, amount=1.5, purpose="w")
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id=_bench_session(store), task_id=None, amount=0.01, purpose="w")
    assert e.value.cap == "experiment"
    assert budget.summary()["experiment_reserved_open"] == 30.0


def test_the_experiment_line_is_monthly_and_utc(budget: Budget, store: Store, clock: Clock) -> None:
    rid = budget.reserve(session_id=_bench_session(store), task_id=None, amount=1.5, purpose="w")
    budget.settle(rid, 1.5)
    assert budget.spent_experiment_month() == 1.5
    clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    assert budget.spent_experiment_month() == 0.0


def test_a_bench_task_has_its_own_cap(budget: Budget, store: Store) -> None:
    job = store.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    task = store.create_task(job.id, {"task_id": f"{job.id}-w1"}, "claude")
    budget.reserve(session_id=None, task_id=task.id, amount=1.5, purpose="worker")
    with pytest.raises(BudgetExceeded) as e:
        budget.reserve(session_id=None, task_id=task.id, amount=0.1, purpose="worker")
    assert e.value.cap == "bench_task"
    # an ordinary task keeps the $2 cap
    ops = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    t2 = store.create_task(ops.id, {"task_id": f"{ops.id}-w1"}, "claude")
    budget.reserve(session_id=None, task_id=t2.id, amount=2.0, purpose="worker")


def test_a_non_bench_job_never_draws_from_it(budget: Budget, store: Store) -> None:
    ops = _ops_session(store)
    rid = budget.reserve(session_id=ops, task_id=None, amount=2.0, purpose="worker")
    budget.settle(rid, 1.0)
    s = budget.summary()
    assert s["experiment_reserved_open"] == 0.0
    assert budget.spent_experiment_month() == 0.0
    assert budget.spent_today() == 1.0
    for _ in range(20):  # a full experiment line leaves the day alone
        budget.reserve(session_id=_bench_session(store), task_id=None, amount=1.5, purpose="w")
    budget.reserve(session_id=ops, task_id=None, amount=1.0, purpose="worker")


def test_a_draw_and_a_sweep_keep_the_pool(budget: Budget, store: Store) -> None:
    """One way to do each thing: a draw's session reservation and its settle stay in the line."""
    bench = _bench_session(store)
    stage = budget.reserve(
        session_id=None,
        task_id=None,
        amount=1.0,
        purpose="review",
        job_id=(store.get_session(bench).job_id),
    )
    rid = budget.draw(stage, session_id=bench, amount=0.4, purpose="reviewer")
    budget.settle(rid, 0.3)
    budget.settle(stage, 0.0)
    assert budget.spent_experiment_month() == pytest.approx(0.3)
    assert budget.spent_today() == 0.0


def test_doctor_shows_the_lines_spend(cfg: Config) -> None:
    from vigild.doctor import _experiment_line

    assert _experiment_line(cfg).ok is None  # no vigil.db yet
    cfg.vigild.data_path.mkdir(parents=True, exist_ok=True)
    store = Store.open(cfg.vigild.data_path / "vigil.db")
    b = Budget(store, cfg.budgets)
    rid = b.reserve(session_id=_bench_session(store), task_id=None, amount=1.5, purpose="w")
    b.settle(rid, 1.25)
    check = _experiment_line(cfg)
    assert check.name == "experiment budget" and check.ok is True
    assert "$1.25 spent" in check.note and "$30.00" in check.note


def test_a_bench_jobs_own_call_books_to_the_experiment_line(
    cfg: Config, store: Store, budget: Budget
) -> None:
    """The decider and every call vigild makes with no session or task: given the
    job, it is the experiment line's, never the $5 day."""
    from vigild.config import Secrets
    from vigild.gateway.service import Gateway

    gw = Gateway(cfg, store, budget, Secrets({}))
    job = store.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    day = budget.spent_today()
    with gw._own_call(0.4, "decider", job_id=job.id) as cost:
        assert budget.open_experiment() == 0.4
        assert budget.open_today() == 0.0
        cost[0] = 0.3
    assert budget.spent_experiment_month() == 0.3
    assert budget.spent_today() == day
    ops = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    with gw._own_call(0.4, "decider", job_id=ops.id) as cost:
        assert budget.open_today() == 0.4
        cost[0] = 0.1
    assert budget.spent_today() == day + 0.1


def test_remaining_caps_a_bench_task_at_the_bench_cap(world: dict[str, object]) -> None:
    store = world["store"]
    runner = world["runner"]
    assert isinstance(store, Store)
    job = store.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    task = store.create_task(
        job.id, {"task_id": f"{job.id}-w1", "limits": {"max_cost_usd": 9.0}}, "claude"
    )
    cap, _, _ = runner.remaining(task.id)  # type: ignore[attr-defined]
    assert cap == 1.5
    ops = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    t2 = store.create_task(
        ops.id, {"task_id": f"{ops.id}-w1", "limits": {"max_cost_usd": 9.0}}, "claude"
    )
    assert runner.remaining(t2.id)[0] == 2.0  # type: ignore[attr-defined]
