"""The private-repo rule, on each repo's live GitHub visibility (owner,
2026-10-23). Owner decision Q2 (2026-10-22): `anthropic` is allowed for
thomasbardhi01/vigil only; Jev on Cloudflare is the one hosted route any
private repo may use."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from vigild import config as config_mod
from vigild.config import CONFIG_DIR, Config, RepoPolicy
from vigild.repos import (
    RepoPolicyError,
    check_backend,
    check_decider_route,
    jev_route,
    live_private,
    repo_rule_errors,
    verify_visibility,
)


def _load_with(repos: dict[str, Any]) -> Config:
    return config_mod.load(overrides={"repos": {"repos": repos}})


def _policy(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decider": [],
        "backends": [],
        "dev_dockerfile": "images/dev/vigil.Dockerfile",
        "dev_lockfiles": ["uv.lock"],
        "dev_suites": {"t": "true"},
        "dev_image_tools": [],
    }
    base.update(kw)
    return base


def _errors(repo: str, **kw: Any) -> list[str]:
    cfg = _load_with({repo: _policy(**kw)})
    return repo_rule_errors(cfg, repo, private=True)


def test_shipped_repos_yaml_loads() -> None:
    cfg = config_mod.load()
    assert "visibility" not in RepoPolicy.model_fields  # GitHub's live answer is the source
    for repo in ("thomasbardhi01/vigil", "thomasbardhi01/parkagent"):
        assert cfg.repos.repos[repo].decider == ["haiku", "jev"]
    raw = yaml.safe_load((CONFIG_DIR / "repos.yaml").read_text())
    assert "claude-sonnet" in raw["repos"]["thomasbardhi01/vigil"]["backends"]
    assert all("visibility" not in r for r in raw["repos"].values())


def test_a_yaml_visibility_field_is_refused() -> None:
    with pytest.raises(Exception, match="extra"):
        _load_with({"o/x": _policy(visibility="public")})


def test_a_decider_list_names_jev_not_a_route() -> None:
    with pytest.raises(RepoPolicyError, match="list `jev`"):
        _load_with({"o/x": _policy(decider=["jev-openrouter"])})


def test_jev_resolves_by_visibility() -> None:
    assert jev_route(False) == "jev-openrouter"
    assert jev_route(True) == "jev-cloudflare"


def test_a_lookup_that_fails_is_private_and_never_openrouter() -> None:
    def broken(repo: str) -> bool:
        raise OSError("timeout")

    assert live_private("o/x", broken) is True
    assert live_private("o/x", lambda r: None) is True  # type: ignore[arg-type,return-value]
    assert live_private("o/x", lambda r: "public") is True  # type: ignore[arg-type,return-value]
    assert live_private("o/x", lambda r: False) is False
    assert jev_route(live_private("o/x", broken)) == "jev-cloudflare"


def test_vigil_may_list_anthropic_while_private() -> None:
    assert (
        _errors("thomasbardhi01/vigil", backends=["claude-sonnet"], decider=["haiku", "jev"]) == []
    )


def test_another_private_repo_may_not_list_anthropic() -> None:
    assert (
        "hosted backend claude-sonnet"
        in _errors("thomasbardhi01/hearth", backends=["claude-sonnet"])[0]
    )


def test_another_private_repo_may_not_use_haiku_as_decider() -> None:
    assert "decider route haiku" in _errors("thomasbardhi01/hearth", decider=["haiku"])[0]


def test_openrouter_is_never_allowed_for_a_private_repo_even_vigil() -> None:
    assert "openrouter" in _errors("thomasbardhi01/vigil", backends=["openrouter"])[0]
    cfg = _load_with({"thomasbardhi01/vigil": _policy(decider=["jev"])})
    with pytest.raises(RepoPolicyError):
        check_decider_route(cfg, "thomasbardhi01/vigil", "jev-openrouter", private=True)
    check_decider_route(cfg, "thomasbardhi01/vigil", "jev-cloudflare", private=True)
    check_decider_route(cfg, "thomasbardhi01/vigil", "jev-openrouter", private=False)


def test_overflow_is_checked_too() -> None:
    assert _errors("thomasbardhi01/hearth", worker_overflow=["claude-opus"])


def test_private_repo_may_list_jev_and_local() -> None:
    assert _errors("thomasbardhi01/hearth", decider=["lfm-tower", "jev"],
                   backends=["tower-qwen", "tower-fast"]) == []  # fmt: skip


def test_public_repo_may_list_any_hosted_entry() -> None:
    cfg = _load_with({"o/public": _policy(decider=["jev", "haiku"],
                                          backends=["openrouter", "claude-opus"])})  # fmt: skip
    assert repo_rule_errors(cfg, "o/public", private=False) == []


def test_the_exception_cannot_be_extended_from_yaml() -> None:
    with pytest.raises(Exception, match="extra"):
        _load_with({"thomasbardhi01/hearth": _policy(backends=["claude-sonnet"],
                                                     allow_hosted=["anthropic"])})  # fmt: skip


def test_check_backend_follows_visibility(cfg: Config) -> None:
    check_backend(cfg, "thomasbardhi01/parkagent", "claude-sonnet", private=False)
    with pytest.raises(RepoPolicyError):
        check_backend(cfg, "thomasbardhi01/parkagent", "openrouter", private=False)
    with pytest.raises(RepoPolicyError, match="private"):
        check_backend(cfg, "thomasbardhi01/parkagent", "claude-sonnet", private=True)


def test_start_up_checks_the_rule_on_live_visibility(cfg: Config) -> None:
    seen = verify_visibility(cfg, lambda r: False)
    assert seen == {r: False for r in cfg.repos.repos}
    # vigil private is fine (the exception); parkagent private breaks the rule
    verify_visibility(cfg, lambda r: r == "thomasbardhi01/vigil")
    with pytest.raises(RepoPolicyError, match="parkagent is private"):
        verify_visibility(cfg, lambda r: r == "thomasbardhi01/parkagent")
    with pytest.raises(RepoPolicyError):  # can't read it: private, fail closed
        verify_visibility(cfg, lambda r: (_ for _ in ()).throw(OSError("down")))  # type: ignore[arg-type,return-value]
