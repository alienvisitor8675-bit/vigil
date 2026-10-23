"""The private-repo rule: vigild refuses to start on a hosted entry a private
repo may not use. Owner decision Q2 (2026-10-22): `anthropic` is allowed for
thomasbardhi01/vigil only; jev-cloudflare is allowed where listed."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from vigild import config as config_mod
from vigild.config import CONFIG_DIR, Config
from vigild.repos import RepoPolicyError, check_backend


def _load_with(repos: dict[str, Any]) -> Config:
    return config_mod.load(overrides={"repos": {"repos": repos}})


def _private(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "visibility": "private",
        "decider": [],
        "backends": [],
        "dev_dockerfile": "images/dev/vigil.Dockerfile",
        "dev_lockfiles": ["uv.lock"],
        "dev_suites": {"t": "true"},
        "dev_image_tools": [],
    }
    base.update(kw)
    return base


def test_shipped_repos_yaml_loads() -> None:
    cfg = config_mod.load()
    assert cfg.repos.repos["thomasbardhi01/vigil"].visibility == "private"
    raw = yaml.safe_load((CONFIG_DIR / "repos.yaml").read_text())
    assert "claude-sonnet" in raw["repos"]["thomasbardhi01/vigil"]["backends"]


def test_vigil_may_list_anthropic() -> None:
    _load_with({"thomasbardhi01/vigil": _private(backends=["claude-sonnet"], decider=["haiku"])})


def test_another_private_repo_may_not_list_anthropic() -> None:
    with pytest.raises(RepoPolicyError, match="hosted backend claude-sonnet"):
        _load_with({"thomasbardhi01/hearth": _private(backends=["claude-sonnet"])})


def test_another_private_repo_may_not_use_haiku_as_decider() -> None:
    with pytest.raises(RepoPolicyError, match="decider route haiku"):
        _load_with({"thomasbardhi01/hearth": _private(decider=["haiku"])})


def test_openrouter_is_never_allowed_for_a_private_repo_even_vigil() -> None:
    with pytest.raises(RepoPolicyError, match="openrouter"):
        _load_with({"thomasbardhi01/vigil": _private(backends=["openrouter"])})
    with pytest.raises(RepoPolicyError, match="jev-openrouter"):
        _load_with({"thomasbardhi01/vigil": _private(decider=["jev-openrouter"])})


def test_overflow_is_checked_too() -> None:
    with pytest.raises(RepoPolicyError):
        _load_with({"thomasbardhi01/hearth": _private(worker_overflow=["claude-opus"])})


def test_private_repo_may_list_jev_cloudflare_and_local() -> None:
    _load_with(
        {
            "thomasbardhi01/hearth": _private(
                decider=["lfm-tower", "jev-cloudflare"], backends=["tower-qwen", "tower-fast"]
            )
        }
    )


def test_public_repo_may_list_any_hosted_entry() -> None:
    _load_with(
        {
            "o/public": {
                "visibility": "public",
                "decider": ["jev-openrouter", "haiku"],
                "backends": ["openrouter", "claude-opus"],
                "dev_dockerfile": "images/dev/vigil.Dockerfile",
                "dev_lockfiles": ["uv.lock"],
                "dev_suites": {"t": "true"},
                "dev_image_tools": [],
            }
        }
    )


def test_the_exception_cannot_be_extended_from_yaml() -> None:
    # A YAML field that tried to grant an exception would be an unknown key.
    with pytest.raises(Exception, match="extra"):
        _load_with(
            {
                "thomasbardhi01/hearth": _private(
                    backends=["claude-sonnet"], allow_hosted=["anthropic"]
                )
            }
        )


def test_check_backend_at_submit_time(cfg: Config) -> None:
    check_backend(cfg, "thomasbardhi01/parkagent", "claude-sonnet")
    with pytest.raises(RepoPolicyError):
        check_backend(cfg, "thomasbardhi01/parkagent", "openrouter")
