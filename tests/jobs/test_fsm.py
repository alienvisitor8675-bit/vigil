"""The job state machine: one test per transition, plus the illegal ones."""

from __future__ import annotations

import json

import pytest

from vigild.db import Store
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import JobState, Role
from vigild.provenance import Provenance

S = JobState


@pytest.fixture
def store() -> Store:
    return Store.open(":memory:")


def _job(store: Store, state: JobState = S.SPEC_PENDING) -> str:
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    if state is not S.SPEC_PENDING:
        store._force_state(job.id, state)  # test-only shortcut
    return job.id


def _spec_ready(store: Store, job_id: str) -> int:
    store.add_artifact(job_id, "spec.md", "# spec", Provenance.CLAUDE)
    store.add_artifact(job_id, "plan.md", "# plan", Provenance.CLAUDE)
    store.add_artifact(
        job_id, "spec_gate.json", json.dumps({"ok": True, "errors": []}), Provenance.HUMAN
    )
    return store.create_inbox_item("specs", "spec", job_id, "approve?", {}).id


def _worker(store: Store, job_id: str, *, pr: bool, report: bool) -> str:
    task = store.create_task(job_id, {"task_id": f"{job_id}-w1"}, Provenance.CLAUDE)
    store.route_task(
        task.id, tier="claude-sonnet", tier_reason="rule 9", backend_preset="claude-sonnet"
    )
    sess = store.create_session(
        job_id, Role.WORKER, task_id=task.id, backend_preset="claude-sonnet"
    )
    if pr:
        store.update_session(sess.id, status="pr_open", pr_url="https://github.com/o/r/pull/1")
    if report:
        store.add_artifact(job_id, "worker_report.json", "{}", Provenance.CLAUDE, task_id=task.id)
    return task.id


# spec_pending -> approved
def test_approve_from_inbox_by_owner(store: Store) -> None:
    jid = _job(store)
    item = _spec_ready(store, jid)
    job = transition(
        store, jid, S.APPROVED, Actor.OWNER, channel="inbox", evidence={"inbox_item_id": item}
    )
    assert job.state is S.APPROVED
    ev = store.events(job_id=jid)[-1]
    assert ev.type == "state" and ev.data["to"] == "approved" and ev.data["actor"] == "owner"


def test_approve_refused_for_anyone_but_the_owner(store: Store) -> None:
    jid = _job(store)
    item = _spec_ready(store, jid)
    for actor in (Actor.VIGILD, Actor.COORD):
        with pytest.raises(TransitionError, match="owner"):
            transition(store, jid, S.APPROVED, actor, evidence={"inbox_item_id": item})


def test_approve_refused_outside_the_inbox(store: Store) -> None:
    jid = _job(store)
    item = _spec_ready(store, jid)
    with pytest.raises(TransitionError, match="inbox"):
        transition(
            store, jid, S.APPROVED, Actor.OWNER, channel="talk", evidence={"inbox_item_id": item}
        )


def test_approve_refused_without_a_passing_spec_gate(store: Store) -> None:
    jid = _job(store)
    item = store.create_inbox_item("specs", "spec", jid, "approve?", {}).id
    with pytest.raises(TransitionError, match=r"spec\.md"):
        transition(
            store, jid, S.APPROVED, Actor.OWNER, channel="inbox", evidence={"inbox_item_id": item}
        )


# approved -> planning
def test_approved_to_planning(store: Store) -> None:
    jid = _job(store, S.APPROVED)
    assert transition(store, jid, S.PLANNING, Actor.VIGILD).state is S.PLANNING


# planning -> building
def test_planning_to_building_needs_routed_worker_tasks(store: Store) -> None:
    jid = _job(store, S.PLANNING)
    with pytest.raises(TransitionError, match="worker"):
        transition(store, jid, S.BUILDING, Actor.VIGILD)
    _worker(store, jid, pr=False, report=False)
    assert transition(store, jid, S.BUILDING, Actor.VIGILD).state is S.BUILDING


