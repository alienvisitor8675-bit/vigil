"""Stages 5 and 6 in the runner (plan 2a, item 1): a reviewer session per PR,
an audit where the ladder samples, fix rounds to the same worker, and rule 5
after two rounds with findings."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import auth, git
from tests.jobs.test_coord_loop import (
    CLEAN_AUDIT,
    CLEAN_REVIEW,
    World,
    _approve,
    _failure_item,
    _spend,
)
from tests.jobs.test_coord_loop import world as world
from vigild.executors.base import SessionSpec
from vigild.jobs.model import Role

MEDIUM = {"severity": "medium", "file": "docs/feature-map.md", "line": 3,
          "claim": "the row has no unit", "fix": "say req/min"}  # fmt: skip
HIGH = {**MEDIUM, "severity": "high", "claim": "the limit is wrong"}


def _rounds(per_task: dict[str, list[dict[str, Any]]]) -> Any:
    """A reviewer that answers each task's rounds in order, then clean."""
    seen: dict[str, int] = {}

    def review(spec: SessionSpec) -> dict[str, Any]:
        tid = str(spec.task_id)[-2:]
        n = seen.get(tid, 0)
        seen[tid] = n + 1
        answers = per_task.get(tid, [])
        return answers[n] if n < len(answers) else CLEAN_REVIEW

    return review


CHANGES = {**CLEAN_REVIEW, "verdict": "changes", "findings": [MEDIUM]}


def _workers(w: World, task: str) -> list[SessionSpec]:
    return [s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id).endswith(task)]


def test_an_empty_review_fails_the_stage_gate(world: World) -> None:
    w = world
    w.standin.review = lambda spec: {}
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    item = _failure_item(w, job, "review_failed")
    assert item["body"]["actions"] == ["retry", "abandon"]
    assert "no valid review.json" in json.dumps(item["body"]["tasks"])
    assert w.store.get_artifact(job, "review.json", f"{job}-w1") is None
    # retry from the inbox runs the review again; a good answer moves on
    w.standin.review = lambda spec: CLEAN_REVIEW
    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state.value == "ready"


def test_findings_go_back_to_the_same_worker_as_a_fix_round(world: World) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES]})
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    _, fix = _workers(w, "w1")
    assert len(_workers(w, "w2")) == 1
    # the fix round starts from the PR's head, with the findings fenced in its packet
    assert "Fix round 1" in fix.packet and "the row has no unit" in fix.packet
    assert "<<<UNTRUSTED review-findings" in fix.packet
    log = git(fix.clone, "log", "--format=%s", "-3")
    assert "harness: leftovers" in log  # the first round's commit is there
    # the same PR, pushed again: no new PR
    assert sorted(p["head"] for p in w.publisher.prs) == [f"vigil/{job}-w1", f"vigil/{job}-w2"]
    t = w.store.get_task(f"{job}-w1")
    assert t.state == "reviewed" and t.contract["review_rounds"] == 1
    rv = json.loads(w.store.get_artifact(job, "review.json", f"{job}-w1").content)  # type: ignore[union-attr]
    assert rv["round"] == 2 and rv["verdict"] == "clean"


def test_a_fix_round_reuses_the_workers_remaining_budget(world: World) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES]})
    w.standin.on_session = _spend(w, 15_000)  # $0.30 per worker session
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    first, fix = _workers(w, "w1")
    assert first.max_budget_usd == pytest.approx(2.0)
    assert fix.max_budget_usd == pytest.approx(1.70)
    held = w.store.q("SELECT amount_usd FROM reservations WHERE task_id=? ORDER BY id",
                     (f"{job}-w1",))  # fmt: skip
    assert [r["amount_usd"] for r in held] == [2.0, pytest.approx(1.70)]
    # reviews and audits are booked against the day, not the task's cap
    assert w.runner.remaining(f"{job}-w1")[1] == pytest.approx(0.60)


def test_two_rounds_with_findings_trigger_rule_5(world: World) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES, CHANGES]})
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    t = w.store.get_task(f"{job}-w1")
    assert t.contract["review_rounds"] == 2
    assert t.tier_reason is not None and t.tier_reason.startswith("rule5:escalated")
    (esc,) = [e for e in w.store.events(job_id=job) if e.type == "escalation"]
    assert esc.data["task_id"] == f"{job}-w1" and esc.data["review_rounds"] == 2
    assert len(_workers(w, "w1")) == 3


def test_findings_after_the_escalated_round_page_the_owner(world: World) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES, CHANGES, CHANGES]})
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    item = _failure_item(w, job, "review_rounds")
    assert item["body"]["actions"] == ["replan", "abandon"]
    assert len(_workers(w, "w1")) == 3


def test_an_audit_finding_the_review_missed_is_a_fix_round(world: World) -> None:
    w = world
    audits = iter([{"findings": [{**HIGH, "matched": None}], "false_findings": []}])
    w.standin.audit = lambda spec: next(audits, CLEAN_AUDIT)
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    fixed = [t for t in ("w1", "w2") if len(_workers(w, t)) == 2]
    assert len(fixed) == 1
    fix = _workers(w, fixed[0])[1]
    assert "the limit is wrong" in fix.packet
    rows = w.store.q("SELECT missed_high FROM audits ORDER BY id")
    assert sum(r["missed_high"] for r in rows) == 1


def test_reviewers_get_read_only_sessions_of_the_head(world: World) -> None:
    w = world
    job = _approve(w)
    reviewers = [s for s in w.standin.specs if s.role is Role.REVIEWER]
    assert len(reviewers) == 2
    for s in reviewers:
        assert s.output_schema is not None
        assert s.max_output_tokens == 8192  # the gateway's worst case fits the reservation
        assert "<<<UNTRUSTED diff" in s.packet  # the diff is data, not instructions
        assert "Fixture rules" in s.packet  # the base's CLAUDE.md
        assert (s.clone / "tests").is_dir()  # the worker's head, checked out
    # the reviewer's spend isn't the task's
    assert all(
        r["task_id"] is None
        for r in w.store.q(
            "SELECT task_id FROM reservations WHERE purpose IN ('reviewer','auditor')"
        )
    )
    assert w.store.get_job(job).state.value == "ready"


def test_an_escalated_attempt_forks_the_failed_attempts_whole_transcript(world: World) -> None:
    """Plan 2a, item 2: the escalated attempt's first packet carries the prior
    transcript (an SDK fork), not a summary."""
    w = world
    w.standin.review = _rounds({"w1": [CHANGES, CHANGES]})
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    first, second, escalated = _workers(w, "w1")
    assert first.resume is None
    # a fix round starts fresh (#78): no fork below the escalation attempt
    assert second.resume is None
    # the escalated attempt (rule 5) carries the whole failed attempt, history included
    assert w.store.get_task(f"{job}-w1").tier_reason.startswith("rule5")  # type: ignore[union-attr]
    assert escalated.resume is not None and escalated.resume.from_session == second.session_id
    sdk, stored = w.store.transcript(second.session_id)
    assert escalated.resume.session_id == sdk == f"sdk-{second.session_id}"
    assert escalated.resume.entries == stored
    uuids = [e["uuid"] for e in escalated.resume.entries]
    assert uuids == [f"u-{second.session_id}", f"a-{second.session_id}"]
    forks = [e for e in w.store.events(job_id=job) if e.type == "fork"]
    assert [f.data["escalated"] for f in forks] == [True]
