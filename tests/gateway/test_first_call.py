"""The first-call check: every role's worst-case first call fits its cap.
One function (vigild/gateway/firstcall.py) serves this CI test and the
preflight at job submission, so a model or price change fails CI and a bad
submission is refused with the numbers before any container starts."""

from __future__ import annotations

import pytest

from vigild.config import Config
from vigild.gateway.firstcall import FIRST_CALL_CHARS, first_calls, refusals

REPO = "thomasbardhi01/parkagent"


def test_every_roles_worst_case_first_call_fits_its_cap(cfg: Config) -> None:
    rows = first_calls(cfg)
    roles = {r.role for r in rows}
    assert {"coord", "fallback", "reviewer", "auditor"} <= roles
    assert any(r.role.startswith("tier:") for r in rows)
    for r in rows:
        assert r.worst_usd <= r.limit_usd, (
            f"{r.role} on {r.model}: worst-case first call ${r.worst_usd:.4f} > ${r.limit_usd:.2f}"
        )


def test_the_opus_coords_first_call_fits_its_two_dollar_reservation(cfg: Config) -> None:
    (coord,) = [r for r in first_calls(cfg) if r.role == "coord"]
    assert coord.model == "claude-opus-5-5"
    # 200,000 chars / 3 = 66,667 tokens at $8/M, plus 16,384 output tokens at $20/M
    assert coord.worst_usd == pytest.approx(66_667 * 8e-6 + 16_384 * 20e-6, rel=1e-3)
    assert coord.limit_usd == cfg.budgets.coord_session_usd == 2.00
    assert coord.worst_usd < 0.90


def test_an_opus_workers_first_call_fits_the_task_cap(cfg: Config) -> None:
    (w,) = [r for r in first_calls(cfg) if r.role == "tier:claude-opus"]
    assert w.limit_usd == cfg.budgets.per_task_usd
    assert w.worst_usd < w.limit_usd


def test_a_cap_that_no_longer_fits_is_refused_with_the_numbers(cfg: Config) -> None:
    assert refusals(cfg, REPO) == []
    cfg.budgets.coord_session_usd = 0.50
    (msg,) = refusals(cfg, REPO)
    assert "coord" in msg and "claude-opus-5-5" in msg and "$0.50" in msg
    assert f"{FIRST_CALL_CHARS:,}" in msg


def test_a_role_the_repo_may_not_use_is_not_checked(cfg: Config) -> None:
    cfg.budgets.coord_session_usd = 0.50
    cfg.repo(REPO).backends = ["claude-sonnet"]  # no Opus here: the coord can't run anyway
    assert all("coord" not in m for m in refusals(cfg, REPO))


def test_the_coord_keeps_headroom_for_a_long_session(cfg: Config) -> None:
    """Why $2: at $1 a coord is refused mid-session once its request reaches
    about 230-270k characters, a few file reads past its ~200k start. At $2,
    with ~$0.30 already spent, the gateway can still give it the 4,096-token
    floor until the request is about 600k characters."""
    from vigild.gateway.service import MIN_OUTPUT_TOKENS, worst_case_usd
    from vigild.metering.prices import PriceTable

    prices = PriceTable(cfg.prices)
    left = cfg.budgets.coord_session_usd - 0.30
    assert worst_case_usd(prices, "claude-opus-5-5", 590_000, MIN_OUTPUT_TOKENS) < left
    assert worst_case_usd(prices, "claude-opus-5-5", 620_000, MIN_OUTPUT_TOKENS) > left


def test_the_hidden_test_authors_first_call_is_checked_under_its_own_cap(cfg: Config) -> None:
    (a,) = [r for r in first_calls(cfg) if r.role == "hidden_test_author"]
    assert a.model == "claude-sonnet-5-5"  # the reviewer tier
    assert a.limit_usd == cfg.budgets.hidden_tests_session_usd == 1.00
    assert a.worst_usd <= a.limit_usd


def test_a_hidden_tests_cap_that_no_longer_fits_is_refused_at_submission(cfg: Config) -> None:
    cfg.budgets.hidden_tests_session_usd = 0.20
    (msg,) = refusals(cfg, REPO)
    assert "hidden_test_author" in msg and "$0.20" in msg


def test_reviews_are_checked_against_the_day_not_the_task_cap(cfg: Config) -> None:
    # 2a: reviews sit outside the task's cap and an over-cap review grows against the
    # day and the month, so the day is the only cap a review's first call must fit
    cfg.budgets.per_task_usd = 0.30
    roles = {r.role: r for r in first_calls(cfg)}
    for role in ("reviewer", "auditor"):
        assert roles[role].limit_usd == cfg.budgets.daily_usd
    assert not [m for m in refusals(cfg, REPO) if m.startswith(("reviewer", "auditor"))]


def test_a_review_that_could_never_fit_a_day_is_refused_at_submission(cfg: Config) -> None:
    cfg.budgets.daily_usd = 0.50
    msgs = refusals(cfg, REPO)
    assert any(m.startswith("auditor") and "$0.50" in m for m in msgs)
