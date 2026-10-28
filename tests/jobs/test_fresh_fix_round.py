"""A fix round starts the worker fresh (#78, owner, 2026-10-28).

Its packet carries spec.md, the PR diff, the review and audit findings and the
verifier's summary, never the worker's transcript. The fork stays for true
escalation: attempt >= routing.escalate_attempt (or rule 5)."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

from typing import Any

from tests.conftest import auth
from tests.jobs.test_coord_loop import World, _failure_item, _work, world  # noqa: F401
from tests.jobs.test_fix_with_findings import (  # noqa: F401
    LOW_A,
    NOTE,
    _fix,
    _ready,
    _reset,
    _workers,
)
from vigild.executors.base import SessionSpec
from vigild.provenance import Provenance

FIRST_CALL_TOKENS = 20_000


def _fixed(w: World) -> tuple[str, list[object]]:
    job, item = _ready(w)
    task = f"{job}-w1"
    assert _fix(w, item["id"], task_id=task, findings=["audit:0"], note=NOTE).status_code == 200
    return job, _workers(w, "w1")


def test_a_fix_round_starts_fresh(world: World) -> None:
    w = world
    job, (_, fix) = _fixed(w)
    assert fix.resume is None
    assert not w.store.q("SELECT 1 FROM events WHERE job_id=? AND type='fork'", (job,))
    # first-call size: the whole packet (the system prompt's rules included), chars/4
    assert len(fix.packet) / 4 < FIRST_CALL_TOKENS


def test_the_fresh_fix_packet_carries_spec_diff_findings_and_verifier(world: World) -> None:
    w = world
    _, (_, fix) = _fixed(w)
    p = fix.packet
    assert "The approved spec.md" in p
    assert "<<<UNTRUSTED pr-diff" in p and "diff --git" in p  # fenced
    assert "<<<UNTRUSTED verifier-summary" in p
    assert "<<<UNTRUSTED review-findings" in p and LOW_A["claim"] in p
    assert "<<<UNTRUSTED owner-note" in p and NOTE in p
    assert "## docs/feature-map.md (excerpt)" not in p  # no context pack


def test_a_fix_round_forks_at_the_configured_escalation_attempt(world: World) -> None:
    w = world
    w.runner.cfg.routing.escalate_attempt = 1  # attempt 1 is already the escalation
    job, (first, fix) = _fixed(w)
    assert fix.resume is not None and fix.resume.from_session == first.session_id
    assert w.store.q("SELECT 1 FROM events WHERE job_id=? AND type='fork'", (job,))


def _fail_the_second_run(w: World) -> None:
    tries: dict[str, int] = {}

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        tid = str(spec.task_id)
        tries[tid] = tries.get(tid, 0) + 1
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if tries[tid] == 2 and tid.endswith("w1"):  # the fix round strays out of scope
            (spec.clone / "README.md").write_text("out of scope\n")

    w.standin.worker_edit = edit


def test_a_retry_at_attempt_two_starts_fresh_too(world: World) -> None:
    w = world
    _fail_the_second_run(w)
    job, item = _ready(w)
    task = f"{job}-w1"
    assert _fix(w, item["id"], task_id=task, findings=["audit:0"]).status_code == 200
    page = _failure_item(w, job)
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    _first, fix, retry = _workers(w, "w1")
    assert fix.resume is None and retry.resume is None
    assert not w.store.q("SELECT 1 FROM events WHERE job_id=? AND type='fork'", (job,))
    assert "The approved spec.md" in retry.packet and "<<<UNTRUSTED pr-diff" in retry.packet
    assert len(retry.packet) / 4 < FIRST_CALL_TOKENS


def test_the_fix_packet_carries_the_plan_and_the_spec_changelog(world: World) -> None:
    w = world
    job, item = _ready(w)
    w.store.add_artifact(job, "spec_changelog.md", "- CHANGELOG-MARK\n", Provenance.CLAUDE)
    assert _fix(w, item["id"], task_id=f"{job}-w1", findings=["audit:0"]).status_code == 200
    p = _workers(w, "w1")[-1].packet
    assert "CHANGELOG-MARK" in p and "<<<UNTRUSTED spec_changelog-md" in p
    assert "<<<UNTRUSTED plan-md" in p and "## Workers" in p
