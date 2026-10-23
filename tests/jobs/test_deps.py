"""Deps jobs (plan 2a, item 5; B39): no model, a lockfile-only update in a
registry-only container, the 14-day rule on everything the lockfile pulls in,
manifests changed only in their dependency fields, the dev image rebuilt and
validated, its own PR, and the parent resumed on the new image once it merges."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.conftest import auth, git
from tests.jobs.test_coord_loop import CLEAN_REVIEW, REPO, World, _approve, _failure_item, _work
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import assert_actionable
from vigild.contract.models import DepRequest
from vigild.executors.base import SessionSpec
from vigild.jobs.deps import (
    DepsExecutor,
    check_requests,
    lock_packages,
    manifest_errors,
    update_command,
)
from vigild.workspace import Workspace

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
PYPROJECT = (
    '[project]\nname = "fixture"\nversion = "0.1.0"\nrequires-python = ">=3.14"\n'
    'dependencies = ["pytest==9.1.1"]\n'
)
LOCK = (
    'version = 1\n\n[[package]]\nname = "pytest"\nversion = "9.1.1"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
)


def _lock(*pkgs: tuple[str, str]) -> str:
    return LOCK + "".join(
        f'\n[[package]]\nname = "{n}"\nversion = "{v}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        for n, v in pkgs
    )


class Registry:
    def __init__(self, ages: dict[str, int]) -> None:
        self.ages = ages  # name -> days old
        self.asked: list[tuple[str, str, str]] = []

    def published(
        self, ecosystem: str, name: str, version: str, hashes: frozenset[str] = frozenset()
    ) -> datetime | None:
        self.asked.append((ecosystem, name, version))
        days = self.ages.get(name)
        return None if days is None else NOW - timedelta(days=days)


def _seed(fixture_repo: dict[str, Any]) -> None:
    seed = fixture_repo["seed"]
    (seed / "pyproject.toml").write_text(PYPROJECT)
    (seed / "uv.lock").write_text(LOCK)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "lockfiles")
    git(seed, "push", "-q", "origin", "main")


class PackageManager:
    """Stands in for the build container: rewrites the files the way the
    package manager would, and records what it was asked to run."""

    def __init__(self, pyproject: str, lock: str, extra: dict[str, str] | None = None) -> None:
        self.pyproject, self.lock, self.extra = pyproject, lock, extra or {}
        self.ran: list[tuple[str, list[str], str]] = []

    def __call__(self, image: str, work: Path, command: str) -> tuple[int, str]:
        self.ran.append((image, sorted(str(p.relative_to(work)) for p in work.rglob("*")), command))
        (work / "pyproject.toml").write_text(self.pyproject)
        (work / "uv.lock").write_text(self.lock)
        for name, text in self.extra.items():
            (work / name).write_text(text)
        return 0, "Resolved 3 packages"


def _executor(
    tmp_path: Path, fixture_repo: dict[str, Any], pm: PackageManager, ages: dict[str, int]
) -> tuple[DepsExecutor, Workspace, str]:
    _seed(fixture_repo)
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    base = ws.head_sha(REPO, "main")
    ex = DepsExecutor(ws, pm, Registry(ages), lambda: NOW, tmp_path / "deps")
    return ex, ws, base


SIX = [DepRequest(ecosystem="pypi", package="six", version="1.17.0", why="compat")]
NEW_PY = PYPROJECT.replace('["pytest==9.1.1"]', '["pytest==9.1.1", "six==1.17.0"]')


def test_a_lockfile_only_update_with_old_packages_passes(
    tmp_path: Path, fixture_repo: dict[str, Any]
) -> None:
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0")))
    ex, _, base = _executor(tmp_path, fixture_repo, pm, {"six": 600})
    res = ex.run(REPO, base, ["pyproject.toml", "uv.lock"], SIX, "vigil-dev/fixture:1")
    assert res.errors == [] and res.added == ["six==1.17.0"]
    assert set(res.files) == {"pyproject.toml", "uv.lock"}
    # the work dir held the manifests and lockfiles only: no source, no .git
    ((image, files, command),) = pm.ran
    assert files == ["pyproject.toml", "uv.lock"] and image == "vigil-dev/fixture:1"
    assert command == "uv add --no-sync --no-build six==1.17.0"


def test_a_too_new_transitive_dependency_fails_the_job_with_the_list(
    tmp_path: Path, fixture_repo: dict[str, Any]
) -> None:
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0"), ("fresh-lib", "0.1.0"), ("odd", "2.0")))
    ex, _, base = _executor(tmp_path, fixture_repo, pm, {"six": 600, "fresh-lib": 3})
    res = ex.run(REPO, base, ["pyproject.toml", "uv.lock"], SIX, "img")
    assert res.added == ["fresh-lib==0.1.0", "odd==2.0", "six==1.17.0"]
    assert any("fresh-lib==0.1.0: published 2026-10-20, under 14 days" in e for e in res.errors)
    assert any("odd==2.0: no publish time" in e for e in res.errors)
    assert not any("six" in e for e in res.errors)


def test_a_manifest_edit_outside_the_dependency_fields_fails(
    tmp_path: Path, fixture_repo: dict[str, Any]
) -> None:
    sneaky = NEW_PY.replace('version = "0.1.0"', 'version = "9.9.9"')
    pm = PackageManager(sneaky, _lock(("six", "1.17.0")))
    ex, _, base = _executor(tmp_path, fixture_repo, pm, {"six": 600})
    res = ex.run(REPO, base, ["pyproject.toml", "uv.lock"], SIX, "img")
    assert res.errors == ["pyproject.toml: changed outside its dependency fields"]
    assert manifest_errors("package.json", '{"scripts": {"t": "a"}, "dependencies": {}}',
                           '{"scripts": {"t": "b"}, "dependencies": {"x": "1"}}')  # fmt: skip
    assert not manifest_errors(
        "package.json", '{"dependencies": {}}', '{"dependencies": {"x": "1"}}'
    )


def test_a_file_other_than_manifests_and_lockfiles_fails(
    tmp_path: Path, fixture_repo: dict[str, Any]
) -> None:
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0")), extra={"setup.py": "import os"})
    ex, _, base = _executor(tmp_path, fixture_repo, pm, {"six": 600})
    res = ex.run(REPO, base, ["pyproject.toml", "uv.lock"], SIX, "img")
    assert any("setup.py" in e for e in res.errors)


def test_the_deps_gate_and_the_parsers() -> None:
    ok = DepRequest(ecosystem="pypi", package="six", version="1.17.0")
    assert check_requests(["uv.lock"], [ok]) == []
    assert check_requests(["uv.lock"], [ok.model_copy(update={"version": ">=1.0"})])
    assert check_requests(["pnpm-lock.yaml"], [ok])  # no uv.lock to update
    npm = DepRequest(ecosystem="npm", package="@scope/x", version="1.2.3")
    assert check_requests(["pnpm-lock.yaml"], [npm]) == []
    assert update_command([npm]) == "pnpm add -w --lockfile-only --ignore-scripts @scope/x@1.2.3"
    pnpm = "lockfileVersion: '9.0'\npackages:\n  '@scope/x@1.2.3':\n    resolution: {}\n  y@4.0.0(z@1.0.0):\n    resolution: {}\n"
    assert lock_packages("pnpm-lock.yaml", pnpm) == {("@scope/x", "1.2.3"), ("y", "4.0.0")}


# --- the job: spawned, approved, built, merged, and the parent resumed ------------------------


def test_the_parent_resumes_on_the_new_image_key_after_the_deps_job_merges(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    w = world
    _seed(fixture_repo)
    w.runner.cfg.repos.repos[REPO].dev_lockfiles = ["pyproject.toml", "uv.lock"]
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0")))
    w.runner.deps = DepsExecutor(
        w.runner.workspace, pm, Registry({"six": 600}), lambda: NOW, w.runner.cfg.vigild.data_path
    )
    bare = fixture_repo["bare"]

    def pr_state(repo: str, url: str) -> str:
        n = int(url.rsplit("/", 1)[1])
        head = w.publisher.prs[n - 1]["head"]
        tip = git(bare, "rev-parse", head)
        merged = git(bare, "merge-base", "--is-ancestor", tip, "main") == ""
        return "merged" if merged else "open"

    def merged_ok(repo: str, url: str) -> str:
        try:
            return pr_state(repo, url)
        except Exception:
            return "open"

    w.runner.pr_state = merged_ok
    images: dict[str, list[str | None]] = {}
    tries: dict[str, int] = {}

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        tid = str(spec.task_id)
        tries[tid] = tries.get(tid, 0) + 1
        images.setdefault(tid, []).append(spec.image)
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if tid.endswith("w1") and tries[tid] == 1:
            d = spec.clone / ".vigil" / spec.job_id / tid
            d.mkdir(parents=True, exist_ok=True)
            (d / "worker_report.json").write_text(json.dumps({
                "task_id": tid, "job_id": spec.job_id, "files_changed": [],
                "summary": "needs six", "provenance": "claude",
                "self_check": {"test_failed_first": True, "outside_scope": [], "not_done": "all",
                               "reviewer_would_flag": "-", "could_delete": "-"},
                "needs_deps": [{"ecosystem": "pypi", "package": "six", "version": "1.17.0",
                                "why": "compat"}]}))  # fmt: skip

    w.standin.worker_edit = edit
    parent = _approve(w)
    assert w.store.get_job(parent).state.value == "blocked"
    page = _failure_item(w, parent, "needs_deps")
    assert_actionable(w, parent)  # the walk over blocked paths (coord decision 2)
    child = page["body"]["deps_job"]
    assert w.store.get_job(parent).blocked_on == child
    assert w.store.get_job(child).parent_job_id == parent
    # the deps job waits for the owner's approval, like any spec; nothing ran yet
    assert pm.ran == []
    (spec_item,) = [x for x in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()[
        "specs"] if x["job_id"] == child]  # fmt: skip
    assert spec_item["body"]["shape"] == "deps"
    r = w.client.post(
        f"/api/inbox/{spec_item['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    assert r.status_code == 200, r.text
    # no model ran for it: no session but the reviewer's and auditor's
    assert not [s for s in w.standin.specs if s.job_id == child and s.role.value == "worker"]
    assert len(pm.ran) == 1
    deps_pr = w.publisher.prs[-1]
    assert deps_pr["head"] == f"vigil/{child}-w1" and "the owner reviews" in deps_pr["body"]
    files = git(bare, "diff", "--name-only", "main", deps_pr["head"]).split()
    assert files == ["pyproject.toml", "uv.lock"]
    # reviewed at rung 1, then ready; the parent still waits
    rv = json.loads(w.store.get_artifact(child, "review.json", f"{child}-w1").content)  # type: ignore[union-attr]
    assert rv["shape"] == "deps" and rv["rung"] == 1
    assert w.store.get_job(child).state.value == "ready"
    assert w.runner.poll_prs() == []
    assert w.store.get_job(parent).state.value == "blocked"
    # the owner merges it; vigild's PR poll sees it, and the parent resumes on its own
    git(bare, "update-ref", "refs/heads/main", git(bare, "rev-parse", deps_pr["head"]))
    assert w.runner.poll_prs() == [child]
    assert w.store.get_job(child).state.value == "merged"
    assert w.store.get_job(parent).state.value == "ready"
    # on the new image key: the base's lockfiles changed
    first, second = images[f"{parent}-w1"]
    assert first and second and first != second
    assert w.store.get_task(f"{parent}-w1").state == "reviewed"


def test_a_uv_lock_keeps_its_cutoff(tmp_path: Path, fixture_repo: dict[str, Any]) -> None:
    """The 2a done-when's reviewer caught `uv add` dropping the lock's
    `[options] exclude-newer`: vigild passes it back, and fails a lock whose
    options changed."""
    from vigild.jobs.deps import uv_exclude_newer

    cut = "2026-10-08T23:59:59Z"
    assert (
        update_command(SIX, cut) == f"uv add --no-sync --no-build --exclude-newer {cut} six==1.17.0"
    )
    with_options = f'version = 1\n\n[options]\nexclude-newer = "{cut}"\n'
    assert uv_exclude_newer(with_options) == cut
    assert manifest_errors("uv.lock", with_options, "version = 1\n") == [
        "uv.lock: its resolution options changed"
    ]
    assert (
        manifest_errors("uv.lock", with_options, with_options + "\n[[package]]\nname = 'six'\n")
        == []
    )


def test_a_failed_deps_review_closes_the_pr_and_regenerates_once_then_pages(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    """Owner ruling 3: no fix round for a deps PR. The first failed review
    closes it and regenerates the deps job; the second closes that PR too and
    pages the owner. The parent stays blocked throughout."""
    from tests.review.test_review_stage import CHANGES

    w = world
    _seed(fixture_repo)
    w.runner.cfg.repos.repos[REPO].dev_lockfiles = ["pyproject.toml", "uv.lock"]
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0")))
    w.runner.deps = DepsExecutor(
        w.runner.workspace, pm, Registry({"six": 600}), lambda: NOW, w.runner.cfg.vigild.data_path
    )

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        d = spec.clone / ".vigil" / spec.job_id / str(spec.task_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "worker_report.json").write_text(json.dumps({
            "task_id": spec.task_id, "job_id": spec.job_id, "files_changed": [],
            "summary": "needs six", "provenance": "claude",
            "self_check": {"test_failed_first": True, "outside_scope": [], "not_done": "all",
                           "reviewer_would_flag": "-", "could_delete": "-"},
            "needs_deps": [{"ecosystem": "pypi", "package": "six", "version": "1.17.0",
                            "why": "compat"}]}))  # fmt: skip

    w.standin.worker_edit = edit
    parent = _approve(w)
    child = _failure_item(w, parent, "needs_deps")["body"]["deps_job"]
    w.standin.review = lambda spec: CHANGES if spec.job_id == child else CLEAN_REVIEW
    (item,) = [x for x in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
               if x["job_id"] == child]  # fmt: skip
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(child).state.value == "blocked"
    assert w.store.get_job(parent).state.value == "blocked"
    deps_prs = [p for p in w.publisher.prs if p["head"].startswith(f"vigil/{child}-w1")]
    assert [p["head"] for p in deps_prs] == [f"vigil/{child}-w1", f"vigil/{child}-w1-r2"]
    assert len(w.publisher.closed) == 2  # both closed: the first, then its regeneration
    assert len(pm.ran) == 2  # regenerated once, never a fix round by a model
    page = _failure_item(w, child, "deps_review")
    assert page["body"]["actions"] == ["abandon"] and "twice" in page["title"]
    assert_actionable(w, child)
    assert w.store.get_job(parent).state.value == "blocked"
    assert w.store.get_job(parent).blocked_on == child


def test_a_deps_pr_regenerated_once_and_then_clean_moves_on(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    from tests.review.test_review_stage import CHANGES

    w = world
    _seed(fixture_repo)
    w.runner.cfg.repos.repos[REPO].dev_lockfiles = ["pyproject.toml", "uv.lock"]
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0")))
    w.runner.deps = DepsExecutor(
        w.runner.workspace, pm, Registry({"six": 600}), lambda: NOW, w.runner.cfg.vigild.data_path
    )
    w.standin.worker_edit = _needs_six
    parent = _approve(w)
    child = _failure_item(w, parent, "needs_deps")["body"]["deps_job"]
    reviews = {"n": 0}

    def review(spec: SessionSpec) -> Any:
        if spec.job_id != child:
            return CLEAN_REVIEW
        reviews["n"] += 1
        return CHANGES if reviews["n"] == 1 else CLEAN_REVIEW

    w.standin.review = review
    (item,) = [x for x in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
               if x["job_id"] == child]  # fmt: skip
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(child).state.value == "ready"
    assert len(w.publisher.closed) == 1 and len(pm.ran) == 2
    assert w.store.get_job(parent).state.value == "blocked"


def _needs_six(spec: SessionSpec, task: dict[str, Any]) -> None:
    d = spec.clone / ".vigil" / spec.job_id / str(spec.task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "worker_report.json").write_text(json.dumps({
        "task_id": spec.task_id, "job_id": spec.job_id, "files_changed": [],
        "summary": "needs six", "provenance": "claude",
        "self_check": {"test_failed_first": True, "outside_scope": [], "not_done": "all",
                       "reviewer_would_flag": "-", "could_delete": "-"},
        "needs_deps": [{"ecosystem": "pypi", "package": "six", "version": "1.17.0",
                        "why": "compat"}]}))  # fmt: skip


# --- review A4: the age check reads the files the lock pins --------------------------------------

LOCK_PINNED = LOCK + (
    '\n[[package]]\nname = "six"\nversion = "1.17.0"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files/six-1.17.0.tar.gz", hash = "sha256:aaa", size = 1 }\n'
    'wheels = [\n    { url = "https://files/six-1.17.0-py3-none-any.whl", hash = "sha256:bbb", '
    "size = 1 },\n]\n"
)


def _pypi(files: dict[str, int]) -> Any:
    import httpx2

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"urls": [
            {"digests": {"sha256": h},
             "upload_time_iso_8601": (NOW - timedelta(days=d)).isoformat().replace("+00:00", "Z")}
            for h, d in files.items()]})  # fmt: skip

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_the_lock_pins_files_by_hash() -> None:
    from vigild.jobs.deps import lock_hashes

    assert lock_hashes("uv.lock", LOCK_PINNED)[("six", "1.17.0")] == frozenset({"aaa", "bbb"})


def test_a_wheel_uploaded_yesterday_to_an_old_release_is_refused() -> None:
    """Review A4: the check used the earliest upload, so a new wheel added
    to an old release passed. It reads the newest of the files the lock pins."""
    from vigild.jobs.deps import HttpRegistry, too_new

    reg = HttpRegistry(_pypi({"aaa": 400, "bbb": 1, "ccc": 1}))
    pinned = {("pypi", "six", "1.17.0"): frozenset({"aaa", "bbb"})}
    out = too_new({("pypi", "six", "1.17.0")}, reg, NOW, pinned)
    assert out and "six==1.17.0" in out[0] and "under 14 days" in out[0]
    # pinned to the old sdist only: a new file the lock doesn't use doesn't count
    old = {("pypi", "six", "1.17.0"): frozenset({"aaa"})}
    assert too_new({("pypi", "six", "1.17.0")}, reg, NOW, old) == []
    # a pinned file the registry doesn't list: no proof of age
    gone = {("pypi", "six", "1.17.0"): frozenset({"zzz"})}
    assert "no publish time" in too_new({("pypi", "six", "1.17.0")}, reg, NOW, gone)[0]


def test_an_npm_tarball_is_checked_against_its_pinned_integrity() -> None:
    import httpx2

    from vigild.jobs.deps import HttpRegistry

    doc = {"time": {"1.0.0": (NOW - timedelta(days=30)).isoformat()},
           "versions": {"1.0.0": {"dist": {"integrity": "sha512-good"}}}}  # fmt: skip
    reg = HttpRegistry(httpx2.Client(transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, json=doc))))  # fmt: skip
    assert reg.published("npm", "left-pad", "1.0.0", frozenset({"sha512-good"})) is not None
    assert reg.published("npm", "left-pad", "1.0.0", frozenset({"sha512-other"})) is None


# --- review A5 and coord 11: no build before the owner merges; no source builds ---------------


def test_no_dev_image_is_built_from_a_deps_change_before_it_merges(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    w = world
    _seed(fixture_repo)
    w.runner.cfg.repos.repos[REPO].dev_lockfiles = ["pyproject.toml", "uv.lock"]
    pm = PackageManager(NEW_PY, _lock(("six", "1.17.0")))
    w.runner.deps = DepsExecutor(
        w.runner.workspace, pm, Registry({"six": 600}), lambda: NOW, w.runner.cfg.vigild.data_path
    )
    built: list[str] = []
    real = w.runner.dev_images.ensure_fresh

    def ensure_fresh(repo: str, sha: str, **kw: Any) -> Any:
        built.append(sha)
        return real(repo, sha, **kw)

    w.runner.dev_images.ensure_fresh = ensure_fresh  # type: ignore[method-assign]
    w.standin.worker_edit = _needs_six
    parent = _approve(w)
    child = _failure_item(w, parent, "needs_deps")["body"]["deps_job"]
    (item,) = [x for x in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
               if x["job_id"] == child]  # fmt: skip
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(child).state.value == "ready"
    head = git(fixture_repo["bare"], "rev-parse", f"vigil/{child}-w1")
    assert head not in built  # nothing built from the unmerged lockfiles
    deps_pr = w.publisher.prs[-1]
    assert "only after you merge" in deps_pr["body"]


def test_deps_jobs_and_dev_images_refuse_source_builds() -> None:
    from vigild.jobs.deps import update_command

    npm = DepRequest(ecosystem="npm", package="left-pad", version="1.3.0", why="x")
    cmd = update_command([*SIX, npm])
    assert "uv add --no-sync --no-build " in cmd
    assert "pnpm add -w --lockfile-only --ignore-scripts " in cmd
    root = Path(__file__).parents[2] / "images" / "dev"
    for name in ("vigil.Dockerfile", "fixture-uv.Dockerfile", "parkagent.Dockerfile"):
        text = (root / name).read_text()
        syncs = [ln for ln in text.splitlines() if "uv sync" in ln]
        assert syncs and all("--no-build" in ln for ln in syncs), name


def test_a_dependency_that_needs_a_source_build_fails_the_deps_job_by_name(
    tmp_path: Path, fixture_repo: dict[str, Any]
) -> None:
    class NoWheel(PackageManager):
        def __call__(self, image: str, work: Path, command: str) -> tuple[int, str]:
            self.ran.append((image, [], command))
            return 2, "error: six==1.17.0 has no wheel and --no-build is set"

    ex, _ws, base = _executor(tmp_path, fixture_repo, NoWheel(NEW_PY, LOCK), {"six": 600})
    res = ex.run(REPO, base, ["pyproject.toml", "uv.lock"], SIX, "img")
    assert any("source build" in e for e in res.errors)
