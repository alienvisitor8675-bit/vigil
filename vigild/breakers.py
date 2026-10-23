"""Circuit breakers (design "Circuit breakers"; plan 2a, item 4): one module,
one table, one inbox page type.

A breaker has a kind and a scope. Tripped (open), it pauses dispatch for its
scope and pages the owner once, however often it trips again; the owner resets
it from the inbox, and the jobs it held resume. A backend breaker also closes
on its own: after its cool-down one call goes through (half-open), and a
success closes it.

| kind | scope | trips when |
|---|---|---|
| daily_cap, monthly_cap | global | a reservation hits the cap (B14) |
| per_task_cap | task:<id> | a task's reservation is exhausted |
| wip | repo:<name> | four open fleet PRs (B41) |
| escalations | global | more than `max_escalations_per_day` escalations today (UTC) |
| fix_depth | tree:<root job> | a child job past depth 3 |
| backend_errors | backend:<preset> | N 5xx or timeouts in a row; routes fall to the next preset |
| repo_failures | repo:<name> | K consecutive failed or abandoned jobs |
| verifier_unknowns | repo:<name> | M `unknown` verdicts in a row |
| decider_credit | route:jev-cloudflare | Cloudflare answers 2021: the route is off until reset |
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from vigild.config import Config
from vigild.db import Store, iso, utcnow
from vigild.provenance import Provenance


class Kind(StrEnum):
    DAILY = "daily_cap"
    MONTHLY = "monthly_cap"
    PER_TASK = "per_task_cap"
    WIP = "wip"
    ESCALATIONS = "escalations"
    FIX_DEPTH = "fix_depth"
    BACKEND = "backend_errors"
    REPO_FAILURES = "repo_failures"
    VERIFIER_UNKNOWNS = "verifier_unknowns"
    DECIDER_CREDIT = "decider_credit"  # Cloudflare's 2021: the AI Gateway credit is empty


GLOBAL = "global"
GLOBAL_KINDS = (Kind.DAILY, Kind.MONTHLY, Kind.ESCALATIONS)
REPO_KINDS = (Kind.WIP, Kind.REPO_FAILURES, Kind.VERIFIER_UNKNOWNS)
CAP_KIND = {"daily": Kind.DAILY, "monthly": Kind.MONTHLY, "per_task": Kind.PER_TASK}


@dataclass(frozen=True)
class Breaker:
    id: int
    kind: Kind
    scope: str
    state: str  # closed | open | half_open
    count: int
    item_id: int | None
    held: list[str]
    detail: dict[str, Any]
    reopen_at: str | None


Pager = Callable[[str | None, str, dict[str, Any]], Any]  # (job_id, title, body) -> InboxItem


class Breakers:
    def __init__(
        self,
        store: Store,
        cfg: Config,
        pager: Pager | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.pager = pager
        self.clock = clock

    # --- reading --------------------------------------------------------------------

    def _row(self, kind: Kind, scope: str) -> Breaker | None:
        rows = self.store.q("SELECT * FROM breakers WHERE kind=? AND scope=?", (kind.value, scope))
        return self._load(rows[0]) if rows else None

    @staticmethod
    def _load(r: Any) -> Breaker:
        return Breaker(
            id=int(r["id"]),
            kind=Kind(r["kind"]),
            scope=str(r["scope"]),
            state=str(r["state"]),
            count=int(r["count"]),
            item_id=r["item_id"],
            held=json.loads(r["held"]),
            detail=json.loads(r["detail"]),
            reopen_at=r["reopen_at"],
        )

    def get(self, breaker_id: int) -> Breaker:
        rows = self.store.q("SELECT * FROM breakers WHERE id=?", (breaker_id,))
        if not rows:
            raise KeyError(breaker_id)
        return self._load(rows[0])

    def is_open(self, kind: Kind, scope: str) -> bool:
        """Open pauses its scope. A backend breaker past its cool-down goes
        half-open: this caller's probe is let through, and only it."""
        b = self._row(kind, scope)
        if b is None or b.state == "closed":
            return False
        cooled = b.reopen_at is not None and iso(self.clock()) >= b.reopen_at
        if b.state == "open" and kind is Kind.BACKEND and cooled:
            self._set(b, state="half_open")
            return False
        return True

    def blocking(
        self,
        *,
        repo: str | None = None,
        task: str | None = None,
        tree: str | None = None,
        wip: bool = True,
    ) -> Breaker | None:
        """The first open breaker whose scope covers this work, or None. The
        WIP breaker counts only for work that opens PRs (`wip`)."""
        scopes = [(k, GLOBAL) for k in GLOBAL_KINDS]
        if repo:
            scopes += [(k, f"repo:{repo}") for k in REPO_KINDS if wip or k is not Kind.WIP]
        if task:
            scopes.append((Kind.PER_TASK, f"task:{task}"))
        if tree:
            scopes.append((Kind.FIX_DEPTH, f"tree:{tree}"))
        for kind, scope in scopes:
            if self.is_open(kind, scope):
                return self._row(kind, scope)
        return None

    # --- moving -----------------------------------------------------------------------

    def _ensure(self, kind: Kind, scope: str) -> Breaker:
        self.store.x(
            "INSERT OR IGNORE INTO breakers (kind, scope, state, count, held, detail, updated_at)"
            " VALUES (?,?,'closed',0,'[]','{}',?)",
            (kind.value, scope, iso(self.clock())),
        )
        b = self._row(kind, scope)
        assert b is not None
        return b

    def _set(self, b: Breaker, **fields: Any) -> None:
        for k in ("held", "detail"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        sets = ", ".join(f"{k}=?" for k in fields)
        self.store.x(
            f"UPDATE breakers SET {sets}, updated_at=? WHERE id=?",  # noqa: S608 (fixed names)
            (*fields.values(), iso(self.clock()), b.id),
        )

    def trip(
        self,
        kind: Kind,
        scope: str,
        detail: dict[str, Any],
        *,
        job_id: str | None = None,
        actions: tuple[str, ...] = ("reset",),
    ) -> Breaker:
        """Open the breaker. It pages once: tripping an open breaker again adds
        nothing to the inbox."""
        with self.store.lock:
            b = self._ensure(kind, scope)
            if b.state in ("open", "half_open"):
                if b.state == "half_open":  # the probe failed: open again
                    self._set(b, state="open", reopen_at=self._cooldown())
                return self._row(kind, scope) or b
            item_id = None
            if self.pager is not None:
                item = self.pager(
                    job_id,
                    f"breaker tripped: {kind.value} ({scope})",
                    {
                        "kind": "breaker",
                        "breaker": b.id,
                        "breaker_kind": kind.value,
                        "scope": scope,
                        "detail": detail,
                        "actions": list(actions),
                    },
                )
                item_id = getattr(item, "id", None)
            self._set(
                b,
                state="open",
                count=max(b.count, 1),
                item_id=item_id,
                detail=detail,
                reopen_at=self._cooldown() if kind is Kind.BACKEND else None,
            )
            self.store.add_event(
                "breaker",
                {"breaker": b.id, "kind": kind.value, "scope": scope, "state": "open", **detail},
                Provenance.HUMAN,
                job_id=job_id,
            )
            return self._row(kind, scope) or b

    def _cooldown(self) -> str:
        secs = self.cfg.vigild.breakers.backend_cooldown_seconds
        return iso(self.clock() + timedelta(seconds=secs))

    def failure(
        self, kind: Kind, scope: str, threshold: int, detail: dict[str, Any]
    ) -> Breaker | None:
        """One more in a row; trips at `threshold`. Returns the breaker if open."""
        with self.store.lock:
            b = self._ensure(kind, scope)
            if b.state == "half_open":
                return self.trip(kind, scope, detail)
            count = b.count + 1
            self._set(b, count=count)
            if count >= threshold:
                return self.trip(kind, scope, {**detail, "in_a_row": count})
            return None

    def success(self, kind: Kind, scope: str) -> None:
        """The run of failures ends; a half-open backend breaker closes."""
        with self.store.lock:
            b = self._row(kind, scope)
            if b is None:
                return
            if b.state == "half_open":
                self._close(b, by="probe")
            elif b.state == "closed" and b.count:
                self._set(b, count=0)

    def hold(self, b: Breaker, job_id: str) -> None:
        """Note a job this breaker paused; resetting it resumes the job."""
        with self.store.lock:
            cur = self.get(b.id)
            if job_id not in cur.held:
                self._set(cur, held=[*cur.held, job_id])

    def _close(self, b: Breaker, *, by: str) -> list[str]:
        self._set(b, state="closed", count=0, held=[], reopen_at=None)
        self.store.add_event(
            "breaker",
            {"breaker": b.id, "kind": b.kind.value, "scope": b.scope, "state": "closed", "by": by},
            Provenance.HUMAN,
        )
        return b.held

    def reset(self, breaker_id: int) -> list[str]:
        """The owner's reset, from the inbox: closed, its count cleared. Returns
        the jobs it held, for the runner to resume."""
        with self.store.lock:
            return self._close(self.get(breaker_id), by="owner")

    def open_breakers(self) -> list[Breaker]:
        rows = self.store.q("SELECT * FROM breakers WHERE state != 'closed' ORDER BY id")
        return [self._load(r) for r in rows]


def escalations_today(store: Store, clock: Callable[[], datetime] = utcnow) -> int:
    day = clock().strftime("%Y-%m-%d")
    rows = store.q(
        "SELECT COUNT(*) n FROM events WHERE type='escalation' AND ts LIKE ?", (f"{day}%",)
    )
    return int(rows[0]["n"])
