"""Review round 1, blockers 1-3: host git never runs in a worker-writable
`.git`; the scope check can't be dodged by renames or a forged base ref;
planted symlinks never read host files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import git
from tests.harness import finish_session
from vigild.github.publish import PublishError, publish
from vigild.safefile import UnsafeFile, safe_read_bytes
from vigild.workspace import Workspace, WorkspaceError

REPO = "o/fixture"
BRANCH = "vigil/fx-0001-w1"
TID = "fx-0001-w1"


class Pulls:
    def __init__(self) -> None:
        self.prs: list[dict[str, object]] = []

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> str:
        self.prs.append({"head": head, "draft": draft})
        return f"https://github.com/{repo}/pull/{len(self.prs)}"


@pytest.fixture
def ws(tmp_path: Path, fixture_repo: dict[str, Path]) -> Workspace:
    seed = fixture_repo["seed"]
    (seed / "money").mkdir()
    (seed / "money" / "critical.py").write_text("FEE = 1\n")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_a.py").write_text("def test_a():\n    assert 1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "more files")
    git(seed, "push", "-q", "origin", "main")
    return Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))


def _session(ws: Workspace, tmp_path: Path, edit: object = None) -> tuple[Path, Path, str]:
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    if callable(edit):
        edit(clone)
    else:
        (clone / "docs" / "feature-map.md").write_text("# Feature map\nrate limit\n")
    out = tmp_path / "out"
    finish_session(
        clone,
        out,
        branch=BRANCH,
        base_sha=base,
    )
    return clone, out, base


def _tip(out: Path) -> str:
    """The branch tip in the bundle: what the harness names on its result line."""
    heads = git(out, "bundle", "list-heads", str(out / "result.bundle"), f"refs/heads/{BRANCH}")
    return heads.split()[0] if heads else "0" * 40


def _plant_everything(clone: Path, tmp_path: Path) -> Path:
    """What a hostile worker leaves in its .git for host git to trip on."""
    marker = tmp_path / "PWNED"
    evil = tmp_path / "evil-common"
    (evil / "hooks").mkdir(parents=True)
    hook = f"#!/bin/sh\necho hook >> {marker}\n"
    for name in ("pre-push", "post-checkout", "reference-transaction", "pre-commit"):
        (evil / "hooks" / name).write_text(hook)
        (evil / "hooks" / name).chmod(0o755)
    ext = tmp_path / "ext.sh"
    ext.write_text(f"#!/bin/sh\necho ext >> {marker}\n")
    ext.chmod(0o755)
    (evil / "config").write_text(
        f"[core]\n\thooksPath = {evil / 'hooks'}\n\tfsmonitor = {ext}\n"
        f"[diff]\n\texternal = {ext}\n"
        f'[url "{tmp_path}/elsewhere"]\n\tinsteadOf = /\n'
    )
    for d in ("objects", "refs"):
        os.symlink(clone / ".git" / d, evil / d)
    (clone / ".git" / "commondir").write_text(str(evil) + "\n")
    alt = clone / ".git" / "objects" / "info"
    alt.mkdir(parents=True, exist_ok=True)
    (alt / "alternates").write_text(str(tmp_path / "someone-elses-repo" / "objects") + "\n")
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").write_text(hook)
    (hooks / "pre-push").chmod(0o755)
    return marker


# --- blocker 1 ----------------------------------------------------------------------------


def test_planted_git_state_never_runs_on_the_host(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    clone, out, base = _session(ws, tmp_path)
    marker = _plant_everything(clone, tmp_path)
    head = ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))
    assert ws.changed(REPO, base, head) == [("M", "docs/feature-map.md")]
    ws.diff(REPO, base, head, "docs/feature-map.md")
    url = publish(
        ws,
        repo=REPO,
        head=head,
        base_sha=base,
        branch=BRANCH,
        base="main",
        title="t",
        body="b",
        pulls=Pulls(),
        scope=["docs/feature-map.md"],
    )
    assert url.endswith("/pull/1")
    assert not marker.exists()
    vault = ws.vault(REPO)
    assert not (vault / "objects" / "info" / "alternates").exists()
    assert not (vault / "commondir").exists()
    assert git(fixture_repo["bare"], "rev-parse", f"refs/heads/{BRANCH}") == head


def test_the_planted_hook_would_have_run_under_the_old_host_git(
    ws: Workspace, tmp_path: Path
) -> None:
    """Control: plain host git in the worker's clone does run the plant."""
    clone, _, _ = _session(ws, tmp_path)
    marker = _plant_everything(clone, tmp_path)
    subprocess.run(["git", "diff", "HEAD~1"], cwd=clone, capture_output=True, check=False)
    assert marker.exists()


