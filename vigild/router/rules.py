"""The router: tier rules top to bottom, first match wins, the rule's name
goes to tier_reason. A tier resolves to a backend preset; vigild and the
scheduler only ever see the preset name.
"""

from __future__ import annotations

from dataclasses import dataclass

from vigild.config import Config
from vigild.contract.models import Size, Task
from vigild.intake.paths import matches_any
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

    preset = cfg.routing.tiers[tier]
    if not cfg.preset(preset).live:
        fallback = cfg.routing.fallback
        reason = f"{reason}; {preset} not live, fallback {fallback}"
        preset = fallback
    try:
        check_backend(cfg, task.repo, preset, private=live_private(task.repo))
    except RepoPolicyError as e:
        raise RoutingError(str(e)) from e
    return Route(tier=tier, tier_reason=reason, preset=preset)
