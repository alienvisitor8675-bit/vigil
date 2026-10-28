"""Intake: every task says why it is worth doing; self-generated tasks clear a higher bar."""

from __future__ import annotations

from vigild.intake.scoring import IntakeOutcome, score

MONEY = ["src/billing/**", "src/auth/**", "**/payments/**", "**/consent/**"]


def _req(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "why": "docs are stale",
        "value": 3,
        "risk": 2,
        "source": "human",
        "files_in_scope": ["docs/a.md"],
    }
    base.update(kw)
    return base


def test_human_task_with_why_value_risk_is_accepted() -> None:
    assert score(_req(), MONEY).outcome is IntakeOutcome.ACCEPT


def test_missing_why_is_rejected() -> None:
    r = score(_req(why=""), MONEY)
    assert r.outcome is IntakeOutcome.REJECT and "why" in r.reasons[0]


def test_missing_value_or_risk_is_rejected() -> None:
    assert score(_req(value=None), MONEY).outcome is IntakeOutcome.REJECT
    assert score(_req(risk=None), MONEY).outcome is IntakeOutcome.REJECT


def test_out_of_range_score_is_rejected() -> None:
    assert score(_req(value=7), MONEY).outcome is IntakeOutcome.REJECT


# Rule 1: self-generated tasks need value >= 3.
def test_self_generated_low_value_is_proposed_not_queued() -> None:
    r = score(_req(source="self", value=2), MONEY)
    assert r.outcome is IntakeOutcome.PROPOSE
    assert any("value" in x for x in r.reasons)


def test_self_generated_value_three_is_accepted() -> None:
    assert score(_req(source="self", value=3), MONEY).outcome is IntakeOutcome.ACCEPT


# Rule 2: self-generated tasks never touch a money, auth or consent path.
def test_self_generated_money_path_is_proposed_not_queued() -> None:
    r = score(_req(source="self", value=5, files_in_scope=["src/billing/fee.py"]), MONEY)
    assert r.outcome is IntakeOutcome.PROPOSE
    assert any("money" in x for x in r.reasons)


def test_decider_money_flag_is_a_second_check() -> None:
    r = score(_req(source="self", value=4), MONEY, decider_touches_money=True)
    assert r.outcome is IntakeOutcome.PROPOSE


def test_human_task_may_touch_money_paths() -> None:
    r = score(_req(files_in_scope=["src/billing/fee.py"]), MONEY)
    assert r.outcome is IntakeOutcome.ACCEPT and r.touches_money


def test_a_pinned_ladder_path_is_not_an_intake_refusal() -> None:
    """ladder_pinned_paths pin a PR at rung 0; intake accepts and scores as before."""
    r = score(_req(files_in_scope=["vigild/gateway/service.py"]), MONEY)
    assert r.outcome is IntakeOutcome.ACCEPT and not r.touches_money
