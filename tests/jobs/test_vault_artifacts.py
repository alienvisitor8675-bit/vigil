"""Tier 1 (owner, 2026-10-26): a job's `.vigil/<job>/` artifacts stay in the vault.

Both phase 1 PRs on a real repo carried `.vigil/<job>/` into the target repo. What
vigild pushes is the checked tree without `.vigil/`; the vault keeps the worker's
whole result, so the verifier, the reviewer, a fix round, a retry and a replan read
what they always read. A job cannot enter `ready` while a PR head's tree has `.vigil/`.
"""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git
from tests.harness import finish_session
from tests.jobs.test_coord_loop import (  # noqa: F401
    REPO,
    World,
    _approve,
    _failure_item,
    _flaky,
    auth,
    world,
)
from tests.jobs.test_in_review_invariant import _plan_of
from tests.review.test_review_stage import CHANGES, _rounds
from vigild.github.publish import LegacyBranch, PublishError, publish
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import JobState, Role
from vigild.provenance import Provenance
from vigild.workspace import Workspace

S = JobState
TID, BRANCH = "fx-0001-w1", "vigil/fx-0001-w1-doc"
SCOPE = ["docs/feature-map.md", ".vigil/fx-0001/fx-0001-w1/**"]
REPORT = ".vigil/fx-0001/fx-0001-w1/worker_report.json"


@dataclass
class Pulls:
    prs: list[str] = field(default_factory=list)

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> str:
        self.prs.append(head)
        return f"https://github.com/{repo}/pull/{len(self.prs)}"


def _session(ws: Workspace, tmp: Path, bare: Path, text: str, *, start: str | None = None) -> str:
    """A worker session that edits the doc and writes its report; its result imported."""
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main", start=start)
    base = ws.base_sha(TID)
    (clone / "docs" / "feature-map.md").write_text(text)
    (clone / REPORT).parent.mkdir(parents=True, exist_ok=True)
    (clone / REPORT).write_text(json.dumps({"summary": text}))
    out = tmp / f"out-{len(text)}"
    r = finish_session(clone, out, branch=BRANCH, base_sha=base)
    return ws.import_result(REPO, TID, out, BRANCH, base, head=r["head"])


@pytest.fixture
def ws(tmp_path: Path, fixture_repo: dict[str, Path]) -> Workspace:
    return Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))


def _pub(ws: Workspace, head: str, base: str, pulls: Pulls, existing: str | None = None) -> str:
    return publish(
        ws, repo=REPO, head=head, base_sha=base, branch=BRANCH, base="main", title="t",
        body="b", pulls=pulls, scope=SCOPE, existing=existing,
    )  # fmt: skip