# building -> in_review
def test_building_to_in_review_needs_prs_and_reports(store: Store) -> None:
    jid = _job(store, S.BUILDING)
    _worker(store, jid, pr=True, report=False)
    with pytest.raises(TransitionError, match="worker_report"):
        transition(store, jid, S.IN_REVIEW, Actor.VIGILD)
    jid2 = _job(store, S.BUILDING)
    _worker(store, jid2, pr=False, report=True)
    with pytest.raises(TransitionError, match="PR"):
        transition(store, jid2, S.IN_REVIEW, Actor.VIGILD)
    jid3 = _job(store, S.BUILDING)
    _worker(store, jid3, pr=True, report=True)
    assert transition(store, jid3, S.IN_REVIEW, Actor.VIGILD).state is S.IN_REVIEW


# in_review -> building (findings become a fix task)
def test_in_review_back_to_building_needs_findings(store: Store) -> None:
    jid = _job(store, S.IN_REVIEW)
    with pytest.raises(TransitionError, match="findings"):
        transition(store, jid, S.BUILDING, Actor.VIGILD)
    job = transition(
        store, jid, S.BUILDING, Actor.VIGILD, evidence={"findings": [{"severity": "high"}]}
    )
    assert job.state is S.BUILDING


# in_review -> ready
def _review(tid: str, jid: str, **kw: object) -> str:
    from vigild.contract.models import CHECKLIST

    doc = {
        "verdict": "clean", "findings": [], "checklist": {c: True for c in CHECKLIST},
        "task_id": tid, "job_id": jid, "head": "a" * 40, "shape": "docs", "rung": 0,
        "round": 1, "audit_required": False, "reviewer": "claude-sonnet",
        "provenance": "claude", **kw,
    }  # fmt: skip
    return json.dumps(doc)


def test_in_review_to_ready_needs_clean_reviews(store: Store) -> None:
    jid = _job(store, S.IN_REVIEW)
    tid = _worker(store, jid, pr=True, report=True)
    with pytest.raises(TransitionError, match="review"):
        transition(store, jid, S.READY, Actor.VIGILD)
    # phase 2a, item 1: an empty review.json fails the stage gate
    store.add_artifact(jid, "review.json", "{}", Provenance.CLAUDE, task_id=tid)
    with pytest.raises(TransitionError, match=r"review\.json is invalid"):
        transition(store, jid, S.READY, Actor.VIGILD)
    medium = {"severity": "medium", "file": "a.py", "line": 1, "claim": "c", "fix": "f"}
    store.add_artifact(
        jid, "review.json", _review(tid, jid, verdict="changes", findings=[medium]),
        Provenance.CLAUDE, task_id=tid,
    )  # fmt: skip
    with pytest.raises(TransitionError, match="not clean"):
        transition(store, jid, S.READY, Actor.VIGILD)
    # a review sampled for audit needs the audit of the same head
    store.add_artifact(
        jid, "review.json", _review(tid, jid, audit_required=True), Provenance.CLAUDE, task_id=tid
    )
    with pytest.raises(TransitionError, match=r"audit\.json"):
        transition(store, jid, S.READY, Actor.VIGILD)
    audit = {
        "findings": [{**medium, "matched": None}], "false_findings": [], "task_id": tid,
        "job_id": jid, "head": "a" * 40, "shape": "docs", "rung": 0, "auditor": "claude-opus",
        "provenance": "claude",
    }  # fmt: skip
    store.add_artifact(jid, "audit.json", json.dumps(audit), Provenance.CLAUDE, task_id=tid)
    with pytest.raises(TransitionError, match="missed"):
        transition(store, jid, S.READY, Actor.VIGILD)
    audit["findings"] = []
    store.add_artifact(jid, "audit.json", json.dumps(audit), Provenance.CLAUDE, task_id=tid)
    assert transition(store, jid, S.READY, Actor.VIGILD).state is S.READY


