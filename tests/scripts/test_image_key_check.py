"""scripts/image-key-check.py: what the CI `image-key` job calls."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from vigild import config

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "image_key_check", ROOT / "scripts/image-key-check.py"
)
assert _spec and _spec.loader
ik = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ik)

REPO = "thomasbardhi01/vigil"
REPOS = {REPO: {"dev_suites": {"tests": "pytest"}, "dev_image_tools": ["uv"], "auto_merge": False}}


def _changed(files: list[str], head: dict | None = None) -> list[str]:
    cfg = config.load()
    return ik.changed_inputs(cfg, REPO, files, REPOS, head or REPOS)


def test_a_fake_diff_reports_each_changed_input() -> None:
    got = _changed(
        [
            "images/dev/vigil.Dockerfile",
            "images/dev/parkagent.Dockerfile",
            "uv.lock",
            "images/claude-executor/entrypoint.py",
            "configs/managed-settings.json",
            "schemas/worker_report.schema.json",
        ]
    )
    assert got == [
        "configs/managed-settings.json",
        "images/claude-executor/entrypoint.py",
        "images/dev/parkagent.Dockerfile",
        "images/dev/vigil.Dockerfile",
        "schemas/worker_report.schema.json",
        "uv.lock",
    ]


def test_a_diff_of_unrelated_files_reports_nothing() -> None:
    assert _changed(["README.md", "vigild/budget.py", "docs/runbook.md", "package.json"]) == []


def test_a_suite_or_tools_change_in_repos_yaml_is_an_input_but_not_other_fields() -> None:
    head = {REPO: {**REPOS[REPO], "dev_suites": {"tests": "pytest -n auto"}}}
    assert _changed(["configs/repos.yaml"], head) == [f"configs/repos.yaml: {REPO}: dev_suites"]
    head = {REPO: {**REPOS[REPO], "auto_merge": True}}
    assert _changed(["configs/repos.yaml"], head) == []


def test_the_pr_body_must_name_the_repo_or_layer() -> None:
    assert ik.body_names_rebuild("x\nRebuilds images: thomasbardhi01/vigil dev image\n")
    assert ik.body_names_rebuild("Rebuilds images: executor layer")
    assert not ik.body_names_rebuild("Rebuilds images:\n")
    assert not ik.body_names_rebuild("no line here\n<!-- Rebuilds images: x -->")
    assert not ik.body_names_rebuild("")
    assert not ik.body_names_rebuild(None)
