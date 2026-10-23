"""Budgets with reservations (B14): daily, per task and monthly hard stops.

Before any session starts (coord, worker, reviewer, auditor) or vigild makes
a model call of its own (the decider), it reserves that session's
max_cost_usd. A reservation is refused unless

    spent (today) + open reservations (today) + this one <= daily cap,

the same against the month, and the task's own spend + this one <= the
per-task cap. Settling turns a reservation into metered spend (the sum of the
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

    def spent_today(self) -> float:
        return self._sum("SELECT SUM(amount_usd) FROM ledger WHERE day=?", (self._day(),))

    def open_today(self) -> float:
        # Open reservations count against the day they were made. One left open
        # by a crash stops counting at midnight; the startup reconcile settles it.
        return self._sum(
            "SELECT SUM(amount_usd) FROM reservations WHERE status='open' AND day=?",
            (self._day(),),
        )

    def open_month(self) -> float:
        return self._sum(
            "SELECT SUM(amount_usd) FROM reservations WHERE status='open' AND month=?",
            (self._month(),),
        )

    def spent_month(self) -> float:
        return self._sum("SELECT SUM(amount_usd) FROM ledger WHERE month=?", (self._month(),))

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
        self, *, session_id: str | None, task_id: str | None, amount: float, purpose: str
    ) -> int:
        if amount <= 0:
            raise ValueError("a reservation must be positive")
        with self.store.tx() as c:
            if task_id and self.task_total(task_id) + amount > self.cfg.per_task_usd + EPS:
                raise BudgetExceeded(
                    "per_task",
                    f"task {task_id} has {self.task_total(task_id):.2f} of "
                    f"{self.cfg.per_task_usd:.2f}; {amount:.2f} more doesn't fit",
                )
            day_used = self.spent_today() + self.open_today()
            if day_used + amount > self.cfg.daily_usd + EPS:
                raise BudgetExceeded(
                    "daily", f"{day_used:.2f} of {self.cfg.daily_usd:.2f} used; {amount:.2f} more"
                )
            month_used = self.spent_month() + self.open_month()
            if month_used + amount > self.cfg.monthly_usd + EPS:
                raise BudgetExceeded(
                    "monthly",
                    f"{month_used:.2f} of {self.cfg.monthly_usd:.2f} used; {amount:.2f} more",
                )
            cur = c.execute(
                "INSERT INTO reservations (session_id, task_id, purpose, amount_usd, status, day,"
                " month, created_at) VALUES (?,?,?,?, 'open', ?,?,?)",
                (
                    session_id,
                    task_id,
                    purpose,
                    amount,
                    self._day(),
                    self._month(),
                    iso(self.clock()),
                ),
            )
            return int(cur.lastrowid or 0)

    def settle(self, reservation_id: int, amount: float | None = None) -> float:
        """Close a reservation; its session's metered cost (or `amount`, for a
        call vigild made itself) becomes spend."""
        with self.store.tx() as c:
            row = c.execute(
                "SELECT session_id, task_id, status FROM reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "open":
                return 0.0
            if amount is not None:
                spent = amount
            elif row["session_id"]:
                spent = session_cost(self.store, row["session_id"])
            else:
                spent = 0.0
            c.execute(
                "UPDATE reservations SET status='settled', closed_at=? WHERE id=?",
                (iso(self.clock()), reservation_id),
            )
            if amount is None and row["session_id"]:
                # the session's other reservations (a draw's top-up, an
                # over-cap extension) close with it: its cost is booked once
                c.execute(
                    "UPDATE reservations SET status='released', closed_at=?"
                    " WHERE session_id=? AND status='open' AND id != ?",
                    (iso(self.clock()), row["session_id"], reservation_id),
                )
            c.execute(
                "INSERT INTO ledger (reservation_id, session_id, task_id, amount_usd, day, month,"
                " ts) VALUES (?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    row["session_id"],
                    row["task_id"],
                    spent,
                    self._day(),
                    self._month(),
                    iso(self.clock()),
                ),
            )
            return spent

    def draw(self, parent_id: int, *, session_id: str, amount: float, purpose: str) -> int:
        """A session's own reservation, taken out of a job's review-stage
        reservation (owner, 2026-10-23). A draw counts against the day and
        month it happens in (2a round 6): on the stage's own day and month
        the stage shrinks by what the session takes and the child carries
        the same stamp, so the total held doesn't change; after a UTC day or
        month boundary the child is reserved afresh, with the new day's and
        month's cap check, and the stage still shrinks so the old day stops
        counting that part. If the stage has less left than `amount`, the
        rest is reserved as usual; if that fails, the draw is undone."""
        day, month = self._day(), self._month()
        rid, take = 0, 0.0
        with self.store.tx() as c:
            row = c.execute(
                "SELECT amount_usd, status, day, month FROM reservations WHERE id=?", (parent_id,)
            ).fetchone()
            open_ = row is not None and row["status"] == "open"
            if open_ and (row["day"], row["month"]) == (day, month):
                take = min(amount, float(row["amount_usd"]))
            if take > 0:
                c.execute(
                    "UPDATE reservations SET amount_usd = amount_usd - ? WHERE id=?",
                    (take, parent_id),
                )
                cur = c.execute(
                    "INSERT INTO reservations (session_id, task_id, purpose, amount_usd, status,"
                    " day, month, created_at) VALUES (?,?,?,?, 'open', ?,?,?)",
                    (session_id, None, purpose, take, day, month, iso(self.clock())),
                )
                rid = int(cur.lastrowid or 0)
        if take <= 0:
            # another day or month (or nothing left): this day's and month's caps decide
            rid = self.reserve(session_id=session_id, task_id=None, amount=amount, purpose=purpose)
            if open_:
                self._shrink(parent_id, amount)
            return rid
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

    def _shrink(self, reservation_id: int, amount: float) -> None:
        with self.store.tx() as c:
            c.execute(
                "UPDATE reservations SET amount_usd = MAX(0, amount_usd - ?)"
                " WHERE id=? AND status='open'",
                (amount, reservation_id),
            )

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
            "decider_today": self.spent_today_by_purpose().get("decider", 0.0),
            "decider_fallback_today": self.spent_today_by_purpose().get("decider_fallback", 0.0),
        }
