"""The trust ladder (design "Review tiers and the trust ladder"; plan 2a,
item 1; 2a round 2: owner ruling 1, review B8).

- **Shapes** start where `configs/review.yaml` says. vigild keeps each
  shape's live rung in its `ladder` table; once a shape has a row, an edit to
  its starting rung in review.yaml is ignored (the row is the live state).
- **Every rung change goes through one function** (`_move`), which writes the
  new rung, a `ladder_changes` row (shape, old and new rung, reason, the
  audit sample size and match rate behind it, time) and a `ladder` event in
  one transaction. Nothing else writes a rung: a change without its row is
  impossible by construction. The row is the shape's "last change" on the
  API (`GET /api/review/ladder`) and the Review page.
- **Runner-file tasks** (scope names a file that decides how tests run) are
  rung 0 whatever their shape's rung, audited every time, and their audits are
  never recorded: they count toward no shape's graduation or demotion.
- **Rung 0 never graduates** (the design's table: "n/a; this is where every
  shape starts"). **No shape passes rung 1 before the local-reviewer eval**
  (design: "a reviewer enters rung 1 only if it matches >= 80% of the
  auditor's findings on the eval set"; `local_reviewer_eval` in review.yaml,
  None until phase 7). Until then every rung keeps the Sonnet reviewer and
  the Opus auditor on every review, so in practice audits only demote.
- **Who reviews and how often audits run** come from the rung: rung 0 and 1
  audit every review; above that a share, sampled twice as hard when the
  task's criteria sit at verify level 0.
- **Promotion** (from rung 1 up, after the eval) needs `promote_after` PRs
  since the last move, each counted by its verify level's weight (a level-0
  audit counts half), every one with a match rate at or above `min_match`, no
  missed medium or high finding, and at rung 1 no more than
  `max_false_per_pr` wrong reviewer findings.
- **Demotion:** a missed high finding drops a shape to rung 1 (or 0 from rung
  1); a missed medium finding at rung 2 or 3 drops it to rung 1. A missed
  finding also restarts the promotion window.
- **Hard limits, not configurable:** money, auth and consent stay at rung 0;
  deps never passes rung 1 (owner, 2026-10-23). `validate_policy` refuses a
  config that says otherwise; `set_rung` refuses a move past a limit and logs
  the refusal (a `ladder_refused` event).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vigild.config import Config, ConfigError
from vigild.contract.models import Audit, Severity, Shape, Task
from vigild.db import Store, iso
from vigild.intake.paths import matches_any
from vigild.provenance import Provenance

PERMANENT_RUNG_0 = frozenset({Shape.MONEY, Shape.AUTH, Shape.CONSENT})
MAX_RUNG = {Shape.DEPS: 1}
TOP = 3
AUTH_GLOBS = ("src/auth/**", "**/auth/**")
CONSENT_GLOBS = ("**/consent/**",)
LABEL_SHAPES = (
    ("deps", Shape.DEPS),
    ("tests", Shape.TESTS),
    ("docs", Shape.DOCS),
    ("fix", Shape.FIX),
    ("bug", Shape.FIX),
    ("refactor", Shape.REFACTOR),
)


def validate_policy(cfg: Config) -> None:
    pol = cfg.review
    for name, sp in pol.shapes.items():
        try:
            shape = Shape(name)
        except ValueError as e:
            raise ConfigError(f"review.yaml: unknown shape {name!r}") from e
        if shape in PERMANENT_RUNG_0 and (sp.rung, sp.max_rung) != (0, 0):
            raise ConfigError(f"review.yaml: {name} stays at rung 0 permanently")
        cap = MAX_RUNG.get(shape)
        if cap is not None and sp.max_rung > cap:
            raise ConfigError(f"review.yaml: {name} is capped at rung {cap}")
        if sp.rung > sp.max_rung:
            raise ConfigError(f"review.yaml: {name} starts above its max_rung")
    missing = [s.value for s in Shape if s.value not in pol.shapes]
    if missing:
        raise ConfigError(f"review.yaml: shapes without a rung: {missing}")
    for n in range(TOP + 1):
        if n not in pol.rungs:
            raise ConfigError(f"review.yaml: rung {n} is missing")
        for role in ("reviewer", "auditor"):
            preset = getattr(pol.rungs[n], role)
            if preset not in cfg.backends.presets:
                raise ConfigError(f"review.yaml: rung {n} {role} {preset} is not a preset")


def classify_shape(task: Task, cfg: Config) -> Shape:
    """A task's shape. Paths win over everything: a money, auth or consent
    path makes the shape, whatever the contract or labels say."""
    files = task.spec.files_in_scope
    if any(matches_any(f, list(AUTH_GLOBS)) for f in files):
        return Shape.AUTH
    if any(matches_any(f, list(CONSENT_GLOBS)) for f in files):
        return Shape.CONSENT
    if any(matches_any(f, cfg.routing.money_paths) for f in files):
        return Shape.MONEY
    if task.shape is not None:
        return task.shape
    labels = set(task.labels)
    for label, shape in LABEL_SHAPES:
        if label in labels:
            return shape
    return Shape.FEATURE


@dataclass(frozen=True)
class LadderChange:
    shape: Shape
    frm: int
    to: int
    reason: str
    sample_size: int = 0
    match_rate: float | None = None


class Ladder:
    def __init__(
        self, store: Store, cfg: Config, *, rng: Callable[[], float] = random.random
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.rng = rng

    # --- reading ----------------------------------------------------------------

    def _limit(self, shape: Shape) -> tuple[int, str]:
        """The highest rung the shape may hold, and why it stops there."""
        if shape in PERMANENT_RUNG_0:
            return 0, f"{shape.value} stays at rung 0 permanently"
        cap = min(self.cfg.review.shapes[shape.value].max_rung, MAX_RUNG.get(shape, TOP))
        if self.cfg.review.local_reviewer_eval is None and cap > 1:
            return 1, "no shape passes rung 1 before the local-reviewer eval"
        return cap, f"{shape.value} is capped at rung {cap}"

    def _cap(self, shape: Shape) -> int:
        return self._limit(shape)[0]

    def rung(self, shape: Shape, *, runner_file: bool = False) -> int:
        """The shape's live rung. A task that changes how tests run is rung 0
        whatever its shape's rung is (`runner_file`): the owner reads it, and
        its audit is never counted toward the shape's graduation."""
        if runner_file:
            return 0
        rows = self.store.q("SELECT rung FROM ladder WHERE shape=?", (shape.value,))
        start = rows[0]["rung"] if rows else self.cfg.review.shapes[shape.value].rung
        return max(0, min(int(start), self._cap(shape)))

    def presets(self, shape: Shape, *, runner_file: bool = False) -> tuple[str, str]:
        """(reviewer, auditor) for the shape's rung; a preset that isn't live
        yet (the tower's) falls back to routing.yaml's reviewer. Before the
        local-reviewer eval, the rung is at most 1: Sonnet and Opus."""
        r = self.cfg.review.rungs[self.rung(shape, runner_file=runner_file)]
        reviewer = r.reviewer if self.cfg.preset(r.reviewer).live else self.cfg.routing.reviewer
        auditor = r.auditor if self.cfg.preset(r.auditor).live else self.cfg.routing.auditor
        return reviewer, auditor

    def audit_rate(self, shape: Shape, verify_level: int, *, runner_file: bool = False) -> float:
        base = self.cfg.review.rungs[self.rung(shape, runner_file=runner_file)].audit
        if verify_level == 0:
            base *= self.cfg.review.level0_audit_factor
        return min(1.0, base)

    def should_audit(self, shape: Shape, verify_level: int, *, runner_file: bool = False) -> bool:
        rate = self.audit_rate(shape, verify_level, runner_file=runner_file)
        return rate >= 1.0 or self.rng() < rate

    # --- moving ---------------------------------------------------------------------

    def set_rung(
        self,
        shape: Shape,
        rung: int,
        *,
        reason: str,
        sample_size: int = 0,
        match_rate: float | None = None,
    ) -> LadderChange | None:
        """Move a shape. A move past its limit is refused and logged, never
        clamped silently; no move (the same rung) changes nothing."""
        frm = self.rung(shape)
        cap, why = self._limit(shape)
        if rung > cap:
            self.store.add_event(
                "ladder_refused",
                {"shape": shape.value, "from": frm, "to": rung, "reason": reason, "why": why},
                Provenance.HUMAN,
            )
            return None
        to = max(0, rung)
        if to == frm:
            return None
        return self._move(shape, frm, to, reason, sample_size, match_rate)

    def _move(
        self,
        shape: Shape,
        frm: int,
        to: int,
        reason: str,
        sample_size: int,
        match_rate: float | None,
    ) -> LadderChange:
        """The one place a rung changes: the rung, its ladder_changes row and
        the event commit together (owner ruling 1)."""
        now = iso()
        with self.store.tx() as c:
            last = c.execute("SELECT COALESCE(MAX(id), 0) FROM audits").fetchone()[0]
            c.execute(
                "INSERT INTO ladder (shape, rung, window_from, updated_at, reason)"
                " VALUES (?,?,?,?,?) ON CONFLICT(shape) DO UPDATE SET rung=excluded.rung,"
                " window_from=excluded.window_from, updated_at=excluded.updated_at,"
                " reason=excluded.reason",
                (shape.value, to, int(last), now, reason),
            )
            c.execute(
                "INSERT INTO ladder_changes (shape, old_rung, new_rung, reason, sample_size,"
                " match_rate, ts, provenance) VALUES (?,?,?,?,?,?,?,?)",
                (shape.value, frm, to, reason, sample_size, match_rate, now,
                 Provenance.HUMAN.value),
            )  # fmt: skip
            self.store.add_event(
                "ladder",
                {"shape": shape.value, "from": frm, "to": to, "reason": reason,
                 "sample_size": sample_size, "match_rate": match_rate},
                Provenance.HUMAN,
            )  # fmt: skip
        return LadderChange(shape, frm, to, reason, sample_size, match_rate)

    def _restart_window(self, shape: Shape, reason: str) -> None:
        """A missed finding restarts the promotion window; the rung stays."""
        last = self.store.q("SELECT COALESCE(MAX(id), 0) n FROM audits")[0]["n"]
        self.store.x(
            "INSERT INTO ladder (shape, rung, window_from, updated_at, reason) VALUES (?,?,?,?,?)"
            " ON CONFLICT(shape) DO UPDATE SET window_from=excluded.window_from,"
            " updated_at=excluded.updated_at, reason=excluded.reason",
            (shape.value, self.rung(shape), int(last), iso(), reason),
        )

    def last_change(self, shape: Shape) -> dict[str, Any] | None:
        rows = self.store.q(
            "SELECT shape, old_rung, new_rung, reason, sample_size, match_rate, ts"
            " FROM ladder_changes WHERE shape=? ORDER BY id DESC LIMIT 1",
            (shape.value,),
        )
        return dict(rows[0]) if rows else None

    def state(self) -> dict[str, Any]:
        """What the Review page reads: each shape's live rung, its start, its
        limit and its last change."""
        shapes = []
        for shape in Shape:
            cap, why = self._limit(shape)
            reviewer, auditor = self.presets(shape)
            shapes.append({
                "shape": shape.value, "rung": self.rung(shape),
                "start_rung": self.cfg.review.shapes[shape.value].rung,
                "max_rung": cap, "limit": why, "locked": shape in PERMANENT_RUNG_0,
                "reviewer": reviewer, "auditor": auditor,
                "audit_rate": self.audit_rate(shape, 1),
                "last_change": self.last_change(shape),
            })  # fmt: skip
        return {"shapes": shapes, "local_reviewer_eval": self.cfg.review.local_reviewer_eval}

    def record(
        self, audit: Audit, *, verify_level: int, runner_file: bool = False
    ) -> LadderChange | None:
        """Store one audit and move the shape if it now should move. A runner-file
        task's audit is not stored and moves nothing: it is rung 0 for good and
        never counts toward a shape's graduation (finding #2)."""
        if runner_file:
            return None
        bench = self.store.is_bench(job_id=audit.job_id)
        missed = audit.missed()
        high = sum(1 for f in missed if f.severity is Severity.HIGH)
        medium = sum(1 for f in missed if f.severity is Severity.MEDIUM)
        self.store.x(
            "INSERT INTO audits (job_id, task_id, shape, rung, verify_level, match_rate,"
            " missed_high, missed_medium, false_findings, ts, provenance)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                audit.job_id,
                audit.task_id,
                audit.shape.value,
                audit.rung,
                verify_level,
                audit.match_rate(),
                high,
                medium,
                len(audit.false_findings),
                iso(),
                # vigild's own tally of the auditor's output; a bench job's is eval-only
                self.store.row_provenance(Provenance.HUMAN, job_id=audit.job_id),
            ),
        )
        if bench:
            return None  # a bench run measures vigil; it moves no rung and no window
        shape, now = audit.shape, self.rung(audit.shape)
        rate = audit.match_rate()
        if high:
            to = 1 if now > 1 else 0
            reason = f"missed high finding ({audit.task_id})"
            if to == now:
                self._restart_window(shape, reason)
                return None
            return self._move(shape, now, to, reason, 1, rate)
        if medium and now >= 2:
            return self._move(shape, now, 1, f"missed medium finding ({audit.task_id})", 1, rate)
        if medium:
            # the window restarts: no promotion counts audits before a miss
            self._restart_window(shape, f"missed medium finding ({audit.task_id})")
            return None
        return self._maybe_promote(shape, now)

    def _maybe_promote(self, shape: Shape, now: int) -> LadderChange | None:
        pol = self.cfg.review.rungs[now]
        if now == 0 or pol.promote_after is None or now >= self._cap(shape):
            return None  # rung 0 never graduates; past rung 1 only after the eval
        rows = self.store.q(
            "SELECT * FROM audits WHERE shape=? AND provenance != 'eval-only' AND id > COALESCE("
            "(SELECT window_from FROM ladder WHERE shape=?), 0) ORDER BY id",
            (shape.value, shape.value),
        )
        weight = 0.0
        false = 0
        for r in rows:
            if r["match_rate"] < pol.min_match or r["missed_high"] or r["missed_medium"]:
                return None
            weight += self.cfg.review.verify_weight.get(int(r["verify_level"]), 1.0)
            false += int(r["false_findings"])
        if weight < pol.promote_after:
            return None
        if pol.max_false_per_pr is not None and rows and false / len(rows) > pol.max_false_per_pr:
            return None
        mean = sum(float(r["match_rate"]) for r in rows) / len(rows)
        return self.set_rung(
            shape, now + 1, reason=f"{len(rows)} audits ({weight:g} weighted) at rung {now}",
            sample_size=len(rows), match_rate=mean,
        )  # fmt: skip