# ready -> merged
def test_ready_to_merged_needs_merge_evidence(store: Store) -> None:
    jid = _job(store, S.READY)
    with pytest.raises(TransitionError, match="merge"):
        transition(store, jid, S.MERGED, Actor.VIGILD)
    job = transition(store, jid, S.MERGED, Actor.VIGILD, evidence={"merge_commits": ["abc123"]})
    assert job.state is S.MERGED


# merged -> verifying
def test_merged_to_verifying(store: Store) -> None:
    jid = _job(store, S.MERGED)
    assert transition(store, jid, S.VERIFYING, Actor.VIGILD).state is S.VERIFYING


# verifying -> done
def test_verifying_to_done_needs_a_clean_verify_record(store: Store) -> None:
    jid = _job(store, S.VERIFYING)
    with pytest.raises(TransitionError, match="verify"):
        transition(store, jid, S.DONE, Actor.VIGILD)
    store.add_artifact(jid, "verify.json", json.dumps({"clean": True}), Provenance.HUMAN)
    assert transition(store, jid, S.DONE, Actor.VIGILD).state is S.DONE


# * -> blocked / escalated, and back
@pytest.mark.parametrize(
    "start", [s for s in JobState if s not in (S.DONE, S.ABANDONED, S.BLOCKED, S.ESCALATED)]
)
@pytest.mark.parametrize("to", [S.BLOCKED, S.ESCALATED])
def test_blocked_and_escalated_reachable_from_every_live_state(
    store: Store, start: JobState, to: JobState
) -> None:
    jid = _job(store, start)
    with pytest.raises(TransitionError, match="reason"):
        transition(store, jid, to, Actor.VIGILD)
    job = transition(store, jid, to, Actor.VIGILD, evidence={"reason": "budget"})
    assert job.state is to and job.blocked_from is start


def test_resume_returns_to_the_state_it_left_owner_only(store: Store) -> None:
    jid = _job(store, S.BUILDING)
    transition(store, jid, S.BLOCKED, Actor.VIGILD, evidence={"reason": "budget"})
    with pytest.raises(TransitionError, match="owner"):
        transition(store, jid, S.BUILDING, Actor.VIGILD)
    with pytest.raises(TransitionError):
        transition(store, jid, S.READY, Actor.OWNER, channel="inbox")
    job = transition(store, jid, S.BUILDING, Actor.OWNER, channel="inbox")
    assert job.state is S.BUILDING and job.blocked_from is None


def test_done_is_terminal(store: Store) -> None:
    jid = _job(store, S.DONE)
    with pytest.raises(TransitionError):
        transition(store, jid, S.BLOCKED, Actor.VIGILD, evidence={"reason": "x"})


@pytest.mark.parametrize(
    "start,to",
    [
        (S.SPEC_PENDING, S.PLANNING),
        (S.SPEC_PENDING, S.BUILDING),
        (S.APPROVED, S.BUILDING),
        (S.BUILDING, S.READY),
        (S.BUILDING, S.MERGED),
        (S.READY, S.DONE),
        (S.MERGED, S.DONE),
    ],
)
def test_skipping_a_stage_is_illegal(store: Store, start: JobState, to: JobState) -> None:
    jid = _job(store, start)
    with pytest.raises(TransitionError, match="no transition"):
        transition(store, jid, to, Actor.OWNER, channel="inbox", force=True)


def test_force_is_owner_from_inbox_only_and_logged(store: Store) -> None:
    jid = _job(store, S.VERIFYING)
    with pytest.raises(TransitionError, match="override"):
        transition(store, jid, S.DONE, Actor.VIGILD, force=True)
    job = transition(store, jid, S.DONE, Actor.OWNER, channel="inbox", force=True)
    assert job.state is S.DONE
    assert store.events(job_id=jid)[-1].data["override"] is True


def test_force_never_skips_approval(store: Store) -> None:
    jid = _job(store)
    with pytest.raises(TransitionError):
        transition(store, jid, S.APPROVED, Actor.OWNER, channel="inbox", force=True)


