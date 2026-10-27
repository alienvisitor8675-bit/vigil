"""Fix with findings (owner spec, 2026-10-27): a reviewed PR goes back to its worker
with findings the owner chose from the review and audit, and a note.

`POST /api/inbox/{id}/fix` takes {"task_id", "findings", "note"}. It runs the 2a fix
round unchanged (same branch and PR, then the verifier, a re-review and a re-audit)
on the task's remaining cap. The workers, reviewers and auditors are scripted."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import (  # noqa: F401
    CLEAN_AUDIT,
    World,
    _approve,
    _spend,
    world,
)
from vigild.executors.base import SessionSpec
from vigild.jobs.model import JobState, Role

LOW_A = {"severity": "low", "file": "docs/feature-map.md", "line": 3,
         "claim": "the unit is missing from the row", "fix": "say req/min"}  # fmt: skip
LOW_B = {**LOW_A, "line": 9, "claim": "the heading is out of order", "fix": "move it up"}
NOTE = "Keep the change to the docs; leave the code alone."


def _low_audit(spec: SessionSpec) -> dict[str, Any]:
    """The first audit of w1 reports two low findings the reviewer missed."""
    if str(spec.task_id).endswith("w1") and len(seen) < 2:
        seen.append(1)
        found = [LOW_A, LOW_B] if len(seen) == 1 else [LOW_A]
        return {"findings": [{**f, "matched": None} for f in found], "false_findings": []}
    return CLEAN_AUDIT


seen: list[int] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    seen.clear()


def _ready(w: World) -> tuple[str, dict[str, Any]]:
    w.standin.audit = _low_audit
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    return job, _item(w, job)


def _item(w: World, job: str) -> dict[str, Any]:
    ready = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["ready"]
    (item,) = [i for i in ready if i["job_id"] == job]
    return item  # type: ignore[no-any-return]


def _fix(w: World, item: int, **body: Any) -> Any:
    return w.client.post(f"/api/inbox/{item}/fix", headers=auth(w.tokens.admin), json=body)


def _workers(w: World, task: str) -> list[SessionSpec]:
    return [s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id).endswith(task)]


def _states(w: World, job: str) -> list[tuple[str, str]]:
    return [(e.data["from"], e.data["to"]) for e in w.store.events(job_id=job) if e.type == "state"]


def test_the_ready_item_lists_each_tasks_selectable_findings(world: World) -> None:
    _, item = _ready(world)
    pr = next(p for p in item["body"]["prs"] if p["task_id"].endswith("w1"))
    assert [f["id"] for f in pr["selectable"]] == ["audit:0", "audit:1"]
    assert [f["index"] for f in pr["selectable"]] == [0, 1]
    assert pr["selectable"][0]["claim"] == LOW_A["claim"]
    assert item["body"]["actions"] == ["fix"]


def test_fix_with_two_audit_findings_and_a_note(world: World) -> None:
    w = world
    job, item = _ready(w)
    task = f"{job}-w1"
    before = len(w.store.events(job_id=job))
    r = _fix(w, item["id"], task_id=task, findings=["audit:0", "audit:1"], note=NOTE)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == job

    # the standard fix round: the same worker, branch and PR, the packet fenced
    first, fix = _workers(w, "w1")
    assert "Fix round 1" in fix.packet
    assert "the unit is missing from the row" in fix.packet
    assert "the heading is out of order" in fix.packet
    assert "<<<UNTRUSTED review-findings" in fix.packet
    assert "<<<UNTRUSTED owner-note" in fix.packet and NOTE in fix.packet
    assert sorted(p["head"] for p in w.publisher.prs) == [f"vigil/{job}-w1", f"vigil/{job}-w2"]

    # ready -> building -> in_review -> ready, every move an event
    assert _states(w, job)[-3:] == [
        ("ready", "building"),
        ("building", "in_review"),
        ("in_review", "ready"),
    ]
    assert w.store.get_job(job).state.value == "ready"
    t = w.store.get_task(task)
    assert t.state == "reviewed" and t.contract["review_rounds"] == 1

    # the verifier, a re-review and a re-audit ran after the worker
    rv = json.loads(w.store.get_artifact(job, "review.json", task).content)  # type: ignore[union-attr]
    assert rv["round"] == 2
    roles = [s.role for s in w.standin.specs if str(s.task_id) == task]
    assert roles[-2:] == [Role.REVIEWER, Role.AUDITOR]

    # the old item is resolved; a new one is open
    new = _item(w, job)
    assert new["id"] != item["id"]
    assert w.store.get_inbox_item(item["id"]).status == "resolved"
    assert len(w.store.events(job_id=job)) > before
    (ev,) = [e for e in w.store.events(job_id=job) if e.type == "fix_with_findings"]
    assert ev.data["findings"] == ["audit:0", "audit:1"] and ev.data["round"] == 1
    assert ev.provenance == "human"
    note = w.store.get_artifact(job, "fix_note.json", task)
    assert note is not None and note.provenance == "human"
    assert json.loads(note.content)["note"] == NOTE
    assert first.session_id != fix.session_id


def test_findings_can_be_chosen_by_index_without_a_note(world: World) -> None:
    w = world
    job, item = _ready(w)
    r = _fix(w, item["id"], task_id=f"{job}-w1", findings=[1])
    assert r.status_code == 200, r.text
    fix = _workers(w, "w1")[1]
    assert "the heading is out of order" in fix.packet
    assert "the unit is missing from the row" not in fix.packet
    assert "owner-note" not in fix.packet


def test_it_refuses_when_too_little_of_the_cap_is_left(world: World) -> None:
    w = world
    job, item = _ready(w)
    task = f"{job}-w1"
    rid = w.budget.reserve(session_id=None, task_id=task, amount=1.97, purpose="worker")
    w.budget.settle(rid, 1.97)
    r = _fix(w, item["id"], task_id=task, findings=["audit:0"])
    assert r.status_code == 409, r.text
    assert "cap" in r.json()["detail"]
    assert w.store.get_job(job).state.value == "ready"
    assert w.store.get_inbox_item(item["id"]).status == "open"
    assert len(_workers(w, "w1")) == 1


def test_the_round_runs_on_what_is_left_of_the_cap(world: World) -> None:
    w = world
    w.standin.on_session = _spend(w, 15_000)  # $0.30 per worker session
    job, item = _ready(w)
    r = _fix(w, item["id"], task_id=f"{job}-w1", findings=["audit:0"])
    assert r.status_code == 200, r.text
    first, fix = _workers(w, "w1")
    assert first.max_budget_usd == pytest.approx(2.0)
    assert fix.max_budget_usd == pytest.approx(1.70)


def test_a_merged_pr_is_refused(world: World) -> None:
    w = world
    job, item = _ready(w)
    task = f"{job}-w1"
    w.store.update_task(task, state="merged")
    r = _fix(w, item["id"], task_id=task, findings=["audit:0"])
    assert r.status_code == 409, r.text
    assert "reviewed" in r.json()["detail"] or "merged" in r.json()["detail"]
    assert w.store.get_job(job).state.value == "ready"


def test_a_pr_merged_on_github_since_the_last_poll_is_refused(world: World) -> None:
    w = world
    job, item = _ready(w)
    w.runner.pr_state = lambda repo, url: "merged"
    r = _fix(w, item["id"], task_id=f"{job}-w1", findings=["audit:0"])
    assert r.status_code == 409, r.text
    assert "merged" in r.json()["detail"]
    assert w.store.get_job(job).state.value == "ready"


def test_findings_of_another_task_are_refused(world: World) -> None:
    w = world
    job, item = _ready(w)
    # w2 has no findings: w1's ids are not its own, nor is an index past its list
    for chosen in (["audit:0"], [0], ["review:0"], ["audit:7"], ["nonsense"]):
        r = _fix(w, item["id"], task_id=f"{job}-w2", findings=chosen)
        assert r.status_code == 409, (chosen, r.text)
    r = _fix(w, item["id"], task_id="pa-9999-w1", findings=["audit:0"])
    assert r.status_code == 409, r.text
    assert w.store.get_job(job).state.value == "ready"
    assert len(_workers(w, "w2")) == 1


def test_a_job_in_the_wrong_state_is_refused(world: World) -> None:
    w = world
    job, item = _ready(w)
    w.store._write_state(job, JobState.MERGED, None)
    r = _fix(w, item["id"], task_id=f"{job}-w1", findings=["audit:0"])
    assert r.status_code == 409, r.text
    assert len(_workers(w, "w1")) == 1


def test_an_item_that_is_not_a_ready_item_is_refused(world: World) -> None:
    w = world
    job, item = _ready(w)
    w.store.resolve_inbox_item(item["id"], "owner")
    r = _fix(w, item["id"], task_id=f"{job}-w1", findings=["audit:0"])
    assert r.status_code == 409, r.text
    assert _fix(w, 99999, task_id=f"{job}-w1", findings=["audit:0"]).status_code == 404


def test_the_route_is_admin_only(world: World) -> None:
    w = world
    job, item = _ready(w)
    body = {"task_id": f"{job}-w1", "findings": ["audit:0"]}
    for token in (w.tokens.read, w.tokens.heartbeat):
        r = w.client.post(f"/api/inbox/{item['id']}/fix", headers=auth(token), json=body)
        assert r.status_code == 403
    assert w.client.post(f"/api/inbox/{item['id']}/fix", json=body).status_code in (401, 403)
    assert len(_workers(w, "w1")) == 1


def test_the_note_is_limited_to_8kb_and_must_be_text(world: World) -> None:
    w = world
    job, item = _ready(w)
    task = f"{job}-w1"
    assert _fix(w, item["id"], task_id=task, findings=[0], note="x" * 8193).status_code == 413
    assert _fix(w, item["id"], task_id=task, findings=[0], note=7).status_code == 422
    assert _fix(w, item["id"], task_id=task, findings=[0], note="  ").status_code == 422
    assert _fix(w, item["id"], task_id=task, findings=[], note="n").status_code == 422
    assert _fix(w, item["id"], task_id=task, findings="audit:0").status_code == 422
    assert _fix(w, item["id"], findings=[0]).status_code == 422
    assert w.store.get_job(job).state.value == "ready"
    ok = _fix(w, item["id"], task_id=task, findings=[0], note="x" * 8192)
    assert ok.status_code == 200, ok.text


def test_review_rounds_keep_counting_so_the_2a_limits_apply(world: World) -> None:
    w = world
    job, item = _ready(w)
    task = f"{job}-w1"
    assert _fix(w, item["id"], task_id=task, findings=[0]).status_code == 200
    assert w.store.get_task(task).contract["review_rounds"] == 1
    # the re-audit found one thing again; the second owner round trips rule 5
    again = _item(w, job)
    assert again["id"] != item["id"]
    assert _fix(w, again["id"], task_id=task, findings=["audit:0"]).status_code == 200
    t = w.store.get_task(task)
    assert t.contract["review_rounds"] == 2
    assert t.tier_reason is not None and t.tier_reason.startswith("rule5")
    # nothing is left to choose after a clean re-audit
    last = _item(w, job)
    assert _fix(w, last["id"], task_id=task, findings=[0]).status_code == 409


def test_only_the_owner_from_the_inbox_moves_ready_to_building(world: World) -> None:
    from vigild.jobs.fsm import Actor, TransitionError, transition

    w = world
    job, _ = _ready(w)
    evidence = {"action": "fix", "findings": {f"{job}-w1": 1}}
    for actor, channel in ((Actor.VIGILD, None), (Actor.COORD, None), (Actor.OWNER, "api")):
        with pytest.raises(TransitionError):
            transition(w.store, job, JobState.BUILDING, actor, channel=channel, evidence=evidence)
    with pytest.raises(TransitionError):  # no findings, no fix
        transition(w.store, job, JobState.BUILDING, Actor.OWNER, channel="inbox",
                   evidence={"action": "fix"})  # fmt: skip
    assert w.store.get_job(job).state.value == "ready"