def test_only_the_sessions_branch_is_imported(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        (c / "docs" / "feature-map.md").write_text("x\n")
        git(c, "branch", "-f", "main", "HEAD")  # tries to ship a moved main too

    clone, out, base = _session(ws, tmp_path, edit)
    git(clone, "bundle", "create", "-q", str(out / "result.bundle"), "--all")
    ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))
    refs = git(ws.vault(REPO), "for-each-ref", "--format=%(refname)", "refs/vigil", "refs/heads")
    assert refs.splitlines() == [f"refs/vigil/{TID}"]


def test_a_branch_that_does_not_descend_from_the_base_is_refused(
    ws: Workspace, tmp_path: Path
) -> None:
    def edit(c: Path) -> None:
        git(c, "checkout", "-q", "--orphan", "tmp")
        git(c, "rm", "-rqf", ".")
        (c / "x").write_text("x")
        git(c, "add", "x")
        git(c, "commit", "-qm", "orphan")
        git(c, "branch", "-f", BRANCH, "HEAD")
        git(c, "checkout", "-q", BRANCH)

    clone, out, base = _session(ws, tmp_path, edit)
    git(clone, "bundle", "create", "-q", str(out / "result.bundle"), f"refs/heads/{BRANCH}")
    with pytest.raises(WorkspaceError, match="descend"):
        ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))


# --- blocker 2 ----------------------------------------------------------------------------


def _publish(ws: Workspace, head: str, base: str, scope: list[str]) -> str:
    return publish(
        ws,
        repo=REPO,
        head=head,
        base_sha=base,
        branch=BRANCH,
        base="main",
        title="t",
        body="b",
        pulls=Pulls(),
        scope=scope,
    )


def test_rename_out_of_scope_is_caught(ws: Workspace, tmp_path: Path) -> None:
    _clone, out, base = _session(
        ws, tmp_path, lambda c: git(c, "mv", "money/critical.py", "docs/x.py")
    )
    head = ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))
    assert ("D", "money/critical.py") in ws.changed(REPO, base, head)
    with pytest.raises(PublishError, match=r"money/critical\.py"):
        _publish(ws, head, base, ["docs/x.py"])


def test_renamed_away_test_counts_as_deleted(ws: Workspace, tmp_path: Path) -> None:
    from vigild.jobs.acceptance import weakened_tests

    _clone, out, base = _session(
        ws, tmp_path, lambda c: git(c, "mv", "tests/test_a.py", "docs/test_a.txt")
    )
    head = ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))
    assert weakened_tests(ws, REPO, base, head) == ["tests/test_a.py was deleted"]


def test_a_forged_origin_ref_does_not_hide_changes(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        (c / "money" / "critical.py").write_text("FEE = 0\n")
        git(c, "add", "-A")
        git(c, "commit", "-qm", "sneak")
        git(c, "update-ref", "refs/remotes/origin/main", "HEAD")

    _clone, out, base = _session(ws, tmp_path, edit)
    head = ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))
    with pytest.raises(PublishError, match=r"money/critical\.py"):
        _publish(ws, head, base, ["docs/feature-map.md"])


def test_base_is_recorded_outside_the_clone(ws: Workspace, tmp_path: Path) -> None:
    clone, _, base = _session(ws, tmp_path)
    assert git(clone, "remote") == ""  # no origin: nothing to forge against
    assert (ws.data_dir / "clones" / f"{TID}.base").is_file()
    assert len(base) == 40


# --- blocker 3 ----------------------------------------------------------------------------


