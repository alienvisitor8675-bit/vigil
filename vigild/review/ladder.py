"""The trust ladder (design "Review tiers and the trust ladder"; plan 2a,
item 1).

- **Shapes** start where `configs/review.yaml` says. vigild keeps each
  shape's live rung in its `ladder` table and moves it only from audits, with
  a `ladder` event for every move (the runner pages the owner).
- **Who reviews and how often audits run** come from the rung: rung 0 and 1
  audit every review; above that a share, sampled twice as hard when the
  task's criteria sit at verify level 0.
- **Promotion** needs `promote_after` PRs since the last move, each counted by
  its verify level's weight (a level-0 audit counts half), every one of them
  with a match rate at or above `min_match`, no missed medium or high finding,
  and at rung 1 no more than `max_false_per_pr` wrong reviewer findings.
- **Demotion:** a missed high finding drops a shape to rung 1 (or 0 from rung
  1); a missed medium finding at rung 2 or 3 drops it to rung 1. A missed
  finding also restarts the promotion window.
- **Hard limits, not configurable:** money, auth and consent stay at rung 0;
  deps never passes rung 1 (owner, 2026-10-23). `validate_policy` refuses a
  config that says otherwise, and `set_rung` clamps.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

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


class Ladder:
    def __init__(
        self, store: Store, cfg: Config, *, rng: Callable[[], float] = random.random
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.rng = rng

    # --- reading ----------------------------------------------------------------

    def _cap(self, shape: Shape) -> int:
        if shape in PERMANENT_RUNG_0:
            return 0
        cap = self.cfg.review.shapes[shape.value].max_rung
        return min(cap, MAX_RUNG.get(shape, TOP))

    def rung(self, shape: Shape) -> int:
        rows = self.store.q("SELECT rung FROM ladder WHERE shape=?", (shape.value,))
        start = rows[0]["rung"] if rows else self.cfg.review.shapes[shape.value].rung
        return max(0, min(int(start), self._cap(shape)))

    def presets(self, shape: Shape) -> tuple[str, str]:
        """(reviewer, auditor) for the shape's rung; a preset that isn't live
        yet (the tower's) falls back to routing.yaml's reviewer."""
        r = self.cfg.review.rungs[self.rung(shape)]
        reviewer = r.reviewer if self.cfg.preset(r.reviewer).live else self.cfg.routing.reviewer
        auditor = r.auditor if self.cfg.preset(r.auditor).live else self.cfg.routing.auditor
        return reviewer, auditor

    def audit_rate(self, shape: Shape, verify_level: int) -> float:
        base = self.cfg.review.rungs[self.rung(shape)].audit
        if verify_level == 0:
            base *= self.cfg.review.level0_audit_factor
        return min(1.0, base)

    def should_audit(self, shape: Shape, verify_level: int) -> bool:
        rate = self.audit_rate(shape, verify_level)
        return rate >= 1.0 or self.rng() < rate

    # --- moving ---------------------------------------------------------------------

    def set_rung(self, shape: Shape, rung: int, *, reason: str) -> LadderChange | None:
        frm = self.rung(shape)
        to = max(0, min(rung, self._cap(shape)))
        last = self.store.q("SELECT COALESCE(MAX(id), 0) n FROM audits")[0]["n"]
        self.store.x(
            "INSERT INTO ladder (shape, rung, window_from, updated_at, reason) VALUES (?,?,?,?,?)"
            " ON CONFLICT(shape) DO UPDATE SET rung=excluded.rung,"
            " window_from=excluded.window_from, updated_at=excluded.updated_at,"
            " reason=excluded.reason",
            (shape.value, to, int(last), iso(), reason),
        )
        if to == frm:
            return None
        self.store.add_event(
            "ladder",
            {"shape": shape.value, "from": frm, "to": to, "reason": reason},
            Provenance.HUMAN,
        )
        return LadderChange(shape, frm, to, reason)

    def record(self, audit: Audit, *, verify_level: int) -> LadderChange | None:
        """Store one audit and move the shape if it now should move."""
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
                Provenance.HUMAN.value,  # vigild's own tally of the auditor's output
            ),
        )
        shape, now = audit.shape, self.rung(audit.shape)
        if high:
            return self.set_rung(
                shape, 1 if now > 1 else 0, reason=f"missed high finding ({audit.task_id})"
            )
        if medium and now >= 2:
            return self.set_rung(shape, 1, reason=f"missed medium finding ({audit.task_id})")
        if medium:
            # the window restarts: no promotion counts audits before a miss
            return self.set_rung(shape, now, reason=f"missed medium finding ({audit.task_id})")
        return self._maybe_promote(shape, now)

    def _maybe_promote(self, shape: Shape, now: int) -> LadderChange | None:
        pol = self.cfg.review.rungs[now]
        if pol.promote_after is None or now >= self._cap(shape):
            return None
        rows = self.store.q(
            "SELECT * FROM audits WHERE shape=? AND id > COALESCE("
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
        return self.set_rung(
            shape, now + 1, reason=f"{len(rows)} audits ({weight:g} weighted) at rung {now}"
        )