def test_a_published_branch_has_no_vigil_and_the_vault_keeps_the_report(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    head = _session(ws, tmp_path, bare, "# Map\none\n")
    base = ws.base_sha(TID)
    assert ws.has_vigil(REPO, head, base)
    pulls = Pulls()
    _pub(ws, head, base, pulls)
    files = git(bare, "ls-tree", "-r", "--name-only", f"refs/heads/{BRANCH}").split("\n")
    assert "docs/feature-map.md" in files and not [f for f in files if f.startswith(".vigil")]
    assert git(bare, "show", f"refs/heads/{BRANCH}:docs/feature-map.md") == "# Map\none"
    # one PR commit on the base, never the worker's own commit
    assert git(bare, "rev-parse", f"refs/heads/{BRANCH}") != head
    assert git(bare, "rev-parse", f"refs/heads/{BRANCH}~1") == base
    # the vault still holds the whole result, which readers get as vault blobs
    assert REPORT in ws.tree_paths(REPO, head)
    assert json.loads(ws.read_blob(REPO, head, REPORT) or "")["summary"] == "# Map\none\n"
    assert ws.has_vigil(REPO, head, base)
    sent = ws.published_sha(REPO, BRANCH)
    assert sent and not ws.has_vigil(REPO, sent, base)


def test_a_fix_round_moves_the_branch_forward_without_vigil(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    first = _session(ws, tmp_path, bare, "# Map\none\n")
    base = ws.base_sha(TID)
    pulls = Pulls()
    url = _pub(ws, first, base, pulls)
    sent1 = git(bare, "rev-parse", f"refs/heads/{BRANCH}")
    # the fix round starts from the vault head, `.vigil/` and all, on the same base
    second = _session(ws, tmp_path, bare, "# Map\ntwo, longer\n", start=first)
    assert _pub(ws, second, base, pulls, existing=url) == url
    sent2 = git(bare, "rev-parse", f"refs/heads/{BRANCH}")
    assert git(bare, "rev-parse", f"{sent2}~1") == sent1  # a fast-forward, no force needed
    assert ".vigil" not in git(bare, "ls-tree", "-r", "--name-only", sent2)
    assert git(bare, "show", f"{sent2}:docs/feature-map.md") == "# Map\ntwo, longer"
    assert pulls.prs == [BRANCH]  # the PR that was open stands
    assert REPORT in ws.tree_paths(REPO, second)


def test_a_branch_that_never_had_vigil_is_pushed_as_it_is(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    (clone / "docs" / "feature-map.md").write_text("# Map\nplain\n")
    r = finish_session(clone, tmp_path / "o", branch=BRANCH, base_sha=base)
    head = ws.import_result(REPO, TID, tmp_path / "o", BRANCH, base, head=r["head"])
    _pub(ws, head, base, Pulls())
    assert git(bare, "rev-parse", f"refs/heads/{BRANCH}") == head


def test_a_change_only_under_vigil_changes_nothing_for_the_target_repo(
    ws: Workspace, tmp_path: Path
) -> None:
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    (clone / REPORT).parent.mkdir(parents=True)
    (clone / REPORT).write_text("{}")
    r = finish_session(clone, tmp_path / "o", branch=BRANCH, base_sha=base)
    head = ws.import_result(REPO, TID, tmp_path / "o", BRANCH, base, head=r["head"])
    with pytest.raises(PublishError, match="changes nothing"):
        _pub(ws, head, base, Pulls())


def test_a_branch_sent_before_this_rule_is_refused_and_never_forced(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    head = _session(ws, tmp_path, bare, "# Map\none\n")
    base = ws.base_sha(TID)
    git(ws.vault(REPO), "push", "-q", str(bare), f"{head}:refs/heads/{BRANCH}")  # the old way
    with pytest.raises(LegacyBranch, match="predates"):
        _pub(ws, head, base, Pulls(), existing="https://github.com/o/fixture/pull/9")
    assert git(bare, "rev-parse", BRANCH) == head  # the remote is as the owner left it
    assert ws.published_sha(REPO, BRANCH) is None


def _foreign_commit(bare: Path, base: str) -> str:
    """A commit on the remote branch that is not in the vault (someone else's push)."""
    tree = git(bare, "rev-parse", f"{base}^{{tree}}")
    sha = git(
        bare,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        "owner's own fix",
    )
    git(bare, "update-ref", f"refs/heads/{BRANCH}", sha)
    return sha


def test_a_remote_commit_the_vault_lacks_is_never_overwritten(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    first = _session(ws, tmp_path, bare, "# Map\none\n")
    base = ws.base_sha(TID)
    url = _pub(ws, first, base, Pulls())
    # an identity of its own: the dev image (and CI) has no global git config
    foreign = git(bare, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
                  git(bare, "rev-parse", f"{BRANCH}^{{tree}}"),
                  "-p", BRANCH, "-m", "owner's own fix")  # fmt: skip
    git(bare, "update-ref", f"refs/heads/{BRANCH}", foreign)
    second = _session(ws, tmp_path, bare, "# Map\ntwo, longer\n", start=first)
    with pytest.raises(PublishError):
        _pub(ws, second, base, Pulls(), existing=url)
    assert git(bare, "rev-parse", BRANCH) == foreign
    # a first send onto a remote branch of that name fails the same way, untouched
    ws2_foreign = _foreign_commit(bare, base)
    ws.record_published(REPO, BRANCH, base)  # not the remote's tip
    with pytest.raises(PublishError):
        _pub(ws, second, base, Pulls(), existing=url)
    assert git(bare, "rev-parse", BRANCH) == ws2_foreign


def test_a_base_that_tracks_its_own_vigil_keeps_it_and_the_public_diff_is_exact(
    tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    work = tmp_path / "seedwork"
    git(tmp_path, "clone", "-q", str(bare), str(work))
    (work / ".vigil").mkdir()
    (work / ".vigil" / "x").write_text("the repo's own\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "own .vigil")
    git(work, "push", "-q", "origin", "HEAD:main")
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(bare))
    head = _session(ws, tmp_path, bare, "# Map\none\n")
    base = ws.base_sha(TID)
    _pub(ws, head, base, Pulls())
    sent = git(bare, "rev-parse", BRANCH)
    files = git(bare, "ls-tree", "-r", "--name-only", sent).split("\n")
    assert ".vigil/x" in files and REPORT not in files
    assert git(bare, "show", f"{sent}:.vigil/x") == "the repo's own"
    assert git(bare, "diff", "--name-only", base, sent) == "docs/feature-map.md"
    # the guard's own question is relative to the base's `.vigil`
    assert not ws.has_vigil(REPO, sent, base) and ws.has_vigil(REPO, head, base)


def test_the_public_commit_is_scope_checked_too(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    bare = fixture_repo["bare"]
    head = _session(ws, tmp_path, bare, "# Map\none\n")
    base = ws.base_sha(TID)
    clone = ws.clone(REPO, "fx-0001-w2", branch="vigil/fx-0001-w2-x", base="main")
    (clone / "README.md").write_text("out of scope\n")
    r = finish_session(clone, tmp_path / "o2", branch="vigil/fx-0001-w2-x", base_sha=base)
    stray = ws.import_result(
        REPO, "fx-0001-w2", tmp_path / "o2", "vigil/fx-0001-w2-x", base, head=r["head"]
    )
    ws.public_commit = lambda *a, **k: stray  # type: ignore[method-assign]
    with pytest.raises(PublishError, match="outside"):
        _pub(ws, head, base, Pulls())
    assert ws.published_sha(REPO, BRANCH) is None  # nothing was sent


def test_a_strip_is_deterministic_and_faithful_to_the_original_commit(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    head = _session(ws, tmp_path, fixture_repo["bare"], "# Map\none\n")
    base = ws.base_sha(TID)
    a = ws.public_commit(REPO, head, base, BRANCH)
    b = ws.public_commit(REPO, head, base, BRANCH)
    assert a == b and a != head
    fmt = "--format=%an|%ae|%ad|%cn|%ce|%cd|%B"
    vault = ws.vault(REPO)
    ours = git(vault, "log", "-1", "--date=raw", fmt, a)
    theirs = git(vault, "log", "-1", "--date=raw", fmt, head)
    assert ours == theirs and "vigild:" not in ours


# --- the ready guard -----------------------------------------------------------------------


def _held_in_review(w: World) -> str:
    w.runner.review = lambda job_id: None  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state is S.IN_REVIEW
    del w.runner.review  # the real review stage again
    return job


def _latest(w: World, tid: str) -> dict[str, Any]:
    rows = w.store.q(
        "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
        (tid,),
    )
    return json.loads(rows[0]["data"])  # type: ignore[no-any-return]


def _mark(w: World, job: str, value: bool | None) -> None:
    for t in w.store.list_tasks(job):
        w.store.add_event(
            "pr_open", {**_latest(w, t.id), "vigil_in_pr": value}, Provenance.HUMAN,
            job_id=job, task_id=t.id,
        )  # fmt: skip


def _heads(w: World, job: str) -> dict[str, Any]:
    return w.runner._pr_heads(job)  # type: ignore[no-any-return]


def _try_ready(w: World, job: str, **evidence: Any) -> str:
    with pytest.raises(TransitionError) as e:
        transition(w.store, job, S.READY, Actor.VIGILD, evidence=evidence)
    return str(e.value)


def test_the_ready_guard_passes_vigils_own_part_when_the_evidence_is_current(
    world: World,
) -> None:
    w = world
    job = _held_in_review(w)
    msg = _try_ready(w, job, pr_heads=_heads(w, job))
    assert "PR head" not in msg  # refused for its missing reviews only


def test_the_ready_guard_needs_the_live_evidence(world: World) -> None:
    w = world
    job = _held_in_review(w)
    assert "PR head" in _try_ready(w, job)  # fails closed with none


def test_a_stale_event_fails_the_ready_guard(world: World) -> None:
    w = world
    job = _held_in_review(w)
    for t in w.store.list_tasks(job):  # a later send changed the sha the event names
        w.store.add_event(
            "pr_open", {**_latest(w, t.id), "published": "0" * 40}, Provenance.HUMAN,
            job_id=job, task_id=t.id,
        )  # fmt: skip
    assert "PR head" in _try_ready(w, job, pr_heads=_heads(w, job))


def test_a_pr_head_that_carries_vigil_fails_the_ready_guard(world: World) -> None:
    w = world
    job = _held_in_review(w)
    heads = {t: {**h, "vigil": True} for t, h in _heads(w, job).items()}
    assert "PR head" in _try_ready(w, job, pr_heads=heads)


def test_a_missing_pr_open_row_fails_the_ready_guard(world: World) -> None:
    w = world
    job = _held_in_review(w)
    heads = _heads(w, job)
    w.store.x("DELETE FROM events WHERE type='pr_open' AND job_id=?", (job,))
    assert "PR head" in _try_ready(w, job, pr_heads=heads)


# --- the runner ----------------------------------------------------------------------------


def _vault_tree(w: World, tid: str) -> set[str]:
    head = w.runner._task_head(tid)
    assert head
    return w.runner.workspace.tree_paths(REPO, head)


def test_a_fix_round_and_the_reviewer_read_the_vault_while_the_branch_stays_clean(
    world: World,
) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES]})
    job = _approve(w)
    assert w.store.get_job(job).state is S.READY
    tid = f"{job}-w1"
    # the fix round's session was cloned from the vault head, report and all
    assert len([s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id) == tid]) == 2
    assert f".vigil/{job}/{tid}/worker_report.json" in _vault_tree(w, tid)
    assert any(s.role is Role.REVIEWER for s in w.standin.specs)
    for n in (1, 2):
        out = git(w.bare, "ls-tree", "-r", "--name-only", f"vigil/{job}-w{n}")
        assert ".vigil" not in out
    log = git(w.bare, "log", "--format=%s", f"vigil/{job}-w1").split("\n")
    assert log[-1] == "seed" and len(log) >= 2  # a fix round that changes only .vigil adds none
    assert _latest(w, tid)["vigil_in_pr"] is False


def test_a_retry_after_a_failed_worker_still_finds_its_artifacts(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is S.READY
    for n in (1, 2):
        tid = f"{job}-w{n}"
        assert ".vigil" not in git(w.bare, "ls-tree", "-r", "--name-only", f"vigil/{tid}")
        assert f".vigil/{job}/{tid}/worker_report.json" in _vault_tree(w, tid)


def test_a_replan_keeps_the_published_tasks_artifacts_in_the_vault(world: World) -> None:
    w = world
    w.runner.review = lambda job_id: None  # type: ignore[method-assign]
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    w.standin.coord_drafts = [lambda j: (_spec(w, j), _plan_of(j, (f"{j}-w3", "checker")))]
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    spec_item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    r = w.client.post(
        f"/api/inbox/{spec_item['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is S.IN_REVIEW
    for tid in (f"{job}-w2", f"{job}-w3"):
        assert ".vigil" not in git(w.bare, "ls-tree", "-r", "--name-only", f"vigil/{tid}")
        assert f".vigil/{job}/{tid}/worker_report.json" in _vault_tree(w, tid)
        assert w.store.get_artifact(job, "worker_report.json", tid) is not None


def _spec(w: World, job: str) -> str:
    from tests.jobs.test_coord_loop import SPEC

    return SPEC


def test_a_stale_event_is_rewritten_when_the_branch_is_right(world: World) -> None:
    w = world
    job = _held_in_review(w)
    for t in w.store.list_tasks(job):
        w.store.add_event(
            "pr_open", {**_latest(w, t.id), "published": "0" * 40, "vigil_in_pr": None},
            Provenance.HUMAN, job_id=job, task_id=t.id,
        )  # fmt: skip
    w.runner.review(job)
    assert w.store.get_job(job).state is S.READY
    for t in w.store.list_tasks(job):
        assert _latest(w, t.id)["published"] == w.runner.workspace.published_sha(
            REPO, _latest(w, t.id)["branch"]
        )


def test_a_pre_change_pr_pages_for_the_owner_and_nothing_is_forced(world: World) -> None:
    w = world
    job = _held_in_review(w)
    vault = w.runner.workspace.vault(REPO)
    before = {}
    for t in w.store.list_tasks(job):
        branch = _latest(w, t.id)["branch"]
        git(vault, "update-ref", "-d", w.runner.workspace._pub_ref(branch))  # no record
        before[branch] = git(w.bare, "rev-parse", branch)
    w.runner.review(job)
    assert w.store.get_job(job).state is S.BLOCKED
    qs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (item,) = [q for q in qs if q["job_id"] == job and q["body"].get("kind") == "vigil_in_pr"]
    assert item["body"]["actions"] == ["replan", "abandon"]
    assert not item["body"]["resumable"] and "predates" in json.dumps(item["body"])
    assert {b: git(w.bare, "rev-parse", b) for b in before} == before


def test_a_pr_that_cannot_be_cleaned_pages_with_retry_and_abandon(world: World) -> None:
    w = world
    job = _held_in_review(w)
    ws = w.runner.workspace
    for t in w.store.list_tasks(job):  # the record is behind: the branch must be sent again
        ev = _latest(w, t.id)
        ws.record_published(REPO, ev["branch"], ev["base_sha"])
    real = w.publisher.publish

    def refuse(**kw: Any) -> str:
        raise PublishError("403: no")

    w.publisher.publish = refuse  # type: ignore[method-assign]
    w.runner.review(job)
    assert w.store.get_job(job).state is S.BLOCKED
    qs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (item,) = [q for q in qs if q["job_id"] == job and q["body"].get("kind") == "vigil_in_pr"]
    assert item["body"]["actions"] == ["retry", "abandon"] and item["body"]["resumable"]
    assert sorted(item["body"]["tasks"]) == [f"{job}-w1", f"{job}-w2"]
    # retry (re-strip) once the push works again
    w.publisher.publish = real  # type: ignore[method-assign]
    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is S.READY


def test_branch_moved_pages_with_replan_and_abandon_only(world: World) -> None:
    w = world
    real = w.publisher.publish
    moved: list[str] = []

    def publish_after_outside_commit(**kw: Any) -> str:
        if not moved:  # the owner commits to the first branch before vigild sends it
            moved.append(kw["branch"])
            tree = git(w.bare, "rev-parse", "main^{tree}")
            sha = git(w.bare, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
                      tree, "-p", "main", "-m", "outside")  # fmt: skip
            git(w.bare, "update-ref", f"refs/heads/{kw['branch']}", sha)
        return real(**kw)

    w.publisher.publish = publish_after_outside_commit  # type: ignore[method-assign]
    job = _approve(w)
    item = _failure_item(w, job)
    assert item["body"]["actions"] == ["replan", "abandon"] and not item["body"]["resumable"]
    assert any(
        t["title"].endswith("branch moved on the remote") for t in item["body"]["tasks"].values()
    )
    assert w.runner.workspace.published_sha(REPO, moved[0]) is None


def test_publish_not_moved_still_offers_retry(world: World) -> None:
    w = world
    real = w.publisher.publish
    calls: list[str] = []

    def refuse_once(**kw: Any) -> str:
        if not calls:
            calls.append(kw["branch"])
            raise PublishError("git ls-remote failed: Authentication failed")
        return real(**kw)

    w.publisher.publish = refuse_once  # type: ignore[method-assign]
    job = _approve(w)
    item = _failure_item(w, job)
    assert item["body"]["actions"] == ["retry", "replan", "abandon"]
    assert any(t["title"].endswith("publish refused") for t in item["body"]["tasks"].values())
