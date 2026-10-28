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
from vigild.github.publish import PublishError, artifact_paths, publish
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


def _artifact_pub(
    tmp_path: Path, fixture_repo: dict[str, Path], files: list[str]
) -> tuple[Workspace, str, str]:
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    for f in files:
        (clone / f).parent.mkdir(parents=True, exist_ok=True)
        (clone / f).write_text("{}")
    (clone / "docs" / "feature-map.md").write_text("ok\n")
    r = finish_session(clone, tmp_path / "out", branch=BRANCH, base_sha=base)
    head = ws.import_result(REPO, TID, tmp_path / "out", BRANCH, base, head=r["head"])
    return ws, base, head


def _scoped(ws: Workspace, base: str, head: str, pulls: FakePulls) -> str:
    return publish(
        ws, repo=REPO, head=head, base_sha=base, branch=BRANCH, base="main", title="t", body="b",
        pulls=pulls,
        scope=["docs/feature-map.md", *artifact_paths("fx-0001", TID)],
    )  # fmt: skip


def test_a_worker_may_write_only_its_own_task_folder_and_task_json(
    tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    ws, base, head = _artifact_pub(
        tmp_path,
        fixture_repo,
        [".vigil/fx-0001/fx-0001-w1/worker_report.json", ".vigil/task.json"],
    )
    assert _scoped(ws, base, head, FakePulls(fixture_repo["bare"])).endswith("/pull/1")


@pytest.mark.parametrize(
    "path",
    [
        ".vigil/fx-0001/spec.md",  # its own job's other paths
        ".vigil/fx-0001/fx-0001-w2/worker_report.json",  # another task's folder
        ".vigil/fx-0002/fx-0002-w1/worker_report.json",  # another job
        ".vigil/fx-0001/hidden/fx-0001-w1/test_x.py",  # where hidden tests live
        ".vigil/fx-0001/hidden/pytest.ini",
        ".vigil/playbook/rules.md",  # the playbook paths
        ".vigil/other.json",
    ],
)
def test_any_other_vigil_path_is_out_of_scope(
    tmp_path: Path, fixture_repo: dict[str, Path], path: str
) -> None:
    ws, base, head = _artifact_pub(tmp_path, fixture_repo, [path])
    pulls = FakePulls(fixture_repo["bare"])
    with pytest.raises(PublishError, match="outside the contract's scope"):
        _scoped(ws, base, head, pulls)
    assert pulls.prs == []


def test_the_pr_poll_reads_a_prs_state() -> None:
    """Plan 2a, item 5: merged, closed or open, from GitHub's pulls API."""
    import httpx2

    from vigild.github.publish import RestPulls

    answers = {"1": {"merged": True, "state": "closed"}, "2": {"merged": False, "state": "closed"},
               "3": {"merged": False, "state": "open"}}  # fmt: skip

    def handler(req: httpx2.Request) -> httpx2.Response:
        assert req.headers["authorization"] == "Bearer ghs_read"
        return httpx2.Response(200, json=answers[req.url.path.rsplit("/", 1)[1]])

    pulls = RestPulls(httpx2.Client(transport=httpx2.MockTransport(handler)), "ghs_read")
    assert [pulls.state("o/x", n) for n in (1, 2, 3)] == ["merged", "closed", "open"]


def test_the_pr_poll_reads_only_the_repos_own_prs() -> None:
    import pytest

    from vigild.github.publish import PublishError
    from vigild.jobs.runner import GitHubPRs

    with pytest.raises(PublishError):
        GitHubPRs(app=None).state("o/x", "https://github.com/o/other/pull/1")


def test_closing_a_pr_patches_its_state_with_a_one_repo_write_token() -> None:
    """Owner ruling 3: vigild closes a deps PR whose review failed."""
    import json

    import httpx2

    from vigild.github.app import InstallationToken
    from vigild.github.publish import PublishError
    from vigild.jobs.runner import GitHubPublisher

    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, json={"state": "closed"})

    leased: list[tuple[str, dict[str, str]]] = []

    class App:
        from contextlib import contextmanager

        @contextmanager
        def lease(self, repo: str, perms: dict[str, str], job_id: str | None = None):  # type: ignore[no-untyped-def]
            leased.append((repo, perms))
            yield InstallationToken(repo, perms, "z", "ghs_w")

    pub = GitHubPublisher(App(), httpx2.Client(transport=httpx2.MockTransport(handler)), None)  # type: ignore[arg-type]
    pub.close("o/fixture", "https://github.com/o/fixture/pull/7")
    (req,) = seen
    assert req.method == "PATCH" and req.url.path == "/repos/o/fixture/pulls/7"
    assert json.loads(req.content) == {"state": "closed"}
    assert leased == [("o/fixture", {"pull_requests": "write"})]
    try:
        pub.close("o/fixture", "https://github.com/other/repo/pull/7")
    except PublishError:
        pass
    else:
        raise AssertionError("a PR of another repo was closed")
