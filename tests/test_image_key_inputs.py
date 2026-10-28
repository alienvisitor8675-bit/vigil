"""The list of files and config fields a dev-image key or the executor layer
digest hashes (`key_input_paths`) is the one the CI `image-key` job uses; it
must match what `DevImages.key` and `layer_digest` really hash."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from vigild import devimage
from vigild.config import Config, RepoPolicy
from vigild.devimage import DevImages, key_input_paths, layer_digest

REPO = "thomasbardhi01/vigil"


class RootSources:
    """blob ids read from the tmp tree: vigil's lockfiles are in the checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def blob_ids(
        self, repo: str, sha: str, paths: list[str], source: str = "mirror"
    ) -> dict[str, str | None]:
        return {
            p: hashlib.sha256((self.root / p).read_bytes()).hexdigest()
            if (self.root / p).is_file()
            else None
            for p in paths
        }


@pytest.fixture
def tree(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path]:
    files = {
        "images/dev/vigil.Dockerfile": "FROM x\n",
        "images/dev/other.Dockerfile": "FROM y\n",
        "images/claude-executor/Dockerfile": "FROM z\n",
        "images/claude-executor/cli/package-lock.json": "{}\n",
        "images/claude-executor/__pycache__/x.pyc": "junk",
        "configs/managed-settings.json": "{}\n",
        "schemas/worker_report.schema.json": "{}\n",
        "pyproject.toml": "[project]\n",
        "uv.lock": "version = 1\n",
        "README.md": "readme\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    monkeypatch.setattr(devimage, "ROOT", tmp_path)
    monkeypatch.setattr(devimage, "EXECUTOR_LAYER", tmp_path / "images/claude-executor")
    monkeypatch.setattr(devimage, "MANAGED_SETTINGS", tmp_path / "configs/managed-settings.json")
    monkeypatch.setattr(devimage, "REPORT_SCHEMA", tmp_path / "schemas/worker_report.schema.json")
    cfg.repos.repos = {
        REPO: RepoPolicy(
            decider=["haiku"],
            backends=["claude-haiku"],
            dev_dockerfile="images/dev/vigil.Dockerfile",
            dev_lockfiles=["pyproject.toml", "uv.lock"],
            dev_suites={"tests": "pytest"},
            dev_image_tools=["pytest"],
        )
    }
    return cfg, tmp_path


def _hashes(cfg: Config, root: Path) -> tuple[str, str]:
    dev = DevImages(cfg, None, None, RootSources(root), None)  # type: ignore[arg-type]
    return dev.key(REPO, "sha"), layer_digest()


def test_changing_each_listed_file_changes_the_key_or_the_layer_digest(
    tree: tuple[Config, Path],
) -> None:
    cfg, root = tree
    listed = key_input_paths(cfg, REPO)
    assert "images/dev/vigil.Dockerfile" in listed and "uv.lock" in listed
    assert "images/claude-executor/cli/package-lock.json" in listed
    assert "configs/managed-settings.json" in listed
    assert "schemas/worker_report.schema.json" in listed
    for rel in listed:
        before = _hashes(cfg, root)
        p = root / rel
        old = p.read_text()
        p.write_text(old + "# changed\n")
        assert _hashes(cfg, root) != before, rel
        p.write_text(old)
        assert _hashes(cfg, root) == before


def test_changing_an_unlisted_file_changes_neither(tree: tuple[Config, Path]) -> None:
    cfg, root = tree
    listed = set(key_input_paths(cfg, REPO))
    unlisted = [
        "README.md",
        "images/dev/other.Dockerfile",
        "images/claude-executor/__pycache__/x.pyc",
    ]
    assert not listed & set(unlisted)
    before = _hashes(cfg, root)
    for rel in unlisted:
        (root / rel).write_text("different\n")
    assert _hashes(cfg, root) == before


def test_another_repos_lockfiles_are_not_inputs_from_this_checkout(
    tree: tuple[Config, Path],
) -> None:
    cfg, _ = tree
    cfg.repos.repos["o/pa"] = cfg.repos.repos[REPO].model_copy(
        update={"dev_dockerfile": "images/dev/other.Dockerfile", "dev_lockfiles": ["package.json"]}
    )
    paths = key_input_paths(cfg, REPO)
    assert "images/dev/other.Dockerfile" in paths  # every repo's Dockerfile is in this repo
    assert "package.json" not in paths  # its lockfiles are in its own repo


def test_config_input_changes_name_the_repo_and_field() -> None:
    base: dict[str, Any] = {
        "a": {"dev_suites": {"t": "x"}, "dev_image_tools": ["x"], "auto_merge": False}
    }
    head: dict[str, Any] = {
        "a": {"dev_suites": {"t": "y"}, "dev_image_tools": ["x"], "auto_merge": True}
    }
    assert devimage.config_input_changes(base, head) == ["a: dev_suites"]
    assert devimage.config_input_changes(base, base) == []
    assert devimage.config_input_changes({}, base) == ["a: new repo"]
