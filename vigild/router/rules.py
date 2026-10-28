"""The router: tier rules top to bottom, first match wins, the rule's name
goes to tier_reason. A tier resolves to a backend preset; vigild and the
scheduler only ever see the preset name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from vigild.config import Config, CoordRule
from vigild.contract.models import Size, Task
from vigild.intake.paths import globs_overlap, in_scope, matches, matches_any
from vigild.repos import RepoPolicyError, check_backend, live_private

LOCAL_OK = "vigil:local-ok"


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    tier: str
    tier_reason: str
    preset: str


def _rule(task: Task, cfg: Config) -> tuple[str, str]:
    r = cfg.routing
    spec = task.spec
    files = spec.files_in_scope
    labels = set(task.labels)
    if not (spec.test_cmd or "").strip() or not (spec.done_when or "").strip():
        return "spec", "rule1:no-test_cmd-or-done_when"
    money = [f for f in files if matches_any(f, r.money_paths)]
    if money:
        return "claude-opus", f"rule2:money-path:{money[0]}"
    if labels & set(r.architecture_labels):
        return "claude-opus", "rule3:architecture"
    if "feature" in labels and task.has_existing_tests is False:
        return "claude-sonnet", "rule4:new-feature-no-tests"
    if task.attempt >= r.escalate_attempt or task.review_rounds >= r.escalate_review_rounds:
        return (
            "claude-sonnet",
            f"rule5:escalated(attempt={task.attempt},rounds={task.review_rounds})",
        )
    local = sorted(labels & set(r.local_labels))
    if local and task.size in (Size.S, Size.M) and task.has_existing_tests:
        return "local", f"rule6:label:{local[0]}+size:{task.size.value}+tests-in-scope"
    if task.size is Size.XS and labels and labels <= set(r.xs_labels):
        return "local-fast", f"rule7:size:XS+{'+'.join(sorted(labels))}"
    if "review-followup" in labels and task.size is Size.S:
        return "local", "rule8:review-followup+size:S"
    return "claude-sonnet", "rule9:default"


def route(task: Task, cfg: Config, *, override_tier: str | None = None) -> Route:
    tier, reason = _rule(task, cfg)
    if tier == "claude-opus" and reason.startswith("rule2"):
        if override_tier and override_tier != "claude-opus":
            raise RoutingError(
                f"rule 2 is not overridable ({reason}); --tier {override_tier} refused"
            )
        if LOCAL_OK in task.labels:
            raise RoutingError(f"label {LOCAL_OK} is refused on a money path ({reason})")
    elif override_tier:
        if override_tier not in cfg.routing.tiers:
            raise RoutingError(f"unknown tier {override_tier}")
        reason = f"override:--tier {override_tier} (would have been {reason})"
        tier = override_tier

    preset, reason = _resolve(cfg, task.repo, cfg.routing.tiers[tier], reason)
    return Route(tier=tier, tier_reason=reason, preset=preset)


def _resolve(cfg: Config, repo: str, preset: str, reason: str) -> tuple[str, str]:
    """The preset a repo may use: a preset that isn't live, or isn't one this repo
    lists, falls back (the reason says so); then the private-repo rule."""
    pol = cfg.repo(repo)
    why = ""
    if not cfg.preset(preset).live:
        why = "not live"
    elif (
        cfg.preset(preset).locality == "local" and preset not in pol.backends + pol.worker_overflow
    ):
        why = "not listed"  # a repo that hasn't opted into the tower still routes
    if why:
        fallback = cfg.routing.fallback
        reason = f"{reason}; {preset} {why}, fallback {fallback}"
        preset = fallback
    try:
        check_backend(cfg, repo, preset, private=live_private(repo))
    except RepoPolicyError as e:
        raise RoutingError(str(e)) from e
    return preset, reason


@dataclass(frozen=True)
class CoordShape:
    """What is known about a job when its coord starts (there is no plan yet)."""

    labels: list[str] = field(default_factory=list)
    parent_job_id: str | None = None
    size: Size | None = None  # the request's size, if it carries one (part 5 adds an estimate)
    paths: list[str] = field(default_factory=list)  # scope hints: repo paths the request names
    attempt: int = 1  # 1 plus the replans so far


@dataclass(frozen=True)
class CoordRoute:
    preset: str
    reason: str  # `coord:rule-<name>`, recorded on the coord session


_TICKED = re.compile(r"`([^`\s]+)`")
_PATHISH = re.compile(r"(?<![\w./*-])((?:[\w.*-]+/)+[\w.*-]+|[\w-]+\.[a-z]{1,5})(?![\w/])")


def request_paths(intake: Mapping[str, Any]) -> list[str]:
    """Repo paths a request names: backticked or path-shaped tokens in its title,
    text and notes, then any explicit scope list it carries. Order kept, no repeats."""
    text = "\n".join(
        [str(intake.get("title") or ""), str(intake.get("request") or "")]
        + [str(n) for n in intake.get("notes") or []]
    )
    found = [m.strip(".,;:") for m in _TICKED.findall(text)] + _PATHISH.findall(text)
    for key in ("files_in_scope", "scope"):
        extra = intake.get(key)
        if isinstance(extra, list):
            found += [str(x) for x in extra]
    return list(dict.fromkeys(p.removeprefix("./") for p in found if p))


def _pinned_hit(cfg: Config, repo: str, paths: list[str]) -> str | None:
    """`path ~ glob` for the first hint that meets the repo's pinned paths (strict
    `in_scope`; a wildcard hint counts if it can name a pinned file) or a money path
    (guard's loose `matches`)."""
    pol = cfg.repos.repos.get(repo)
    pins = pol.ladder_pinned_paths if pol else []
    for p in paths:
        for g in pins:
            if in_scope(p, [g]) or (any(c in p for c in "*?[") and globs_overlap(p, g)):
                return f"{p} ~ {g}"
        for g in cfg.routing.money_paths:
            if matches(p, g):
                return f"{p} ~ {g}"
    return None


def _coord_rule(cfg: Config, repo: str, shape: CoordShape) -> tuple[CoordRule, str]:
    r = cfg.routing
    labels = set(shape.labels)
    is_fix = shape.parent_job_id is not None or "fix" in labels
    for rule in r.coord:
        note = ""
        match rule.when:
            case "escalated":
                hit = shape.attempt >= r.escalate_attempt
            case "architecture":
                hit = bool(labels & set(r.architecture_labels))
            case "pinned":
                where = _pinned_hit(cfg, repo, shape.paths)
                hit = where is not None
                note = f" ({where})"
            case "labels":
                hit = bool(labels & set(rule.labels))
            case "fix":
                hit = is_fix and shape.size is not None and shape.size in rule.sizes
            case _:
                hit = True
        if hit:
            return rule, f"coord:rule-{rule.name}{note}"
    raise RoutingError("routing.coord has no matching rule")  # unreachable: the last is `always`


def route_coord(cfg: Config, repo: str, shape: CoordShape) -> CoordRoute:
    """The coord's model by shape (ADR-0005 part 3): the first matching rule of
    `routing.coord`, then the same fallback and private-repo checks as `route()`."""
    rule, reason = _coord_rule(cfg, repo, shape)
    preset, reason = _resolve(cfg, repo, rule.preset, reason)
    return CoordRoute(preset=preset, reason=reason)
