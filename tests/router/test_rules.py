"""Routing rules 1-9: first match wins, the rule's name goes to tier_reason."""

from __future__ import annotations

from typing import Any

import pytest

from vigild.config import Config
from vigild.contract.models import Task
from vigild.router.rules import RoutingError, route

PA = "thomasbardhi01/parkagent"


def _t(**kw: Any) -> Task:
    spec = {
        "goal": "g",
        "test_cmd": "pytest -q",
        "done_when": "green",
        "files_in_scope": ["docs/a.md"],
        **kw.pop("spec", {}),
    }
    base: dict[str, Any] = {
        "task_id": "t",
        "repo": PA,
        "why": "w",
        "value": 3,
        "risk": 2,
        "spec": spec,
    }
    base.update(kw)
    return Task.model_validate(base)


def test_rule1_no_spec_goes_to_spec_writing(cfg: Config) -> None:
    r = route(_t(spec={"test_cmd": None}), cfg)
    assert (r.tier, r.preset) == ("spec", "claude-sonnet")
    assert r.tier_reason.startswith("rule1")


def test_rule2_money_path_goes_to_opus(cfg: Config) -> None:
    r = route(_t(spec={"files_in_scope": ["server/src/billing/fee.ts"]}), cfg)
    assert (r.tier, r.preset) == ("claude-opus", "claude-opus")
    assert "rule2" in r.tier_reason and "src/billing/fee.ts" in r.tier_reason


def test_rule2_beats_labels_and_size(cfg: Config) -> None:
    t = _t(labels=["docs"], size="XS", spec={"files_in_scope": ["a/payments/x.py"]})
    assert route(t, cfg).tier == "claude-opus"


def test_rule2_is_not_overridable(cfg: Config) -> None:
    t = _t(spec={"files_in_scope": ["src/auth/login.py"]})
    with pytest.raises(RoutingError, match="rule 2"):
        route(t, cfg, override_tier="local")
    with pytest.raises(RoutingError, match="local-ok"):
        route(_t(labels=["vigil:local-ok"], spec={"files_in_scope": ["src/auth/x.py"]}), cfg)


def test_rule3_fires_on_architecture_labels_only(cfg: Config) -> None:
    for label in ("architecture", "migration"):
        r = route(_t(labels=[label]), cfg)
        assert (r.tier, r.tier_reason) == ("claude-opus", "rule3:architecture")


def test_rule3_ignores_architecture_md_in_scope(cfg: Config) -> None:
    r = route(_t(spec={"files_in_scope": ["docs/architecture.md"]}), cfg)
    assert (r.tier, r.preset, r.tier_reason) == ("claude-sonnet", "claude-sonnet", "rule9:default")
    assert not hasattr(cfg.routing, "architecture_paths")


def test_rule2_beats_rule3(cfg: Config) -> None:
    t = _t(labels=["architecture"], spec={"files_in_scope": ["src/auth/x.py"]})
    assert route(t, cfg).tier_reason.startswith("rule2")


def test_a_vigil_job_touching_architecture_md_does_not_route_by_rule3(cfg: Config) -> None:
    """vi-0001's shape: gateway, metering, db and docs, labelled needs-review."""
    t = _t(
        repo="thomasbardhi01/vigil",
        labels=["needs-review"],
        spec={
            "files_in_scope": [
                "vigild/gateway/service.py",
                "vigild/metering/meter.py",
                "vigild/db.py",
                "docs/architecture.md",
            ]
        },
    )
    assert route(t, cfg).tier_reason == "rule9:default"


def test_rule4_new_feature_without_tests(cfg: Config) -> None:
    r = route(_t(labels=["feature"], has_existing_tests=False), cfg)
    assert (r.tier, r.tier_reason[:5]) == ("claude-sonnet", "rule4")


def test_rule5_escalation(cfg: Config) -> None:
    assert route(_t(attempt=3), cfg).tier_reason.startswith("rule5")
    assert route(_t(review_rounds=2), cfg).tier_reason.startswith("rule5")


def test_rule6_local_shapes_fall_back_while_the_tower_is_not_live(cfg: Config) -> None:
    cfg.backends.presets["tower-qwen"].live = False
    r = route(_t(labels=["tests"], size="S", has_existing_tests=True), cfg)
    assert r.tier == "local" and r.tier_reason.startswith("rule6")
    assert r.preset == "claude-sonnet" and "tower-qwen not live" in r.tier_reason


def test_rule6_routes_to_the_tower_with_no_fallback(cfg: Config) -> None:
    cfg.repos.repos[PA].backends.append("tower-qwen")
    r = route(_t(labels=["tests"], size="M", has_existing_tests=True), cfg)
    assert r.preset == "tower-qwen"
    assert "not live" not in r.tier_reason and "fallback" not in r.tier_reason


def test_rule6_falls_back_when_the_repo_does_not_list_the_tower(cfg: Config) -> None:
    r = route(_t(labels=["tests"], size="S", has_existing_tests=True), cfg)
    assert r.preset == "claude-sonnet" and "tower-qwen not listed" in r.tier_reason


def test_vigil_lists_the_tower_and_its_rule6_tasks_route_there(cfg: Config) -> None:
    """Owner, 2026-10-28: vv-26 is the first tower-routed job, on vigil. Only
    vigil opts in here; ParkAgent's backends are the owner's to change."""
    assert "tower-qwen" in cfg.repos.repos["thomasbardhi01/vigil"].backends
    assert "tower-qwen" not in cfg.repos.repos[PA].backends
    t = _t(repo="thomasbardhi01/vigil", labels=["tests"], size="S", has_existing_tests=True)
    r = route(t, cfg)
    assert (r.preset, r.tier_reason.startswith("rule6")) == ("tower-qwen", True)
    assert "fallback" not in r.tier_reason


def test_rule7_xs_docs(cfg: Config) -> None:
    r = route(_t(labels=["docs"], size="XS"), cfg)
    assert r.tier == "local-fast" and r.tier_reason.startswith("rule7")


def test_rule8_review_follow_up(cfg: Config) -> None:
    r = route(_t(labels=["review-followup"], size="S"), cfg)
    assert r.tier == "local" and r.tier_reason.startswith("rule8")


def test_rule9_anything_else(cfg: Config) -> None:
    r = route(_t(), cfg)
    assert (r.tier, r.preset, r.tier_reason) == ("claude-sonnet", "claude-sonnet", "rule9:default")


def test_override_tier(cfg: Config) -> None:
    r = route(_t(labels=["docs"], size="XS"), cfg, override_tier="claude-opus")
    assert r.tier == "claude-opus" and "override" in r.tier_reason


def test_backend_not_listed_for_repo_is_refused(cfg: Config) -> None:
    cfg.repos.repos[PA].backends = ["claude-sonnet"]
    with pytest.raises(RoutingError, match="does not list"):
        route(_t(spec={"files_in_scope": ["src/billing/x.py"]}), cfg)
