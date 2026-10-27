"""2a round 3 (a fresh-context re-review at df2404b): a queued run checks
the job before it runs, every breaker-held job can be abandoned, a review
stops quietly when the owner merged meanwhile, a wave leaves room for its own
reviews, a closing probe never resumes a job its own page holds, and resume
and retry say `queued`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import REPO, World, _approve, _flaky
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages, assert_actionable
from vigild.breakers import GLOBAL, Kind
from vigild.executors.base import SessionSpec
from vigild.jobs.model import JobState, Role


def _events(w: World, job: str, kind: str) -> list[dict[str, Any]]:
    return [e.data for e in w.store.events(job_id=job) if e.type == kind]


# --- 1: a queued run re-reads the job ---------------------------------------------------------


def test_a_queued_dispatch_is_dropped_when_the_job_blocked_meanwhile(world: World) -> None:
    """The re-review's scenario: the owner's reset asks for a dispatch while
    the job's dispatch is running; a worker then fails and `_hold_failed`
    blocks the job with its page. The queued dispatch must not re-run the
    workers on the blocked job."""
    w = world
    statuses: list[str | None] = []
    fail_w1 = _flaky({"w1"})

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        if str(spec.task_id).endswith("w1") and not statuses:
            statuses.append(w.runner.on_resume(str(spec.job_id)))  # the reset, mid-dispatch
        fail_w1(spec, task)

    w.standin.worker_edit = edit
    job = _approve(w)
    assert statuses == ["queued"]
    assert w.store.get_job(job).state is JobState.BLOCKED
    w1 = [s for s in w.standin.specs if s.role is Role.WORKER and str(s.task_id).endswith("w1")]
    assert len(w1) == 1  # never re-run without the owner choosing retry
    (dropped,) = _events(w, job, "queued_run_dropped")
    assert dropped["expected"] == "building" and dropped["state"] == "blocked"
    kinds = [p["body"].get("kind") for p in _pages(w, job)]
    assert kinds == ["worker_failed"]  # no "vigild error", one page to act on
    assert_actionable(w, job)


# --- 2: every breaker-held job can be abandoned -----------------------------------------------


def _building(w: World) -> str:
    real = w.runner.dispatch
    w.runner.dispatch = lambda j: None  # type: ignore[method-assign]
    job = _approve(w)
    w.runner.dispatch = real  # type: ignore[method-assign]
    assert w.store.get_job(job).state is JobState.BUILDING
    return job


def _scope(kind: Kind, job: str) -> str:
    return {
        Kind.DAILY: GLOBAL, Kind.MONTHLY: GLOBAL, Kind.ESCALATIONS: GLOBAL,
        Kind.PER_TASK: f"task:{job}-w1", Kind.FIX_DEPTH: f"tree:{job}",
        Kind.BACKEND: "backend:claude-sonnet", Kind.DECIDER_CREDIT: "route:jev-cloudflare",
    }.get(kind, f"repo:{REPO}")  # fmt: skip


@pytest.mark.parametrize("kind", list(Kind))
def test_every_breaker_held_job_can_be_abandoned(world: World, kind: Kind) -> None:
    w = world
    job = _building(w)
    b = w.runner.breakers.trip(kind, _scope(kind, job), {"why": "a test"}, job_id=job)
    w.runner._hold_for(job, b)
    page = assert_actionable(w, job)
    assert "abandon" in page["body"]["actions"]
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                      json={"job_id": job})  # fmt: skip
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state is JobState.ABANDONED
    assert job not in w.runner.breakers.get(b.id).held


def test_a_breaker_page_abandons_only_a_job_it_holds(world: World) -> None:
    w = world
    held, other = _building(w), _building(w)
    b = w.runner.breakers.trip(Kind.DAILY, GLOBAL, {"why": "a test"})
    w.runner._hold_for(held, b)
    (page,) = [p for p in _pages(w) if p["body"].get("kind") == "breaker"]
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                      json={"job_id": other})  # fmt: skip
    assert r.status_code == 409
    assert w.store.get_job(other).state is JobState.BUILDING
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin), json={})
    assert r.status_code == 409  # a breaker page needs the job named


# --- 3: a merge during review --------------------------------------------------------------


def test_a_review_stops_quietly_when_the_owner_merged_meanwhile(world: World) -> None:
    from tests.jobs.test_coord_loop import CLEAN_REVIEW

    w = world
    merged: list[bool] = []

    def review(spec: SessionSpec) -> Any:
        if not merged:  # the owner merges every PR while this review runs
            merged.append(True)
            w.runner.pr_state = lambda repo, url: "merged"
            assert w.runner.poll_prs() == [spec.job_id]
        return CLEAN_REVIEW

    w.standin.review = review
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.MERGED
    assert {t.state for t in w.store.list_tasks(job)} == {"merged"}
    assert [p for p in _pages(w, job)] == []  # no "vigild error"
    assert _events(w, job, "review_dropped")


# --- 7: resume and retry say `queued` ----------------------------------------------------------


def test_retry_says_queued_while_the_jobs_run_is_finishing(world: World) -> None:
    w = world
    w.standin.review = lambda spec: {}
    job = _approve(w)
    (page,) = _pages(w, job)
    with w.runner._running_lock:
        w.runner._running.add(job)
    w.runner.background = True
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert r.json()["run"] == "queued"
    with w.runner._running_lock:
        w.runner._running.discard(job)
        w.runner._queued.pop(job, None)


# --- 9: a closing probe never resumes a job its own page holds -------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 23, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_a_closing_probe_leaves_a_job_its_worker_failed_page_holds(world: World) -> None:
    w = world
    clock = Clock()
    br = w.runner.breakers
    br.clock = clock
    w.runner.cfg.routing.fallbacks["claude-sonnet"] = []
    scope = "backend:claude-sonnet"
    real = w.runner._healthy

    def healthy(preset: str) -> str | None:
        import inspect

        if inspect.stack()[1].function != "_worker":
            return real(preset)  # dispatch's check passes; the breaker trips under the worker
        if not br.is_open(Kind.BACKEND, scope):
            br.trip(Kind.BACKEND, scope, {"preset": preset})
        return None

    w.runner._healthy = healthy  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.BLOCKED
    assert [p["body"]["kind"] for p in _pages(w, job)] == ["worker_failed"]
    w.runner._healthy = real  # type: ignore[method-assign]
    clock.now += timedelta(seconds=w.runner.cfg.vigild.breakers.backend_cooldown_seconds + 1)
    assert br.claim_probe(Kind.BACKEND, scope)
    br.success(Kind.BACKEND, scope)
    assert w.store.get_job(job).state is JobState.BLOCKED  # its own page decides
    (page,) = _pages(w, job)
    assert page["body"]["kind"] == "worker_failed" and page["status"] == "open"
