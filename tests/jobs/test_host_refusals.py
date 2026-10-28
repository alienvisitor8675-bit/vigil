"""ADR-0004 round 2: a sandbox host's refusal never goes silent or uncaught
(a session or a grader refused: pages the owner with Resume), and the deps
flow builds the new image itself, as vigild, before the parent resumes."""

from __future__ import annotations

import json
from typing import Any

from tests.conftest import auth, git
from tests.jobs.test_coord_loop import REPO, World, _approve, _failure_item, _work
from tests.jobs.test_coord_loop import StandIn as StandIn
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_deps import NEW_PY, NOW, PackageManager, Registry, _lock, _seed
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.executors.hosts import HostRefused
from vigild.jobs.deps import DepsExecutor
from vigild.jobs.model import Role


class RefusingWorkers(StandIn):
    def run(self, spec: SessionSpec, gateway_token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER:
            raise HostRefused("disk budget is over and what is left is live work")
        return super().run(spec, gateway_token, on_event)


def test_a_session_the_host_refuses_pages_the_owner_with_resume(world: World) -> None:
    w = world
    w.runner.executor = RefusingWorkers()
    job = _approve(w)
    page = _failure_item(w, job, "host_refused")
    assert page["body"]["resumable"] is True and "live work" in page["body"]["error"]
    assert "sandbox host" in page["title"]
    assert w.publisher.prs == []


def test_a_grader_the_host_refuses_pages_the_owner_with_resume(world: World) -> None:
    w = world

    class RefusingGrader:
        def run(self, *a: Any, **kw: Any) -> Any:
            raise HostRefused("build vigil-dev/x:1 first")

    w.runner.verifier.runner = RefusingGrader()  # type: ignore[assignment]
    w.standin.worker_edit = lambda spec, task: _work(spec.clone, task["spec"]["files_in_scope"][0])
    job = _approve(w)
    page = _failure_item(w, job, "host_refused")
    assert (
        page["body"]["resumable"] is True and "build vigil-dev/x:1 first" in page["body"]["error"]
    )
    assert w.publisher.prs == []


def test_a_deps_pr_merge_rebuilds_the_image_and_resumes_the_parent_with_no_owner_action(
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
        try:
            n = int(url.rsplit("/", 1)[1])
            tip = git(bare, "rev-parse", w.publisher.prs[n - 1]["head"])
            merged = git(bare, "merge-base", "--is-ancestor", tip, "main") == ""
        except Exception:
            return "open"
        return "merged" if merged else "open"

    w.runner.pr_state = pr_state
    seen: list[str | None] = []
    tries: dict[str, int] = {}

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        tid = str(spec.task_id)
        tries[tid] = tries.get(tid, 0) + 1
        seen.append(spec.image)
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
    child = _failure_item(w, parent, "needs_deps")["body"]["deps_job"]
    (item,) = [x for x in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
               if x["job_id"] == child]  # fmt: skip
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(child).state.value == "ready"
    # from here a session only looks images up, as in production: a lookup never builds
    del w.runner.dev_images.resolve, w.runner.session_images.resolve  # type: ignore[union-attr]
    built: list[str] = []
    real = w.runner.session_images.ensure_fresh  # type: ignore[union-attr]
    w.runner.session_images.ensure_fresh = lambda repo, sha, **kw: (  # type: ignore[union-attr,method-assign]
        built.append(sha) or real(repo, sha, **kw)
    )
    deps_pr = w.publisher.prs[-1]
    git(bare, "update-ref", "refs/heads/main", git(bare, "rev-parse", deps_pr["head"]))
    assert w.runner.poll_prs() == [child]  # no owner press anywhere in this test
    assert built, "vigild ran the explicit build step itself"
    assert w.store.get_job(parent).state.value == "ready"
    first, second = [i for i in seen if i][-2:]
    assert first != second  # the parent ran again on the new image key
    assert not [
        q
        for q in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
        if q["job_id"] == parent and q["body"].get("kind") == "dev_image"
    ]  # and nothing paged for a missing image


def test_any_other_lockfile_change_still_pages_with_rebuild(world: World) -> None:
    from vigild.devimage import MissingImage

    w = world
    del w.runner.dev_images.resolve, w.runner.session_images.resolve  # type: ignore[union-attr]
    try:
        w.runner.dev_images.resolve(REPO, w.runner.workspace.head_sha(REPO, "main"))
    except MissingImage:
        pass
    else:  # pragma: no cover
        raise AssertionError("resolve built an image")
    items = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (page,) = [q for q in items if q["body"].get("kind") == "dev_image"]
    assert "rebuild" in page["body"]["actions"] and page["body"]["stage"] == "missing"
