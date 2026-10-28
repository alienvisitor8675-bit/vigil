"""Fixtures for the review replay tests (#47)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from vigild.packets.assemble import PROMPTS_DIR

OLD, NEW = "# Role: reviewer\nOLD-PROMPT-MARKER\n", "# Role: reviewer\nNEW-PROMPT-MARKER\n"


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    p = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@vigil.invalid", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout.strip()


@pytest.fixture
def prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A tiny git repo standing in for this repo: `old` and `new` tags, and `prod`, a
    commit whose reviewer prompt is the one vigil ships today. Maps each tag to the
    sha256 of its prompt."""
    root = tmp_path / "vigil-repo"
    (root / "prompts").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    out: dict[str, str] = {}
    shipped = (PROMPTS_DIR / "reviewer.md").read_text()
    for tag, text in (("old", OLD), ("new", NEW), ("prod", shipped)):
        (root / "prompts" / "reviewer.md").write_text(text)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", tag)
        _git(root, "tag", tag)
        out[tag] = hashlib.sha256(text.encode()).hexdigest()
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")  # what has shipped
    monkeypatch.setattr("vigild.review.replay.REPO_ROOT", root)
    return out
