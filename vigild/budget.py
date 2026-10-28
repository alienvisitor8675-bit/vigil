"""Budgets with reservations (decision `budget-hard-stop`): daily, per task and monthly hard stops.

Before any session starts (coord, worker, reviewer, auditor) or vigild makes
a model call of its own (the decider), it reserves that session's
max_cost_usd. A reservation is refused unless

    spent (today) + every open reservation + this one <= daily cap,

the same against the month, and the task's own spend + this one <= the
per-task cap.

A bench job's reservations (bench 1a, owner 2026-10-27) are the one exception
to the day: they draw from the experiment line instead,

    spent (this month, experiment) + every open experiment reservation
        + this one <= experiment_monthly_usd,

never count against the daily cap, but do count toward the month, and a bench
task's own cap is bench_task_usd. A review replay (#47) is the same: its session draws
from the experiment line. A bench reservation the line has no room for
is refused, never queued. Every reservation and ledger row says which pool it
belongs to (`ops` or `experiment`), worked out here from the session, task or job
it is for, so a caller can't forget it.

Settling turns a reservation into metered spend (the sum of the
session's model_calls rows). The gateway refuses any call whose worst case
would take its session past the reservation, so metered spend can't pass a
cap by more than the error in that worst-case estimate (which errs high).
Days and months are UTC.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from vigild.config import Budgets
from vigild.db import Store, iso, utcnow
from vigild.metering.calls import session_cost

EPS = 1e-9
OPS, EXPERIMENT = "ops", "experiment"


class BudgetExceeded(Exception):
    def __init__(self, cap: str, detail: str) -> None:
        super().__init__(f"{cap} cap: {detail}")
        self.cap = cap
        self.detail = detail


class Budget:
    def __init__(self, store: Store, cfg: Budgets, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.cfg = cfg
        self.clock = clock

    def _day(self) -> str:
        return self.clock().strftime("%Y-%m-%d")

    def _month(self) -> str:
        return self.clock().strftime("%Y-%m")

    def _sum(self, sql: str, args: tuple[object, ...]) -> float:
        return float(self.store.q(sql, args)[0][0] or 0)

    def pool_of(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        job_id: str | None = None,
    ) -> str:
        """`experiment` for a bench job's session, task or job, or a review replay's
        session; else `ops`."""
        if self.store.is_replay(session_id):
            return EXPERIMENT
        bench = self.store.is_bench(job_id=job_id, session_id=session_id, task_id=task_id)
        return EXPERIMENT if bench else OPS

    def spent_today(self) -> float:
        """Today's spend on the operating budget; the experiment line is not the day's."""
        return self._sum(
            "SELECT SUM(amount_usd) FROM ledger WHERE day=? AND pool != ?",
            (self._day(), EXPERIMENT),
        )

    def open_today(self) -> float:
        """Every open reservation, whatever day it was made (2a round 7): a
        session still running at UTC midnight books its whole cost to the
        day it settles, so the new day's cap must already see it. One a
        crash left open is settled by the startup reconcile. Experiment holds are
        not the day's."""
        return self._sum(
            "SELECT SUM(amount_usd) FROM reservations WHERE status='open' AND pool != ?",
            (EXPERIMENT,),
        )

    def open_month(self) -> float:
        """Every open reservation, whatever month it was made (2a round 7)."""
        return self._sum("SELECT SUM(amount_usd) FROM reservations WHERE status='open'", ())

    def spent_month(self) -> float:
        """Every pool: experiment spend counts toward the month."""
        return self._sum("SELECT SUM(amount_usd) FROM ledger WHERE month=?", (self._month(),))

    def spent_experiment_month(self) -> float:
        return self._sum(
            "SELECT SUM(amount_usd) FROM ledger WHERE month=? AND pool=?",
            (self._month(), EXPERIMENT),
        )

    def open_experiment(self) -> float:
        """Every open experiment reservation, whatever month it was made."""
        return self._sum(
            "SELECT SUM(amount_usd) FROM reservations WHERE status='open' AND pool=?",
            (EXPERIMENT,),
        )

    def task_spent(self, task_id: str) -> float:
        return self._sum("SELECT SUM(amount_usd) FROM ledger WHERE task_id=?", (task_id,))

    def task_total(self, task_id: str) -> float:
        spent = self._sum("SELECT SUM(amount_usd) FROM ledger WHERE task_id=?", (task_id,))
        held = self._sum(
            "SELECT SUM(amount_usd) FROM reservations WHERE task_id=? AND status='open'",
            (task_id,),
        )
        return spent + held

    def open_reservation_ids(self) -> list[int]:
        return [int(r[0]) for r in self.store.q("SELECT id FROM reservations WHERE status='open'")]

    def reserve(
        self,
        *,
        session_id: str | None,
        task_id: str | None,
        amount: float,
        purpose: str,
        job_id: str | None = None,
    ) -> int:
        if amount <= 0:
            raise ValueError("a reservation must be positive")
        with self.store.tx() as c:
            self._refuse_abandoned(session_id=session_id, task_id=task_id, job_id=job_id)
            pool = self.pool_of(session_id=session_id, task_id=task_id, job_id=job_id)
            bench = pool == EXPERIMENT
            task_cap = self.cfg.bench_task_usd if bench else self.cfg.per_task_usd
            if task_id and self.task_total(task_id) + amount > task_cap + EPS:
                raise BudgetExceeded(
                    "bench_task" if bench else "per_task",
                    f"task {task_id} has {self.task_total(task_id):.2f} of "
                    f"{task_cap:.2f}; {amount:.2f} more doesn't fit",
                )
            if bench:
                used = self.spent_experiment_month() + self.open_experiment()
                if used + amount > self.cfg.experiment_monthly_usd + EPS:
                    raise BudgetExceeded(
                        "experiment",
                        f"{used:.2f} of {self.cfg.experiment_monthly_usd:.2f} used this month; "
                        f"{amount:.2f} more",
                    )
            else:
                day_used = self.spent_today() + self.open_today()
                if day_used + amount > self.cfg.daily_usd + EPS:
                    raise BudgetExceeded(
                        "daily",
                        f"{day_used:.2f} of {self.cfg.daily_usd:.2f} used; {amount:.2f} more",
                    )
            month_used = self.spent_month() + self.open_month()
            if month_used + amount > self.cfg.monthly_usd + EPS:
                raise BudgetExceeded(
                    "monthly",
                    f"{month_used:.2f} of {self.cfg.monthly_usd:.2f} used; {amount:.2f} more",
                )
            cur = c.execute(
                "INSERT INTO reservations (session_id, task_id, purpose, amount_usd, status, day,"
                " month, created_at, pool) VALUES (?,?,?,?, 'open', ?,?,?,?)",
                (
                    session_id,
                    task_id,
                    purpose,
                    amount,
                    self._day(),
                    self._month(),
                    iso(self.clock()),
                    pool,
                ),
            )
            return int(cur.lastrowid or 0)

    def _refuse_abandoned(
        self, *, session_id: str | None, task_id: str | None, job_id: str | None
    ) -> None:
        """A killed job opens no new reservation (#60): its sessions are over and the
        kill settled what it held."""
        if self.store.is_abandoned(job_id=job_id, session_id=session_id, task_id=task_id):
            raise BudgetExceeded("abandoned", "the job was abandoned; nothing new is reserved")

    def settle(self, reservation_id: int, amount: float | None = None) -> float:
        """Close a reservation; its session's metered cost (or `amount`, for a
        call vigild made itself, or a session whose spend is unknown) becomes spend.
        For a session an `amount` may be as much as all its open reservations together,
        and the others (an over-cap extension, a draw's top-up) close with it."""
        with self.store.tx() as c:
            row = c.execute(
                "SELECT session_id, task_id, status, pool FROM reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "open":
                return 0.0
            if amount is not None:
                # never more than was reserved (2a round 8): the rest is recorded,
                # and the caller (the gateway) trips the daily breaker for it
                if row["session_id"]:
                    held = float(
                        c.execute(
                            "SELECT SUM(amount_usd) FROM reservations"
                            " WHERE session_id=? AND status='open'",
                            (row["session_id"],),
                        ).fetchone()[0]
                    )
                else:
                    held = float(
                        c.execute(
                            "SELECT amount_usd FROM reservations WHERE id=?", (reservation_id,)
                        ).fetchone()[0]
                    )
                spent = min(amount, held)
                if amount > held + EPS:
                    self.store.add_event(
                        "settle_excess",
                        {"reservation": reservation_id, "reserved_usd": round(held, 6),
                         "reported_usd": round(amount, 6), "excess_usd": round(amount - held, 6)},
                        "human",
                    )  # fmt: skip
            elif row["session_id"]:
                spent = session_cost(self.store, row["session_id"])
            else:
                spent = 0.0
            c.execute(
                "UPDATE reservations SET status='settled', closed_at=? WHERE id=?",
                (iso(self.clock()), reservation_id),
            )
            if row["session_id"]:
                # the session's other reservations (a draw's top-up, an
                # over-cap extension) close with it: its cost is booked once
                c.execute(
                    "UPDATE reservations SET status='released', closed_at=?"
                    " WHERE session_id=? AND status='open' AND id != ?",
                    (iso(self.clock()), row["session_id"], reservation_id),
                )
            c.execute(
                "INSERT INTO ledger (reservation_id, session_id, task_id, amount_usd, day, month,"
                " ts, pool) VALUES (?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    row["session_id"],
                    row["task_id"],
                    spent,
                    self._day(),
                    self._month(),
                    iso(self.clock()),
                    row["pool"],
                ),
            )
            return spent

    def book_unmetered(self, session_id: str, task_id: str | None, amount: float) -> None:
        """Book a call's worst case as spent when its real cost couldn't be
        recorded. Unknown counts as worst case, never zero. The session's own
        settle later adds only its metered calls, so this is not counted twice."""
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO ledger (reservation_id, session_id, task_id, amount_usd, day, month,"
                " ts, pool) VALUES (NULL,?,?,?,?,?,?,?)",
                (
                    session_id,
                    task_id,
                    amount,
                    self._day(),
                    self._month(),
                    iso(self.clock()),
                    self.pool_of(session_id=session_id, task_id=task_id),
                ),
            )

    def draw(self, parent_id: int, *, session_id: str, amount: float, purpose: str) -> int:
        """A session's own reservation, taken out of a job's review-stage
        reservation (owner, 2026-10-23): the stage shrinks by what the session
        takes, so the total held doesn't change. Every open reservation counts
        against today and this month whatever day it was made (2a round 7),
        so a draw after UTC midnight moves money within the new day's count.
        If the stage has less left than `amount`, the rest is reserved under
        the caps as usual; if that is refused, the draw is undone."""
        rid, take = 0, 0.0
        with self.store.tx() as c:
            self._refuse_abandoned(session_id=session_id, task_id=None, job_id=None)
            row = c.execute(
                "SELECT amount_usd, status, pool FROM reservations WHERE id=?", (parent_id,)
            ).fetchone()
            if row is not None and row["status"] == "open":
                take = min(amount, float(row["amount_usd"]))
            if take > 0:
                c.execute(
                    "UPDATE reservations SET amount_usd = amount_usd - ? WHERE id=?",
                    (take, parent_id),
                )
                cur = c.execute(
                    "INSERT INTO reservations (session_id, task_id, purpose, amount_usd, status,"
                    " day, month, created_at, pool) VALUES (?,?,?,?, 'open', ?,?,?,?)",
                    (session_id, None, purpose, take, self._day(), self._month(),
                     iso(self.clock()), row["pool"]),
                )  # fmt: skip
                rid = int(cur.lastrowid or 0)
        if take <= 0:
            return self.reserve(session_id=session_id, task_id=None, amount=amount, purpose=purpose)
        if amount - take > EPS:
            try:
                self.reserve(
                    session_id=session_id, task_id=None, amount=amount - take, purpose=purpose
                )
            except BudgetExceeded:
                with self.store.tx() as c:  # undo the draw: nothing is left behind
                    c.execute(
                        "UPDATE reservations SET status='released', closed_at=? WHERE id=?",
                        (iso(self.clock()), rid),
                    )
                    c.execute(
                        "UPDATE reservations SET amount_usd = amount_usd + ? WHERE id=?",
                        (take, parent_id),
                    )
                raise
        return rid

    def settle_session(self, session_id: str) -> float:
        rows = self.store.q(
            "SELECT id FROM reservations WHERE session_id=? AND status='open'", (session_id,)
        )
        return sum(self.settle(int(r[0])) for r in rows)

    def session_headroom(self, session_id: str) -> float:
        held = self._sum(
            "SELECT SUM(amount_usd) FROM reservations WHERE session_id=? AND status='open'",
            (session_id,),
        )
        if held <= 0:
            return 0.0
        return held - session_cost(self.store, session_id)

    def spent_today_by_purpose(self) -> dict[str, float]:
        """Today's metered spend per purpose: a Haiku decider call made because
        Jev failed or drifted books `decider_fallback`, apart from `decider`
        (owner, 2026-10-23), so a drift can't quietly eat the day."""
        rows = self.store.q(
            "SELECT purpose, SUM(cost_usd) n FROM model_calls WHERE ts LIKE ? GROUP BY purpose",
            (f"{self._day()}%",),
        )
        return {str(r["purpose"]): round(float(r["n"] or 0), 6) for r in rows}

    def summary(self) -> dict[str, float]:
        return {
            "daily_cap": self.cfg.daily_usd,
            "monthly_cap": self.cfg.monthly_usd,
            "per_task_cap": self.cfg.per_task_usd,
            "spent_today": round(self.spent_today(), 6),
            "reserved_open": round(self.open_today(), 6),
            "spent_month": round(self.spent_month(), 6),
            "experiment_cap": self.cfg.experiment_monthly_usd,
            "experiment_spent_month": round(self.spent_experiment_month(), 6),
            "experiment_reserved_open": round(self.open_experiment(), 6),
            "decider_today": self.spent_today_by_purpose().get("decider", 0.0),
            "decider_fallback_today": self.spent_today_by_purpose().get("decider_fallback", 0.0),
        }
