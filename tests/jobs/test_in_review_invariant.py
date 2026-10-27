"""Phase 1 run finding #4 (pa-0002): a job enters `in_review` only if every task in its
current plan has an open PR. Otherwise it blocks, with one page offering replan and
abandon that names the tasks with no PR. One test per path into `in_review`."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.jobs.test_coord_loop import (  # noqa: F401
    SPEC,
    World,
    _approve,
    _failure_item,
    _flaky,
    _request,
    auth,
    plan,
    world,
)
from vigild.db import Store
from vigild.jobs.fsm import Actor, TransitionError, plan_gaps, transition
from vigild.jobs.model import JobState, Role
from vigild.provenance import Provenance

S = JobState


def _plan_of(job: str, *ids_goals: tuple[str, str]) -> str:
    """plan.md naming the given (task id, goal) workers, each on the doc and test of the
    stand-in's w1 or w2."""
    doc = json.loads(plan(job).split("```json\n")[1].split("\n```")[0])
    by_id = {w["task_id"]: w for w in doc["workers"]}
    workers = []
    for tid, goal in ids_goals:
        base = by_id.get(tid) or by_id[f"{job}-w2"]
        workers.append({**base, "task_id": tid, "goal": goal})
    head = plan(job).split("```json\n")[0]
    return (
        head + "```json\n" + json.dumps({"workers": workers, "verify": doc["verify"]}) + "\n```\n"
    )


def _tamper(w: World, job: str) -> None:
    """The pa-0002 plan: it names w2, whose PR is another piece of work."""
    w.store.add_artifact(job, "plan.md", _plan_of(job, (f"{job}-w2", "checker")), Provenance.CLAUDE)


def _page(w: World, job: str) -> dict[str, Any]:
    qs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (item,) = [q for q in qs if q["job_id"] == job and q["body"].get("kind") == "plan_without_pr"]
    return item  # type: ignore[no-any-return]


def _assert_paged(w: World, job: str, *tasks: str) -> None:
    assert w.store.get_job(job).state is S.BLOCKED
    item = _page(w, job)
    assert item["body"]["actions"] == ["replan", "abandon"]
    assert item["body"]["resumable"] is False
    assert sorted(item["body"]["tasks"]) == sorted(tasks)
    last = [e for e in w.store.events(job_id=job) if e.type == "state"][-1]
    assert last.data["to"] == "blocked" and last.data["from"] in ("building", "in_review")


def _built(w: World) -> str:
    # The invariant is about entering `in_review`. Phase 2a's review stage then runs
    # and moves a clean job on to `ready`, so it is held off here: the job stays where
    # the guard let it in.
    w.runner.review = lambda job_id: None  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state is S.IN_REVIEW
    return job


def _back_to_building(w: World, job: str) -> None:
    w.store._force_state(job, S.BUILDING)


# --- the guard ----------------------------------------------------------------------------


def test_a_valid_job_still_enters_in_review(world: World) -> None:
    w = world
    job = _built(w)
    assert plan_gaps(w.store, job) == {}


def test_path_dispatch_complete(world: World) -> None:
    w = world
    job = _built(w)
    _tamper(w, job)
    _back_to_building(w, job)
    w.runner.dispatch(job)
    _assert_paged(w, job, f"{job}-w2")


def test_the_review_stage_never_starts_for_a_plan_task_without_its_pr(world: World) -> None:
    """Phase 2a's review stage is entered only through the guard: a job whose plan
    names a task with no open PR for that work blocks before any reviewer runs."""
    w = world
    job = _approve(w)
    assert w.store.get_job(job).state is S.READY  # a valid job is reviewed
    reviewers = lambda: [s for s in w.standin.specs if s.role in (Role.REVIEWER, Role.AUDITOR)]  # noqa: E731
    before = len(reviewers())
    _tamper(w, job)
    _back_to_building(w, job)
    w.runner.dispatch(job)
    _assert_paged(w, job, f"{job}-w2")
    assert len(reviewers()) == before


