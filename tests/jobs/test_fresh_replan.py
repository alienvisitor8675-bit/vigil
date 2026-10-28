"""A replan with a note starts the coord fresh (#78, owner, 2026-10-28).

The packet carries spec.md, plan.md, the owner's note, the failure reason and the
verifier summary, and no transcript is resumed or forked. The fork stays for true
escalation: attempt >= routing.escalate_attempt (attempt = 1 + replans so far)."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

from tests.jobs.test_coord_loop import (  # noqa: F401
    World,
    _approve,
    _failure_item,
    _flaky,
    auth,
    world,
)
from tests.jobs.test_replan_note import AMENDED, CHANGELOG, NOTE, _arm, _replan
from vigild.jobs.model import Role
from vigild.provenance import Provenance

FIRST_CALL_TOKENS = 20_000


def _replanned(w: World) -> tuple[str, list[object]]:
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    _arm(w, AMENDED, CHANGELOG)
    assert _replan(w, item, NOTE).status_code == 200
    return job, [s for s in w.standin.specs if s.role is Role.COORD]


def test_a_replan_with_a_note_starts_the_coord_fresh(world: World) -> None:
    w = world
    job, coords = _replanned(w)
    second = coords[-1]
    assert second.resume is None  # no resume, no fork
    assert not w.store.q("SELECT 1 FROM events WHERE job_id=? AND type='fork'", (job,))
    # first-call size: the whole packet (the system prompt's rules included), chars/4
    assert len(second.packet) / 4 < FIRST_CALL_TOKENS


def test_the_fresh_packet_carries_the_note_the_failure_and_the_plan(world: World) -> None:
    w = world
    job, coords = _replanned(w)
    packet = coords[-1].packet
    assert NOTE in packet  # the owner's note
    assert "outside scope" in packet  # the failure reason, fenced
    assert "## Workers" in packet and f"{job}-w1" in packet  # plan.md
    assert "## docs/" not in packet and "## README.md" not in packet  # no context pack


def test_the_fork_stays_for_escalation_at_the_configured_attempt(world: World) -> None:
    w = world
    w.runner.cfg.routing.escalate_attempt = 2  # the replan is attempt 2 here
    job, coords = _replanned(w)
    first, second = coords[0], coords[-1]
    assert second.resume is not None and second.resume.from_session == first.session_id
    assert w.store.q("SELECT 1 FROM events WHERE job_id=? AND type='fork'", (job,))


def test_the_fresh_packet_carries_the_spec_changelog(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    w.store.add_artifact(job, "spec_changelog.md", "- CHANGELOG-MARK\n", Provenance.CLAUDE)
    _arm(w, AMENDED, CHANGELOG)
    assert _replan(w, _failure_item(w, job), NOTE).status_code == 200
    second = [s for s in w.standin.specs if s.role is Role.COORD][-1]
    assert "CHANGELOG-MARK" in second.packet and "<<<UNTRUSTED spec_changelog-md" in second.packet


def test_a_plain_replan_at_attempt_two_starts_fresh_too(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    second = [s for s in w.standin.specs if s.role is Role.COORD][-1]
    assert second.resume is None
    assert not w.store.q("SELECT 1 FROM events WHERE job_id=? AND type='fork'", (job,))
    assert "outside scope" in second.packet and "## Workers" in second.packet
    assert len(second.packet) / 4 < FIRST_CALL_TOKENS
