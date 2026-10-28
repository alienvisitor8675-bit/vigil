"""The coord's model by shape (ADR-0005 part 3): rules in routing.yaml, first match
wins, the rule's name recorded as `coord:rule-<name>`."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from vigild import config as config_mod
from vigild.config import Config, ConfigError, CoordRule, validate
from vigild.contract.models import Size
from vigild.router.rules import CoordShape, RoutingError, route_coord

VIGIL = "thomasbardhi01/vigil"
PA = "thomasbardhi01/parkagent"
OPUS, SONNET = "claude-opus", "claude-sonnet"


def _pick(cfg: Config, repo: str = VIGIL, **kw: Any) -> tuple[str, str]:
    r = route_coord(cfg, repo, CoordShape(**kw))
    return r.preset, r.reason


def test_the_default_is_opus(cfg: Config) -> None:
    assert _pick(cfg) == (OPUS, "coord:rule-default")
    assert _pick(cfg, labels=["feature"])[0] == OPUS


@pytest.mark.parametrize("label", ["architecture", "migration"])
def test_architecture_labels_pick_opus(cfg: Config, label: str) -> None:
    assert _pick(cfg, labels=[label]) == (OPUS, "coord:rule-architecture")


@pytest.mark.parametrize("label", ["tests", "docs"])
def test_tests_and_docs_pick_sonnet(cfg: Config, label: str) -> None:
    assert _pick(cfg, labels=[label]) == (SONNET, "coord:rule-tests-docs")


def test_a_fix_job_of_small_size_picks_sonnet(cfg: Config) -> None:
    for size in (Size.S, Size.M):
        assert _pick(cfg, parent_job_id="vi-0001", size=size) == (SONNET, "coord:rule-fix-small")
    assert _pick(cfg, labels=["fix"], size=Size.S) == (SONNET, "coord:rule-fix-small")


def test_a_large_fix_stays_opus(cfg: Config) -> None:
    assert _pick(cfg, labels=["fix"], size=Size.L) == (OPUS, "coord:rule-default")


def test_a_fix_with_no_size_gets_the_default_opus_rule(cfg: Config) -> None:
    assert _pick(cfg, parent_job_id="vi-0001") == (OPUS, "coord:rule-default")
    assert _pick(cfg, labels=["fix"]) == (OPUS, "coord:rule-default")


def test_attempt_three_beats_tests(cfg: Config) -> None:
    assert _pick(cfg, labels=["tests"], attempt=2)[0] == SONNET
    assert _pick(cfg, labels=["tests"], attempt=3) == (OPUS, "coord:rule-escalation")


def test_the_escalation_attempt_is_configurable(cfg: Config) -> None:
    cfg.routing.escalate_attempt = 2
    assert _pick(cfg, labels=["docs"], attempt=2) == (OPUS, "coord:rule-escalation")


def test_architecture_beats_docs(cfg: Config) -> None:
    assert _pick(cfg, labels=["docs", "architecture"]) == (OPUS, "coord:rule-architecture")


def test_a_pinned_path_in_the_request_picks_opus_even_with_label_tests(cfg: Config) -> None:
    assert _pick(cfg, labels=["tests"], paths=["vigild/gateway/service.py"]) == (
        OPUS,
        "coord:rule-pinned (vigild/gateway/service.py ~ vigild/gateway/**)",
    )


def test_a_money_path_hit_picks_opus(cfg: Config) -> None:
    preset, reason = _pick(cfg, labels=["docs"], paths=["src/billing/fee.py"])
    assert preset == OPUS
    assert reason == "coord:rule-pinned (src/billing/fee.py ~ src/billing/**)"


def test_a_near_miss_path_is_not_pinned(cfg: Config) -> None:
    assert _pick(cfg, labels=["docs"], paths=["vigild/gatewayx/a.py"]) == (
        SONNET,
        "coord:rule-tests-docs",
    )


def test_escalation_and_architecture_beat_pinned(cfg: Config) -> None:
    pinned = ["vigild/gateway/service.py"]
    assert _pick(cfg, paths=pinned, attempt=3)[1] == "coord:rule-escalation"
    assert _pick(cfg, paths=pinned, labels=["migration"])[1] == "coord:rule-architecture"


def test_pins_are_per_repo(cfg: Config) -> None:
    # ParkAgent pins nothing in repos.yaml for the gateway
    assert _pick(cfg, PA, labels=["docs"], paths=["vigild/gateway/service.py"])[1] == (
        "coord:rule-tests-docs"
    )


def test_tests_beats_fix(cfg: Config) -> None:
    assert _pick(cfg, labels=["fix", "tests"], size=Size.L)[1] == "coord:rule-tests-docs"


def _private_cfg(repo: str, backends: list[str]) -> Config:
    pol: dict[str, Any] = {
        "decider": [],
        "backends": backends,
        "dev_dockerfile": "images/dev/vigil.Dockerfile",
        "dev_lockfiles": ["uv.lock"],
        "dev_suites": {"t": "true"},
        "dev_image_tools": [],
    }
    return config_mod.load(overrides={"repos": {"repos": {repo: pol}}})


def test_a_private_repo_never_gets_a_hosted_coord_it_may_not_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vigild.router.rules.live_private", lambda repo: True)
    monkeypatch.setattr("vigild.repos.live_private", lambda repo: True)
    cfg = _private_cfg(PA, ["claude-opus", "claude-sonnet"])  # listed, but hosted and private
    for shape in (CoordShape(), CoordShape(labels=["docs"])):
        with pytest.raises(RoutingError, match="private"):
            route_coord(cfg, PA, shape)


def test_a_private_repo_never_gets_a_coord_backend_it_does_not_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vigild.router.rules.live_private", lambda repo: True)
    cfg = _private_cfg(VIGIL, ["claude-opus"])  # vigil may use anthropic; lists Opus only
    assert route_coord(cfg, VIGIL, CoordShape()).preset == OPUS
    with pytest.raises(RoutingError, match="does not list backend claude-sonnet"):
        route_coord(cfg, VIGIL, CoordShape(labels=["docs"]))


def test_a_preset_that_is_not_live_falls_back_with_the_reason(cfg: Config) -> None:
    cfg.backends.presets[SONNET].live = False
    cfg.routing.fallback = OPUS
    r = route_coord(cfg, VIGIL, CoordShape(labels=["docs"]))
    assert r.preset == OPUS
    assert r.reason.startswith(
        "coord:rule-tests-docs; claude-sonnet not live, fallback claude-opus"
    )


def _rules(cfg: Config) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in cfg.routing.coord]


def _with(rules: list[dict[str, Any]]) -> Config:
    return config_mod.load(overrides={"routing": {**_routing_raw(), "coord": rules}})


def _routing_raw() -> dict[str, Any]:
    import yaml

    raw: dict[str, Any] = yaml.safe_load((config_mod.CONFIG_DIR / "routing.yaml").read_text())
    return raw


def test_the_shipped_rules_load_in_the_owners_order(cfg: Config) -> None:
    assert [r.name for r in cfg.routing.coord] == [
        "escalation",
        "architecture",
        "pinned",
        "tests-docs",
        "fix-small",
        "default",
    ]


def test_an_unknown_preset_in_a_rule_fails_at_load(cfg: Config) -> None:
    rules = _rules(cfg)
    rules[3]["preset"] = "claude-nonesuch"
    with pytest.raises(ConfigError, match=r"tests-docs.*unknown preset"):
        validate(
            Config.model_validate(
                {
                    **cfg.model_dump(mode="json"),
                    "routing": {**cfg.routing.model_dump(mode="json"), "coord": rules},
                }
            )
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "x", "when": "sometimes", "preset": SONNET},  # unknown condition
        {"name": "x", "when": "labels", "preset": SONNET},  # labels needs labels
        {"name": "x", "when": "fix", "preset": SONNET},  # fix needs sizes
        {"name": "x", "when": "always", "preset": SONNET, "labels": ["a"]},
        {"name": "x", "when": "always", "preset": SONNET, "colour": "red"},  # extra key
        {"name": "X Y", "when": "always", "preset": SONNET},
    ],
)
def test_a_malformed_rule_fails_at_load(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CoordRule.model_validate(bad)


def test_the_last_rule_must_be_the_only_always(cfg: Config) -> None:
    rules = _rules(cfg)
    with pytest.raises(ValidationError, match="last rule"):
        _with(rules[:-1])  # no default
    with pytest.raises(ValidationError, match="last rule"):
        _with([{**rules[-1], "name": "first"}, *rules])  # an always first
    with pytest.raises(ValidationError, match="duplicate"):
        _with([rules[0], rules[0], rules[-1]])


def test_a_single_preset_coord_is_refused_at_load() -> None:
    with pytest.raises(ValidationError):
        config_mod.load(overrides={"routing": {**_routing_raw(), "coord": "claude-opus"}})
