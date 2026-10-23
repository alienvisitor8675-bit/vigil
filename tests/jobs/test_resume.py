"""Checkpoint and resume (plan 2a, item 3): a crashed or killed worker resumes
from its last commit with its transcript and what's left of its budget; a
vigild restart re-queues work that's safe to resume and blocks the rest; a
re-plan resumes the coord."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import auth, git
from tests.harness import checkpoint_bundle
from tests.jobs.test_coord_loop import (
    TESTS,
    World,
    _approve,
    _failure_item,
    _flaky,
    _request,
    _spend,
    transcript_lines,
)
from tests.jobs.test_coord_loop import world as world
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.jobs.model import Role


def _workers(w: World, task: str) -> list[SessionSpec]:
    return [s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id).endswith(task)]


def test_a_killed_worker_resumes_from_its_last_commit_within_its_remainder(
    world: World,
) -> None:
    w = world
    w.standin.on_session = _spend(w, 15_000)  # $0.30 per worker session
    real = w.standin.run
    seen: dict[str, Any] = {}

    def run(spec: SessionSpec, token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER and str(spec.task_id).endswith("w1") and "killed" not in seen:
            seen["killed"] = spec.session_id
            w.standin.specs.append(spec)
            w.standin.on_session(spec)  # type: ignore[misc]
            task = json.loads((spec.clone / ".vigil" / "task.json").read_text())
            test, text = TESTS["docs/feature-map.md"]
            (spec.clone / test).parent.mkdir(parents=True, exist_ok=True)
            (spec.clone / test).write_text(text)  # half the work: the test, no doc yet
            head = checkpoint_bundle(
                spec.clone, spec.session_dir / "out", branch=task["branch"],
                base_sha=task["base_sha"],
            )  # fmt: skip
            on_event({"type": "transcript", "data": transcript_lines(spec)})
            on_event({"type": "checkpoint", "data": {"head": head, "turns": 1}})
            return ExecOutcome("failed", 137, {}, "killed")  # no result line: docker kill
        if spec.role is Role.WORKER and spec.resume is not None:
            seen["resumed_with_test"] = (spec.clone / "tests/test_feature_map.py").is_file()
        return real(spec, token, on_event)

    w.standin.run = run  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    killed, resumed = _workers(w, "w1")
    assert w.store.get_session(killed.session_id).status == "crashed"
    # its last commit: the checkpoint, imported into the vault and checked out
    ckpt = w.store.get_session(killed.session_id).detail["checkpoint"]
    assert seen["resumed_with_test"] is True
    assert git(resumed.clone, "merge-base", "--is-ancestor", ckpt, "HEAD") == ""
    # its transcript, its turn count, and no more than what's left of its cap
    assert resumed.resume is not None and resumed.resume.from_session == killed.session_id
    assert resumed.max_turns == 60 - 1
    assert resumed.max_budget_usd == pytest.approx(2.0 - 0.30)
    held = w.store.q("SELECT amount_usd FROM reservations WHERE task_id=? ORDER BY id",
                     (f"{job}-w1",))  # fmt: skip
    assert [r["amount_usd"] for r in held] == [2.0, pytest.approx(1.70)]
    assert w.runner.remaining(f"{job}-w1")[1] == pytest.approx(0.60)
    assert sorted(p["head"] for p in w.publisher.prs) == [f"vigil/{job}-w1", f"vigil/{job}-w2"]


def test_a_worker_that_keeps_crashing_stops_at_the_cap(world: World) -> None:
    w = world

    def run(spec: SessionSpec, token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER and str(spec.task_id).endswith("w1"):
            w.standin.specs.append(spec)
            return ExecOutcome("failed", 137, {}, "killed")
        return real(spec, token, on_event)

    real = w.standin.run
    w.standin.run = run  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    cap = w.runner.cfg.vigild.scheduler.resume_attempts
    assert len(_workers(w, "w1")) == cap + 1  # the first, then `cap` resumes
    item = _failure_item(w, job)
    assert "crashed" in json.dumps(item["body"]["tasks"])


class Died(BaseException):
    """vigild dying under a running session."""


def _die_in_build(w: World) -> str:
    real = w.standin.run
    dying = [True]

    def run(spec: SessionSpec, token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER and dying[0]:
            w.standin.specs.append(spec)
            on_event({"type": "transcript", "data": transcript_lines(spec)})
            raise Died
        return real(spec, token, on_event)

    w.standin.run = run  # type: ignore[method-assign]
    _request(w)
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][-1]
    assert w.runner.jobs is not None
    with pytest.raises(Died):  # straight through the service: vigild itself dies
        w.runner.jobs.approve(item["id"])  # type: ignore[attr-defined]
    dying[0] = False
    w.standin.run = real  # type: ignore[method-assign]
    job = [j.id for j in w.store.list_jobs() if j.state.value == "building"][-1]
    return job


def test_a_restart_requeues_the_safe_job_and_blocks_the_unsafe_one(world: World) -> None:
    w = world
    safe = _die_in_build(w)
    unsafe = _die_in_build(w)
    # the unsafe job's image key changed since its workers started
    for s in w.store.list_sessions(unsafe):
        if s.role is Role.WORKER:
            w.store.update_session(s.id, detail={"image": "vigil-session/fixture:0000000000000000"})
    out = w.runner.reconcile()
    assert out["requeued"] == 1 and out["held"] == 1
    assert w.store.get_job(unsafe).state.value == "blocked"
    assert w.store.get_job(safe).state.value == "building"
    assert w.runner.deferred == [safe]
    crashed = [s for s in w.store.list_sessions(safe) if s.role is Role.WORKER]
    assert {s.status for s in crashed} == {"crashed"}
    assert {s.status for s in w.store.list_sessions(unsafe) if s.role is Role.WORKER} == {"failed"}
    # once the gateway is up, the safe job resumes its workers and finishes
    assert w.runner.start_deferred() == 1
    assert w.store.get_job(safe).state.value == "ready"
    resumed = [s for s in w.standin.specs if s.job_id == safe and s.role is Role.WORKER][-2:]
    assert all(s.resume is not None for s in resumed)
    assert {s.resume.from_session for s in resumed if s.resume} == {s.id for s in crashed}


def test_a_replan_resumes_the_coord(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    first, second = [s for s in w.standin.specs if s.role is Role.COORD]
    assert first.resume is None
    assert second.resume is not None and second.resume.from_session == first.session_id
    assert "outside scope" in second.packet  # the failure, in the new packet


def test_a_second_crash_resumes_with_both_sessions_context_and_turns(world: World) -> None:
    """2a round 2, review B9: a resumed session's stored transcript carries
    the history it forked (seen live: the second session's store held the
    first's words), so a second resume keeps the first session's context, and
    its turn cap is what's left after both."""
    w = world
    real = w.standin.run
    kills: list[str] = []

    def run(spec: SessionSpec, token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER and str(spec.task_id).endswith("w1") and len(kills) < 2:
            kills.append(spec.session_id)
            w.standin.specs.append(spec)
            on_event({"type": "transcript", "data": transcript_lines(spec)})
            return ExecOutcome("failed", 137, {}, "killed")
        return real(spec, token, on_event)

    w.standin.run = run  # type: ignore[method-assign]
    w.runner.cfg.vigild.scheduler.resume_attempts = 3
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    first, second, third = _workers(w, "w1")
    assert third.resume is not None and third.resume.from_session == second.session_id
    uuids = {e.get("uuid") for e in third.resume.entries}
    assert f"a-{first.session_id}" in uuids and f"a-{second.session_id}" in uuids
    assert third.max_turns == 60 - 2
