"""A job in `spec_pending` with no open inbox item (its coord failed before writing a
spec, and the failure page was resolved) is paged once, with replan and abandon; and a
review never runs on a task whose PR has merged."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

from typing import Any

from tests.jobs.test_coord_loop import (  # noqa: F401
    World,
    _approve,
    _request,
    auth,
    world,
)
from vigild.jobs.model import JobState, Role
from vigild.provenance import Provenance

S = JobState


def _open(w: World, job: str) -> list[dict[str, Any]]:
    inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()
    return [q for q in inbox["questions"] + inbox["specs"] if q["job_id"] == job]


def _stranded(w: World) -> str:
    """The pa-0001 shape: spec_pending, a failed coord, its page resolved."""
    w.runner.run_coord = lambda j: None  # type: ignore[method-assign]  # the coord never drafts
    job = _request(w)
    coord = w.store.create_session(job, Role.COORD, backend_preset="claude-opus")
    w.store.update_session(coord.id, status="failed")
    page = w.runner.jobs.page(  # type: ignore[union-attr]
        job, f"{job}: the coord failed", {"actions": ["retry", "abandon"]}, resumable=True
    )
    w.store.resolve_inbox_item(page.id, "owner")
    del w.runner.run_coord  # the real coord again
    assert _open(w, job) == []
    return job


def test_a_stranded_draft_is_paged_once_and_can_be_abandoned(world: World) -> None:
    w = world
    job = _stranded(w)
    w.runner.reconcile()
    (page,) = _open(w, job)
    assert page["body"]["actions"] == ["replan", "abandon"]
    assert page["title"] == f"{job} is waiting for a spec that was never drafted"
    w.runner.reconcile()  # a second pass adds nothing
    assert w.runner.page_stranded_drafts() == []
    assert len(_open(w, job)) == 1
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is S.ABANDONED


def test_replan_from_that_page_runs_the_coord_again(world: World) -> None:
    w = world
    job = _stranded(w)
    w.runner.page_stranded_drafts()
    (page,) = _open(w, job)
    r = w.client.post(f"/api/inbox/{page['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert [c[0] for c in w.standin.calls] == ["coord"]
    assert w.store.get_job(job).state is S.SPEC_PENDING
    assert w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]


def test_a_draft_with_an_open_spec_item_is_not_paged(world: World) -> None:
    w = world
    job = _request(w)  # the stand-in coord drafts: a spec item is open
    before = _open(w, job)
    assert [q["kind"] for q in before] == ["spec"]
    assert w.runner.page_stranded_drafts() == []
    w.runner.reconcile()
    assert _open(w, job) == before


def test_a_run_in_flight_and_a_young_job_are_not_paged_by_the_sweep(world: World) -> None:
    w = world
    job = _stranded(w)
    assert w.runner.page_stranded_drafts(grace_seconds=600) == []  # just created
    w.runner._running.add(job)
    assert w.runner.page_stranded_drafts() == []  # a coord is running on it
    w.runner._running.discard(job)
    assert w.runner.page_stranded_drafts() == [job]


# --- never review a merged task ------------------------------------------------------------


def _sessions(w: World, role: Role) -> list[Any]:
    return [s for s in w.store.list_sessions() if s.role is role]


def _in_review(w: World) -> str:
    w.runner.review = lambda job_id: None  # type: ignore[method-assign]  # held off, see below
    job = _approve(w)
    assert w.store.get_job(job).state is S.IN_REVIEW
    del w.runner.review
    return job


def _reserved(w: World) -> list[list[str]]:
    return [e.data["tasks"] for e in w.store.events() if e.type == "review_stage_reserved"]


def test_after_a_restart_only_the_open_task_is_reviewed(world: World) -> None:
    w = world
    job = _in_review(w)
    merged, open_ = sorted(t.id for t in w.store.list_tasks(job))
    w.store.update_task(merged, state="merged")
    assert w.runner.reconcile()["requeued"] == 1
    w.runner.start_deferred()
    assert _reserved(w) == [[open_]]
    reviewed = {s.task_id for s in _sessions(w, Role.REVIEWER)}
    assert reviewed == {open_}
    assert {s.task_id for s in _sessions(w, Role.AUDITOR)} <= {open_}


def test_a_pr_the_poll_saw_merged_is_not_reviewed_either(world: World) -> None:
    w = world
    job = _in_review(w)
    merged, open_ = sorted(t.id for t in w.store.list_tasks(job))
    # the poll's record is an event; the task row is still pr_open (a lagging write)
    w.store.add_event("pr_merged", {"pr_url": "u"}, Provenance.HUMAN, job_id=job, task_id=merged)
    w.runner.review(job)
    assert _reserved(w) == [[open_]]
    assert {s.task_id for s in _sessions(w, Role.REVIEWER)} == {open_}


def test_all_tasks_merged_reserves_nothing_and_runs_no_session(world: World) -> None:
    w = world
    job = _in_review(w)
    for t in w.store.list_tasks(job):
        w.store.update_task(t.id, state="merged")
    w.runner.reconcile()
    w.runner.start_deferred()
    assert _reserved(w) == []
    assert _sessions(w, Role.REVIEWER) == [] and _sessions(w, Role.AUDITOR) == []
    assert w.budget.open_reservation_ids() == []
    assert w.store.get_job(job).state is S.IN_REVIEW  # the poll's merged path moves it on
