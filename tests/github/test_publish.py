"""vigild publishes from the host (P2), from its vault: checks, push, draft PR.
Workers hold no token. Rename, base-ref and hostile-.git cases are in
tests/test_vault.py."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git
from tests.harness import finish_session
from vigild.github.publish import PublishError, publish
from vigild.workspace import Workspace

REPO, TID, BRANCH = "o/fixture", "fx-0001-w1", "vigil/fx-0001-w1-doc"


@dataclass
class FakePulls:
    bare: Path
    prs: list[dict[str, Any]] = field(default_factory=list)
    refuse_draft: bool = False

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> str:
        if self.refuse_draft:
            raise PublishError("422: Draft pull requests are not supported in this repository.")
        git(self.bare, "rev-parse", "--verify", f"refs/heads/{head}")  # GitHub checks too
        self.prs.append({"head": head, "base": base, "draft": draft})
        return f"https://github.com/{repo}/pull/{len(self.prs)}"


@pytest.fixture
def setup(tmp_path: Path, fixture_repo: dict[str, Path]) -> tuple[Workspace, str, str, Path]:
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    (clone / "docs" / "feature-map.md").write_text("# Feature map\nrate limit\n")
    r = finish_session(clone, tmp_path / "out", branch=BRANCH, base_sha=base)
    head = ws.import_result(REPO, TID, tmp_path / "out", BRANCH, base, head=r["head"])
    return ws, base, head, fixture_repo["bare"]


def _pub(ws: Workspace, base: str, head: str, pulls: FakePulls, branch: str = BRANCH) -> str:
    return publish(
        ws,
        repo=REPO,
        head=head,
        base_sha=base,
        branch=branch,
        base="main",
        title="t",
        body="b",
        pulls=pulls,
        scope=["docs/feature-map.md"],
    )


def test_publish_pushes_the_branch_and_opens_a_draft(
    setup: tuple[Workspace, str, str, Path],
) -> None:
    ws, base, head, bare = setup
    pulls = FakePulls(bare)
    assert _pub(ws, base, head, pulls) == "https://github.com/o/fixture/pull/1"
    assert pulls.prs[0]["draft"] is True and pulls.prs[0]["base"] == "main"
    assert git(bare, "rev-parse", f"refs/heads/{BRANCH}") == head
    assert git(bare, "log", "-1", "--format=%s", "main") == "seed"


@pytest.mark.parametrize("branch", ["main", "master", "feature/x", "vigil/../main"])
def test_publish_refuses_anything_but_a_vigil_branch(
    setup: tuple[Workspace, str, str, Path], branch: str
) -> None:
    ws, base, head, bare = setup
    with pytest.raises(PublishError):
        _pub(ws, base, head, FakePulls(bare), branch=branch)


def test_publish_never_falls_back_to_a_non_draft(setup: tuple[Workspace, str, str, Path]) -> None:
    ws, base, head, bare = setup
    pulls = FakePulls(bare, refuse_draft=True)
    with pytest.raises(PublishError, match="Draft"):
        _pub(ws, base, head, pulls)
    assert pulls.prs == []


def test_publish_with_nothing_changed_fails(setup: tuple[Workspace, str, str, Path]) -> None:
    ws, base, _, bare = setup
    with pytest.raises(PublishError, match="changes nothing"):
        _pub(ws, base, base, FakePulls(bare))


def test_publish_refuses_files_outside_the_scope(
    tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    (clone / "docs" / "feature-map.md").write_text("ok\n")
    (clone / "README.md").write_text("sneaky\n")
    r = finish_session(clone, tmp_path / "out", branch=BRANCH, base_sha=base)
    head = ws.import_result(REPO, TID, tmp_path / "out", BRANCH, base, head=r["head"])
    pulls = FakePulls(fixture_repo["bare"])
    with pytest.raises(PublishError, match=r"README\.md"):
        _pub(ws, base, head, pulls)
    with pytest.raises(subprocess.CalledProcessError):  # nothing was pushed
        git(fixture_repo["bare"], "rev-parse", "--verify", f"refs/heads/{BRANCH}")
    assert pulls.prs == []


def test_vigil_artifacts_are_always_in_scope(tmp_path: Path, fixture_repo: dict[str, Path]) -> None:
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    (clone / ".vigil" / "fx-0001" / TID).mkdir(parents=True)
    (clone / ".vigil" / "fx-0001" / TID / "worker_report.json").write_text("{}")
    (clone / "docs" / "feature-map.md").write_text("ok\n")
    r = finish_session(clone, tmp_path / "out", branch=BRANCH, base_sha=base)
    head = ws.import_result(REPO, TID, tmp_path / "out", BRANCH, base, head=r["head"])
    assert _pub(ws, base, head, FakePulls(fixture_repo["bare"])).endswith("/pull/1")
