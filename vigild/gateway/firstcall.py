"""The first-call check: for every role, the worst case of its first model
call (a realistic packet in, the session output cap out) must fit the
reservation that role runs under. One function serves the CI test and the
preflight at job submission, so a model or price change fails CI and a job
that could not make its first call is refused with the numbers, before any
container starts."""

from __future__ import annotations

from dataclasses import dataclass

from vigild.config import Config
from vigild.gateway.service import worst_case_usd
from vigild.metering.prices import PriceTable

# A realistic first call, in characters of request body: the packet at its
# ceiling (DEFAULT_BUDGET_CHARS = 120,000 in vigild/packets/assemble.py) plus
# the CLI's system prompt and tool definitions. The live pa-0001 failure
# priced its first call at $3.0751; with the 128,000-token max_tokens Claude
# Code asked Opus for, that is about 193,000 characters in. 200,000 rounds up.
FIRST_CALL_CHARS = 200_000


@dataclass(frozen=True)
class FirstCall:
    role: str
    preset: str
    model: str
    worst_usd: float
    limit_usd: float


def first_calls(cfg: Config, repo: str | None = None) -> list[FirstCall]:
    """Each role's worst-case first call and the reservation it must fit. With
    `repo`, only the roles whose backend that repo may use. A local backend
    has no price and costs nothing here, so it is skipped."""
    prices = PriceTable(cfg.prices)
    cap = cfg.budgets.session_max_output_tokens
    allowed = set(cfg.repo(repo).backends) if repo else None
    rt = cfg.routing
    default = rt.coord[-1].preset
    roles: list[tuple[str, str, float]] = [
        ("coord" if p == default else f"coord:{p}", p, cfg.budgets.coord_session_usd)
        for p in dict.fromkeys(r.preset for r in rt.coord)
    ]
    roles += [(f"tier:{t}", p, cfg.budgets.per_task_usd) for t, p in rt.tiers.items()]
    roles += [
        ("fallback", rt.fallback, cfg.budgets.per_task_usd),
        # reviews sit outside the task's cap and an over-cap review grows against the
        # day (decision `budget-hard-stop`), so the day is the cap a review's first call must fit
        ("reviewer", rt.reviewer, cfg.budgets.daily_usd),
        ("auditor", rt.auditor, cfg.budgets.daily_usd),
        # the hidden-test author runs on the reviewer tier, under its own cap
        ("hidden_test_author", rt.reviewer, cfg.budgets.hidden_tests_session_usd),
    ]
    out: list[FirstCall] = []
    for role, preset, limit in roles:
        if allowed is not None and preset not in allowed:
            continue
        model = cfg.preset(preset).model
        if not model or not prices.has(model):
            continue
        out.append(
            FirstCall(
                role, preset, model, worst_case_usd(prices, model, FIRST_CALL_CHARS, cap), limit
            )
        )
    return out


def refusals(cfg: Config, repo: str | None = None) -> list[str]:
    """One line per role whose first call could not fit, with the numbers."""
    cap = cfg.budgets.session_max_output_tokens
    return [
        f"{r.role} ({r.model}): the worst-case first call is ${r.worst_usd:.4f} "
        f"({FIRST_CALL_CHARS:,} characters in, {cap:,} tokens out), over its "
        f"${r.limit_usd:.2f} reservation; raise it in configs/budgets.yaml or lower "
        "session_max_output_tokens"
        for r in first_calls(cfg, repo)
        if r.worst_usd > r.limit_usd
    ]
