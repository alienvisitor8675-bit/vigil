"""2a round 7: a merge landing on the way to a fix round or a deps job's
regeneration is quiet, and a breaker that holds nothing after an abandon
closes and resolves its page."""

from __future__ import annotations

from typing import Any

from tests.conftest import auth
from tests.jobs.test_coord_loop import World, _approve
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages
from tests.jobs.test_round3_runner import _building
from tests.review.test_review_stage import CHANGES, _rounds
from vigild.breakers import GLOBAL, Kind
from vigild.jobs.model import JobState


def _merge_after_review(w: World, job_calls: int) -> None:
    """The PR poll merges every PR right after a job's review sessions, before
    the review moves the job on (review's second check of the pass)."""
    real = w.runner._paused
    calls: dict[str, int] = {}

    def paused(job: Any, task: str | None = None, *, building: bool = False) -> Any:
        out = real(job, task, building=building)
        if task is None:
            calls[job.id] = calls.get(job.id, 0) + 1
            if calls[job.id] == job_calls:
                w.runner.pr_state = lambda repo, url: "merged"
                w.runner.poll_prs()
        return out

    w.runner._paused = paused  # type: ignore[method-assign]


def test_a_merge_landing_on_the_way_to_a_fix_round_is_quiet(world: World) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES]})
    _merge_after_review(w, 3)  # the coord's check, the review's start, then after it
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.MERGED
    assert [p for p in _pages(w, job)] == []
    assert [e for e in w.store.events(job_id=job) if e.type == "review_dropped"]


def test_a_merge_landing_on_the_way_to_a_deps_regeneration_is_quiet(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    from tests.jobs.test_coord_loop import CLEAN_REVIEW
    from tests.jobs.test_deps import _approve_child, _deps_child

    w = world
    _parent, child, _pm = _deps_child(w, fixture_repo)
    w.standin.review = lambda spec: CHANGES if spec.job_id == child else CLEAN_REVIEW
    _merge_after_review(w, 2)  # a deps job has no coord: the review's start, then after it
    _approve_child(w, child)
    assert w.store.get_job(child).state is JobState.MERGED
    assert [p for p in _pages(w, child)] == []
    assert [e for e in w.store.events(job_id=child) if e.type == "review_dropped"]


def test_abandoning_a_breakers_last_held_job_resolves_its_page(world: World) -> None:
    """Round 7, as the owner ruled in round 8: the pages resolve, and the
    breakers stay open until their own condition clears."""
    w = world
    job = _building(w)
    br = w.runner.breakers
    daily = br.trip(Kind.DAILY, GLOBAL, {"why": "a test"})
    wip = br.trip(Kind.WIP, "repo:o/fixture", {"why": "a test"})
    w.runner._hold_for(job, daily)
    br.hold(wip, job)  # held by two breakers
    pages = {p["body"]["breaker"]: p for p in _pages(w) if p["body"].get("kind") == "breaker"}
    r = w.client.post(f"/api/inbox/{pages[daily.id]['id']}/abandon",
                      headers=auth(w.tokens.admin), json={"job_id": job})  # fmt: skip
    assert r.status_code == 200, r.text
    assert br.get(wip.id).state == "open" and br.get(daily.id).state == "open"
    assert [p for p in _pages(w) if p["body"].get("kind") == "breaker"] == []


def test_a_breaker_that_still_holds_a_job_keeps_its_page(world: World) -> None:
    w = world
    j1, j2 = _building(w), _building(w)
    br = w.runner.breakers
    wip = br.trip(Kind.WIP, "repo:o/fixture", {"why": "a test"})
    w.runner._hold_for(j1, wip)
    w.runner._hold_for(j2, wip)
    (page,) = [p for p in _pages(w) if p["body"].get("kind") == "breaker"]
    w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                  json={"job_id": j1})  # fmt: skip
    assert br.get(wip.id).state == "open"
    assert [p["id"] for p in _pages(w) if p["body"].get("kind") == "breaker"] == [page["id"]]
