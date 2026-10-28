"""scripts/check-all.sh checks the committed HEAD in a clean worktree."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-all.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "ok.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CHECK_ALL_STEPS": "ruff-check",
        "CHECK_ALL_UV": f"uv run --project {ROOT}",
    }
    return subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)


def test_refuses_dirty_tracked_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "ok.py").write_text("x = 2\n")
    r = _run(repo)
    assert r.returncode == 2
    assert "ok.py" in r.stdout + r.stderr


def test_checks_committed_head_not_working_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "ok.py").write_text("import os\n")  # F401
    _git(repo, "commit", "-qam", "lint error")
    (repo / "ok.py").write_text("x = 1\n")  # fixed only in the working tree
    _git(repo, "update-index", "--skip-worktree", "ok.py")  # status looks clean
    r = _run(repo)
    assert r.returncode == 1
    assert "FAIL" in r.stdout


def test_clean_head_passes_and_worktree_removed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout
    assert len(out.strip().splitlines()) == 1
