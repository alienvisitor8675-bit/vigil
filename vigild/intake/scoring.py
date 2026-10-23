"""Intake scoring: no task runs until something has said why it is worth doing.

Rules (design, "Intake"):
- every task carries `why`, `value` (1-5) and `risk` (1-5);
- a self-generated task needs `value >= 3` and never touches a money, auth
  or consent path; one that fails either rule is filed as a proposal for
  the owner's weekly look, not queued.
The decider's `touches_money_path` is only a second check behind the path
rules, never instead of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from vigild.intake.paths import matches_any

SELF_MIN_VALUE = 3


class IntakeOutcome(StrEnum):
    ACCEPT = "accept"
    PROPOSE = "propose"  # filed as vigil:proposed; never dispatched
    REJECT = "reject"


@dataclass(frozen=True)
class IntakeResult:
    outcome: IntakeOutcome
    reasons: list[str] = field(default_factory=list)
    touches_money: bool = False


def _score_ok(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5


def score(
    req: Mapping[str, object],
    money_globs: Sequence[str],
    *,
    decider_touches_money: bool = False,
) -> IntakeResult:
    reasons: list[str] = []
    why = req.get("why")
    if not isinstance(why, str) or not why.strip():
        reasons.append("why is required: say why this is worth doing")
    for key in ("value", "risk"):
        if not _score_ok(req.get(key)):
            reasons.append(f"{key} is required and must be an integer 1-5")
    if reasons:
        return IntakeResult(IntakeOutcome.REJECT, reasons)

    files = req.get("files_in_scope") or []
    assert isinstance(files, list)
    money_files = [f for f in files if isinstance(f, str) and matches_any(f, money_globs)]
    touches = bool(money_files) or decider_touches_money

    if req.get("source", "human") == "self":
        value = req["value"]
        assert isinstance(value, int)
        if value < SELF_MIN_VALUE:
            reasons.append(f"self-generated task has value {value}; needs >= {SELF_MIN_VALUE}")
        if money_files:
            reasons.append(f"self-generated task touches a money/auth/consent path: {money_files}")
        elif decider_touches_money:
            reasons.append("self-generated task: decider says it touches a money path")
        if reasons:
            return IntakeResult(IntakeOutcome.PROPOSE, reasons, touches)
    return IntakeResult(IntakeOutcome.ACCEPT, [], touches)