def test_in_review_reads_the_newest_session_by_sequence(store: Store) -> None:
    """Nit (review r1): sessions made in the same second sort by insertion."""
    jid = _job(store, S.BUILDING)
    tid = _worker(store, jid, pr=True, report=True)
    later = store.create_session(jid, Role.WORKER, task_id=tid, backend_preset="claude-sonnet")
    store.update_session(later.id, status="failed")
    with pytest.raises(TransitionError, match="PR"):
        transition(store, jid, S.IN_REVIEW, Actor.VIGILD)


# review round 2, N5: out of a hold, the owner may also re-plan or abandon
def test_a_held_build_can_be_replanned_or_abandoned_by_the_owner_only(store: Store) -> None:
    for to, action in ((S.SPEC_PENDING, "replan"), (S.ABANDONED, "abandon")):
        jid = _job(store, S.BUILDING)
        transition(store, jid, S.BLOCKED, Actor.VIGILD, evidence={"reason": "worker failed"})
        with pytest.raises(TransitionError):
            transition(store, jid, to, Actor.OWNER, channel="inbox")  # no action named
        with pytest.raises(TransitionError, match="owner"):
            transition(store, jid, to, Actor.VIGILD, evidence={"action": action})
        with pytest.raises(TransitionError):
            transition(
                store, jid, S.READY, Actor.OWNER, channel="inbox", evidence={"action": action}
            )
        job = transition(store, jid, to, Actor.OWNER, channel="inbox", evidence={"action": action})
        assert job.state is to


def test_abandoned_is_terminal_and_reachable_from_spec_pending(store: Store) -> None:
    jid = _job(store, S.SPEC_PENDING)
    with pytest.raises(TransitionError):
        transition(store, jid, S.ABANDONED, Actor.VIGILD, evidence={"action": "abandon"})
    transition(
        store, jid, S.ABANDONED, Actor.OWNER, channel="inbox", evidence={"action": "abandon"}
    )
    with pytest.raises(TransitionError, match="no transition out"):
        transition(store, jid, S.BLOCKED, Actor.VIGILD, evidence={"reason": "x"})


# owner direction, round 3: a task blocked on a dependency waits for its deps job
def test_a_job_blocked_on_a_deps_job_resumes_only_after_it_merges(store: Store) -> None:
    parent = _job(store, S.BUILDING)
    deps = _job(store, S.BUILDING)
    transition(
        store, parent, S.BLOCKED, Actor.VIGILD,
        evidence={"reason": "needs_deps", "blocked_on": deps},
    )  # fmt: skip
    assert store.get_job(parent).blocked_on == deps
    with pytest.raises(TransitionError, match=f"waiting on {deps}"):
        transition(store, parent, S.BUILDING, Actor.OWNER, channel="inbox")
    store._force_state(deps, S.MERGED)
    job = transition(store, parent, S.BUILDING, Actor.OWNER, channel="inbox")
    assert job.state is S.BUILDING and job.blocked_on is None


def test_a_job_blocked_on_deps_can_still_be_abandoned(store: Store) -> None:
    parent = _job(store, S.BUILDING)
    deps = _job(store, S.BUILDING)
    transition(
        store, parent, S.BLOCKED, Actor.VIGILD,
        evidence={"reason": "needs_deps", "blocked_on": deps},
    )  # fmt: skip
    job = transition(
        store, parent, S.ABANDONED, Actor.OWNER, channel="inbox", evidence={"action": "abandon"}
    )
    assert job.state is S.ABANDONED


def test_blocked_on_must_name_a_job(store: Store) -> None:
    parent = _job(store, S.BUILDING)
    with pytest.raises(TransitionError, match="no job"):
        transition(
            store, parent, S.BLOCKED, Actor.VIGILD,
            evidence={"reason": "needs_deps", "blocked_on": "xx-9999"},
        )  # fmt: skip
