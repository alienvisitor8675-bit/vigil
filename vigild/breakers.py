"""Circuit breakers (design "Circuit breakers"; plan 2a, item 4): one module,
one table, one inbox page type.

A breaker has a kind and a scope. Tripped (open), it pauses dispatch for its
scope and pages the owner once, however often it trips again; the owner resets
it from the inbox, and the jobs it held resume. A backend breaker also closes
on its own: after its cool-down one call goes through (half-open), and a
success closes it.

| kind | scope | trips when |
|---|---|---|
| daily_cap, monthly_cap | global | the day's (month's) metered spend reaches the cap (B14) |
| per_task_cap | task:<id> | a task's reservation is exhausted |
| wip | repo:<name> | four open fleet PRs (B41) |
| escalations | global | more than `max_escalations_per_day` escalations today (UTC) |
| fix_depth | tree:<root job> | a child job past depth 3 |
| backend_errors | backend:<preset> | N 5xx or timeouts in a row; routes fall to the next preset |
| repo_failures | repo:<name> | K consecutive failed or abandoned jobs |
| verifier_unknowns | repo:<name> | M `unknown` verdicts in a row |
| decider_credit | route:jev-cloudflare | Cloudflare answers 2021: the route is off until reset |
| decider_fallback_cap | repo:<name> | a Haiku fallback call would cross the repo's day cap |

**A read never moves a breaker** (review B6, A11): `is_open` and `blocking`
only report. A backend breaker past its cool-down is `probe_ready`; its one
half-open probe is claimed (`claim_probe`) by the gateway, at the call it
lets through upstream, and released (`release_probe`) when that call never
reached upstream. The probe's success closes the breaker and hands the jobs
it held to `on_close` (the runner resumes them); its failure opens it again.

The daily and monthly breakers trip when the day's (month's) metered spend
reaches the cap, not when one reservation doesn't fit: a job whose wave
doesn't fit gets its own page (review B1).
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
    # Jev costs cents a day; a repo's Haiku fallback crossing $1 means it has
    # drifted or is failing (owner ruling 4)
    DECIDER_FALLBACK = "decider_fallback_cap"


GLOBAL = "global"
GLOBAL_KINDS = (Kind.DAILY, Kind.MONTHLY, Kind.ESCALATIONS)
REPO_KINDS = (Kind.WIP, Kind.REPO_FAILURES, Kind.VERIFIER_UNKNOWNS, Kind.DECIDER_FALLBACK)
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
        # the jobs a breaker held, when a probe closes it (the runner resumes them)
        self.on_close: Callable[[list[str], int], Any] | None = None

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
        """Open (or half-open, a probe in flight) pauses its scope. A read
        only: nothing moves here."""
        b = self._row(kind, scope)
        return b is not None and b.state != "closed"

    def _cooled(self, b: Breaker) -> bool:
        return b.reopen_at is not None and iso(self.clock()) >= b.reopen_at

    def probe_ready(self, kind: Kind, scope: str) -> bool:
        """An open backend breaker past its cool-down, with no probe in
        flight: work may go to it, and its first call will be the probe."""
        b = self._row(kind, scope)
        return b is not None and b.kind is Kind.BACKEND and b.state == "open" and self._cooled(b)

    def claim_probe(self, kind: Kind, scope: str) -> bool:
        """Claim the one half-open probe, where the upstream call is made."""
        with self.store.lock:
            b = self._row(kind, scope)
            if b is None or b.kind is not Kind.BACKEND or b.state != "open" or not self._cooled(b):
                return False
            self._set(b, state="half_open")
            return True

    def release_probe(self, kind: Kind, scope: str) -> None:
        """The claimed call never reached upstream: the next caller may probe."""
        with self.store.lock:
            b = self._row(kind, scope)
            if b is not None and b.state == "half_open":
                self._set(b, state="open")

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
        actions: tuple[str, ...] = ("reset", "abandon"),
        title: str | None = None,
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
                    title or f"breaker tripped: {kind.value} ({scope})",
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
        """The run of failures ends; a half-open backend breaker closes, and
        the jobs it held go to `on_close`."""
        held: list[str] = []
        item_id = 0
        with self.store.lock:
            b = self._row(kind, scope)
            if b is None:
                return
            if b.state == "half_open":
                held = self._close(b, by="probe")
                item_id = int(b.item_id or 0)
            elif b.state == "closed" and b.count:
                self._set(b, count=0)
        if held and self.on_close is not None:
            self.on_close(held, item_id)

    def unhold(self, job_id: str) -> list[Breaker]:
        """The job no longer waits on any breaker: the owner abandoned it, or
        its own page holds it now (2a round 3). Returns the breakers that
        then hold nothing."""
        emptied = []
        with self.store.lock:
            for b in self.open_breakers():
                if job_id in b.held:
                    rest = [j for j in b.held if j != job_id]
                    self._set(b, held=rest)
                    if not rest:
                        emptied.append(b)
        return emptied

    def empty_page(self, b: Breaker) -> int | None:
        """The page of a breaker that now holds nothing, for the caller to
        resolve. The breaker itself doesn't move (owner, 2026-10-23): a page
        closing is not dispatch resuming. Only its own condition (spend
        under the cap, a probe's success, a reset) reopens dispatch, and a
        job it holds later gets a fresh page."""
        cur = self.get(b.id)
        if cur.state == "closed" or cur.held:
            return None
        return cur.item_id

    def held_by(self, breaker_id: int) -> list[str]:
        try:
            return self.get(breaker_id).held
        except KeyError:
            return []

    def hold(self, b: Breaker, job_id: str) -> None:
        """Note a job this breaker paused; resetting it resumes the job. If
        the breaker's page was resolved while it stayed open (its last held
        job abandoned), the job gets a fresh page (2a round 8)."""
        with self.store.lock:
            cur = self.get(b.id)
            if job_id not in cur.held:
                self._set(cur, held=[*cur.held, job_id])
            if cur.state != "closed" and self.pager is not None and not self._page_open(cur):
                actions = ("reset", "replan", "abandon") if cur.kind is Kind.PER_TASK else (
                    "reset", "abandon")  # fmt: skip
                item = self.pager(
                    job_id,
                    f"breaker tripped: {cur.kind.value} ({cur.scope})",
                    {"kind": "breaker", "breaker": cur.id, "breaker_kind": cur.kind.value,
                     "scope": cur.scope, "detail": cur.detail, "actions": list(actions)},
                )  # fmt: skip
                self._set(self.get(b.id), item_id=getattr(item, "id", None))

    def _page_open(self, b: Breaker) -> bool:
        if b.item_id is None:
            return False
        try:
            return self.store.get_inbox_item(int(b.item_id)).status == "open"
        except KeyError:
            return False

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