def test_path_retry(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    _tamper(w, job)  # w2 holds a PR, but the plan now names other work under its id
    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    _assert_paged(w, job, f"{job}-w2")


def _hold(w: World, job: str, blocked_on: str | None = None) -> int:
    _back_to_building(w, job)
    ev: dict[str, Any] = {"reason": "budget"}
    if blocked_on:
        ev["blocked_on"] = blocked_on
    transition(w.store, job, S.BLOCKED, Actor.VIGILD, evidence=ev)
    return w.store.create_inbox_item("questions", "page", job, "held", {"resumable": True}).id


def test_path_resume(world: World) -> None:
    w = world
    job = _built(w)
    page = _hold(w, job)
    _tamper(w, job)
    r = w.client.post(f"/api/inbox/{page}/resume", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    _assert_paged(w, job, f"{job}-w2")


def test_path_deps_merged(world: World) -> None:
    w = world
    job = _built(w)
    deps = w.store.create_job(repo="o/r", base="main", title="deps", request="r", notes=[])
    page = _hold(w, job, blocked_on=deps.id)
    w.store._force_state(deps.id, S.MERGED)
    _tamper(w, job)
    r = w.client.post(f"/api/inbox/{page}/resume", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    _assert_paged(w, job, f"{job}-w2")


def test_path_reconcile_after_a_restart_resumes_through_retry(world: World) -> None:
    w = world
    job = _built(w)
    _back_to_building(w, job)
    _tamper(w, job)
    assert w.runner.reconcile()["held"] == 1  # a job mid-flight at the restart is held
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (page,) = [q for q in pages if q["job_id"] == job and "restarted" in q["title"]]
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    _assert_paged(w, job, f"{job}-w2")


def test_path_replan_that_reuses_an_id_is_refused_at_the_gate(world: World) -> None:
    """pa-0002: the amended plan names a task id that already has a PR."""
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    reuse = lambda j: (SPEC, _plan_of(j, (f"{j}-w2", "checker")))  # noqa: E731
    w.standin.coord_drafts = [reuse, reuse, reuse]
    w.standin.renumber = False  # a coord that ignores `existing_task_ids`
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is S.SPEC_PENDING
    gate = [e for e in w.store.events(job_id=job) if e.type == "spec_gate"]
    assert gate and all(f"{job}-w2 already exists" in "; ".join(e.data["errors"]) for e in gate)
    assert w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"] == []


def test_path_replan_with_fresh_ids_reaches_review(world: World) -> None:
    w = world
    w.runner.review = lambda job_id: None  # type: ignore[method-assign]  # see _built
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    w.standin.coord_drafts = [lambda j: (SPEC, _plan_of(j, (f"{j}-w3", "checker")))]
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    spec_item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    r = w.client.post(
        f"/api/inbox/{spec_item['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    assert r.status_code == 200, r.text
    print([e.data for e in w.store.events(job_id=job) if e.type in ("spec_gate", "page")])
    print(w.store.get_artifact(job, "plan.md").content[-900:])
    assert w.store.get_job(job).state is S.IN_REVIEW
    assert {t.id: t.state for t in w.store.list_tasks(job)} == {
        f"{job}-w1": "superseded",
        f"{job}-w2": "pr_open",
        f"{job}-w3": "pr_open",
    }


# --- startup reconcile: a job already stuck in in_review -----------------------------------------


def test_reconcile_pages_an_in_review_job_whose_plan_task_has_no_pr(world: World) -> None:
    w = world
    stuck = _built(w)
    fine = _built(w)
    _tamper(w, stuck)
    before = len(w.store.events(job_id=fine))
    out = w.runner.reconcile()
    assert out["held"] == 1
    _assert_paged(w, stuck, f"{stuck}-w2")
    assert w.store.get_job(fine).state is S.IN_REVIEW
    assert len(w.store.events(job_id=fine)) == before
    # a second start pages nothing new: the job is blocked now
    assert w.runner.reconcile()["held"] == 0


def test_the_owner_can_replan_or_abandon_the_page(world: World) -> None:
    w = world
    job = _built(w)
    _tamper(w, job)
    w.runner.reconcile()
    item = _page(w, job)
    r = w.client.post(f"/api/inbox/{item['id']}/abandon", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is S.ABANDONED


# --- the guard, without a runner -----------------------------------------------------------------


def _store_job(state: JobState = S.BUILDING) -> tuple[Store, str]:
    store = Store.open(":memory:")
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    store._force_state(job.id, state)
    return store, job.id


def _task_with_pr(store: Store, job: str, n: int, goal: str) -> str:
    tid = f"{job}-w{n}"
    contract = {
        "task_id": tid,
        "spec": {"goal": goal, "test_cmd": "pytest -q", "files_in_scope": ["a.md"]},
    }
    store.create_task(job, contract, Provenance.CLAUDE)
    store.update_task(tid, state="pr_open")
    sess = store.create_session(job, Role.WORKER, task_id=tid, backend_preset="claude-sonnet")
    store.update_session(sess.id, status="pr_open", pr_url="https://github.com/o/r/pull/1")
    store.add_artifact(job, "worker_report.json", "{}", Provenance.CLAUDE, task_id=tid)
    return tid


def _plan_text(job: str, *workers: tuple[int, str]) -> str:
    ws = [
        {
            "task_id": f"{job}-w{n}",
            "goal": goal,
            "why": "w",
            "value": 3,
            "risk": 1,
            "size": "XS",
            "test_cmd": "pytest -q",
            "done_when": "d",
            "files_in_scope": ["a.md"],
        }
        for n, goal in workers
    ]
    return "# Plan\n\n## Workers\n```json\n" + json.dumps({"workers": ws, "verify": []}) + "\n```\n"


def test_guard_refuses_a_plan_task_that_has_no_task() -> None:
    store, job = _store_job()
    _task_with_pr(store, job, 1, "g")
    store.add_artifact(job, "plan.md", _plan_text(job, (1, "g"), (2, "h")), Provenance.CLAUDE)
    with pytest.raises(TransitionError, match=f"{job}-w2"):
        transition(store, job, S.IN_REVIEW, Actor.VIGILD)
    assert store.get_job(job).state is S.BUILDING


def test_guard_refuses_a_plan_task_whose_task_is_other_work() -> None:
    store, job = _store_job()
    _task_with_pr(store, job, 1, "g")
    store.add_artifact(job, "plan.md", _plan_text(job, (1, "checker")), Provenance.CLAUDE)
    assert list(plan_gaps(store, job)) == [f"{job}-w1"]
    with pytest.raises(TransitionError, match=f"{job}-w1"):
        transition(store, job, S.IN_REVIEW, Actor.VIGILD)


def test_guard_passes_when_every_plan_task_has_its_pr() -> None:
    store, job = _store_job()
    _task_with_pr(store, job, 1, "g")
    _task_with_pr(store, job, 2, "h")
    store.add_artifact(job, "plan.md", _plan_text(job, (1, "g"), (2, "h")), Provenance.CLAUDE)
    assert transition(store, job, S.IN_REVIEW, Actor.VIGILD).state is S.IN_REVIEW


def test_an_older_plans_task_with_its_pr_is_fine_beside_a_newer_plan() -> None:
    store, job = _store_job()
    _task_with_pr(store, job, 1, "g")
    _task_with_pr(store, job, 3, "h")
    store.add_artifact(job, "plan.md", _plan_text(job, (3, "h")), Provenance.CLAUDE)
    assert transition(store, job, S.IN_REVIEW, Actor.VIGILD).state is S.IN_REVIEW


def test_a_held_in_review_job_cannot_resume_into_a_broken_plan() -> None:
    store, job = _store_job(S.IN_REVIEW)
    _task_with_pr(store, job, 1, "g")
    transition(store, job, S.BLOCKED, Actor.VIGILD, evidence={"reason": "x"})
    store.add_artifact(job, "plan.md", _plan_text(job, (1, "checker")), Provenance.CLAUDE)
    with pytest.raises(TransitionError, match=f"{job}-w1"):
        transition(store, job, S.IN_REVIEW, Actor.OWNER, channel="inbox")
    assert store.get_job(job).state is S.BLOCKED