def test_safe_read_refuses_symlinks_and_outside_paths(tmp_path: Path) -> None:
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=x")
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    os.symlink(secret, root / "d" / "f.json")
    with pytest.raises(UnsafeFile):
        safe_read_bytes(root, root / "d" / "f.json")
    os.symlink(tmp_path, root / "link")
    with pytest.raises(UnsafeFile):
        safe_read_bytes(root, root / "link" / "secret.env")
    with pytest.raises(UnsafeFile):
        safe_read_bytes(root, secret)
    (root / "big").write_bytes(b"x" * 100)
    with pytest.raises(UnsafeFile):
        safe_read_bytes(root, root / "big", max_bytes=10)
    (root / "ok").write_text("fine")
    assert safe_read_bytes(root, root / "ok") == b"fine"


def test_a_committed_symlink_is_never_read_as_an_artifact(ws: Workspace, tmp_path: Path) -> None:
    secret = tmp_path / "secret.env"
    secret.write_text("ANTHROPIC_API_KEY=do-not-leak")

    def edit(c: Path) -> None:
        (c / "docs" / "feature-map.md").write_text("# Feature map\nrate limit\n")
        d = c / ".vigil" / "fx-0001"
        d.mkdir(parents=True)
        os.symlink(secret, d / "spec.md")

    _clone, out, base = _session(ws, tmp_path, edit)
    head = ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))
    assert ws.read_blob(REPO, head, ".vigil/fx-0001/spec.md") is None
    assert ws.read_blob(REPO, head, "docs/feature-map.md") == "# Feature map\nrate limit\n"


def test_a_symlinked_bundle_is_refused(ws: Workspace, tmp_path: Path) -> None:
    _clone, out, base = _session(ws, tmp_path)
    real = tmp_path / "real.bundle"
    (out / "result.bundle").rename(real)
    os.symlink(real, out / "result.bundle")
    with pytest.raises(WorkspaceError, match="bundle"):
        ws.import_result(REPO, TID, out, BRANCH, base, head=_tip(out))


# --- review round 2, N1: the bundle is bound to the head on PID 1's result line --------


def test_a_bundle_swapped_after_the_harness_named_its_head_is_refused(
    ws: Workspace, tmp_path: Path
) -> None:
    """The after-test runs repo code after the bundle is written; if that code
    swaps the bundle for another branch tip, vigild refuses it."""
    clone, out, base = _session(ws, tmp_path)
    named = _tip(out)
    (clone / "docs" / "runbook.md").write_text("swapped\n")
    git(clone, "commit", "-qam", "swap")
    git(clone, "bundle", "create", "-q", str(out / "result.bundle"), f"refs/heads/{BRANCH}",
        f"^{base}")  # fmt: skip
    with pytest.raises(WorkspaceError, match="head"):
        ws.import_result(REPO, TID, out, BRANCH, base, head=named)
    refs = git(ws.vault(REPO), "for-each-ref", "--format=%(refname)", "refs/vigil")
    assert refs == ""


def test_a_malformed_head_is_refused(ws: Workspace, tmp_path: Path) -> None:
    _clone, out, base = _session(ws, tmp_path)
    with pytest.raises(WorkspaceError, match="head"):
        ws.import_result(REPO, TID, out, BRANCH, base, head="HEAD")


# --- review round 2, N3: a FIFO can't hang vigild ------------------------------------------


def test_safe_read_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    import threading

    root = tmp_path / "out"
    root.mkdir()
    os.mkfifo(root / "result.bundle")
    errors: list[BaseException] = []

    def read() -> None:
        try:
            safe_read_bytes(root, root / "result.bundle")
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "opening the FIFO blocked"
    assert errors and isinstance(errors[0], UnsafeFile)


def test_host_git_reads_pathspecs_literally(tmp_path: Path) -> None:  # round 3, F8
    from vigild.gitsafe import git as safe_git

    repo = tmp_path / "r"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@x"), ("config", "user.name", "t")):
        git(repo, *args)
    (repo / "a*").write_text("star\n")
    (repo / "ab").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "two")
    assert safe_git(repo, "ls-files", "--", "a*").split() == ["a*"]
