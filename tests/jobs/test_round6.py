"""2a round 6 (the final review of rounds 3-5): the review stage goes back
before a fix round, an empty review pass moves on, a breaker page outlives
the jobs abandoned from it, a review stage that can never fit says so, the
over-cap page can't double, and a merge during the move to ready is quiet."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import World, _approve, _request
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages
from tests.jobs.test_round3_runner import _building
from tests.review.test_review_stage import CHANGES, _rounds
from vigild.breakers import Kind
from vigild.executors.base import SessionSpec
from vigild.jobs.model import JobState, Role


def _open_stages(w: World) -> int:
    rows = w.store.q(
        "SELECT COUNT(*) n FROM reservations WHERE purpose='review_stage' AND status='open'"
    )
    return int(rows[0]["n"])


# --- 2: the stage goes back before the fix round --------------------------------------------


def test_the_pass_releases_its_stage_before_the_fix_round(world: World) -> None:
    w = world
    w.standin.review = _rounds({"w1": [CHANGES]})
    seen: dict[str, list[int]] = {"fix worker": [], "re-review": []}
    reviews = {"w1": 0}

    def watch(spec: SessionSpec) -> None:
        if spec.role is Role.WORKER and "Fix round" in spec.packet:
            seen["fix worker"].append(_open_stages(w))
        if spec.role is Role.REVIEWER and str(spec.task_id).endswith("w1"):
            reviews["w1"] += 1
            if reviews["w1"] == 2:
                seen["re-review"].append(_open_stages(w))

    w.standin.on_session = watch
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.READY
    assert seen == {"fix worker": [0], "re-review": [1]}  # none held through the fix build
    assert w.budget.open_today() == 0


# --- 4: an empty pass moves on --------------------------------------------------------------------


class Died(BaseException):
    pass


def test_a_review_with_nothing_left_to_review_moves_on_after_a_restart(world: World) -> None:
    w = world
    real = w.runner._paused
    calls = {"n": 0}

    def dies_after_reviews(job: Any, task: str | None = None, *, building: bool = False) -> Any:
        calls["n"] += task is None  # the coord's check, then review's two: at its start, after it
        if task is None and calls["n"] == 3:  # every task reviewed, not yet ready: vigild dies
            raise Died
        return real(job, task, building=building)

    w.runner._paused = dies_after_reviews  # type: ignore[method-assign]
    _request(w)
    (item,) = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
    with pytest.raises(Died):  # straight through the service: vigild itself dies
        w.runner.jobs.approve(item["id"])  # type: ignore[union-attr]
    w.runner._paused = real  # type: ignore[method-assign]
    (job,) = [j.id for j in w.store.list_jobs()]
    assert w.store.get_job(job).state is JobState.IN_REVIEW
    assert {t.state for t in w.store.list_tasks(job)} == {"reviewed"}
    w.runner.reconcile()
    w.runner.start_deferred()
    assert w.store.get_job(job).state is JobState.READY
    assert [p for p in _pages(w, job)] == []


# --- 3: a breaker page outlives the jobs abandoned from it ----------------------------------------


def test_abandoning_one_held_job_leaves_the_breaker_page_for_the_others(world: World) -> None:
    w = world
    j1, j2 = _building(w), _building(w)
    b = w.runner.breakers.trip(Kind.FIX_DEPTH, f"tree:{j1}", {"why": "a test"}, job_id=j1)
    w.runner._hold_for(j1, b)
    w.runner._hold_for(j2, b)
    (page,) = [p for p in _pages(w) if p["body"].get("kind") == "breaker"]
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                      json={"job_id": j1})  # fmt: skip
    assert r.status_code == 200, r.text
    assert [p["id"] for p in _pages(w) if p["body"].get("kind") == "breaker"] == [page["id"]]
    r = w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                      json={"job_id": j2})  # fmt: skip
    assert r.status_code == 200, r.text
    assert w.store.get_job(j2).state is JobState.ABANDONED


def test_a_reset_still_works_after_abandoning_one_held_job(world: World) -> None:
    w = world
    j1, j2 = _building(w), _building(w)
    b = w.runner.breakers.trip(Kind.FIX_DEPTH, f"tree:{j1}", {"why": "a test"}, job_id=j1)
    w.runner._hold_for(j1, b)
    w.runner._hold_for(j2, b)
    (page,) = [p for p in _pages(w) if p["body"].get("kind") == "breaker"]
    w.client.post(f"/api/inbox/{page['id']}/abandon", headers=auth(w.tokens.admin),
                  json={"job_id": j1})  # fmt: skip
    r = w.client.post(f"/api/inbox/{page['id']}/reset", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert r.json()["resumed"] == [j2]


# --- finding 5: a stage that can never fit says so ------------------------------------------------


def test_a_review_stage_bigger_than_the_day_says_so_and_offers_no_retry(world: World) -> None:
    w = world
    b = w.runner.cfg.budgets
    b.review_session_usd, b.audit_session_usd = 2.0, 1.0  # 2 tasks x $3 = $6 > $5
    job = _approve(w)
    (page,) = _pages(w, job)
    assert "needs $6.00; the daily cap is $5.00" in page["title"]
    assert "retry" not in page["body"]["actions"] and "abandon" in page["body"]["actions"]


# --- 6: a review session's own budget limit is the day ------------------------------------------


def test_review_sessions_run_with_the_days_cap_as_their_cli_limit(world: World) -> None:
    w = world
    _approve(w)
    rv = [s for s in w.standin.specs if s.role in (Role.REVIEWER, Role.AUDITOR)]
    assert rv and {s.max_budget_usd for s in rv} == {w.runner.cfg.budgets.daily_usd}


# --- 7: the over-cap page can't double ----------------------------------------------------------


def test_two_overages_at_once_page_exactly_once(world: World) -> None:
    w = world
    real = w.store.add_event

    def slow(type: str, data: Any, provenance: Any, **kw: Any) -> int:
        out = real(type, data, provenance, **kw)
        if type == "review_over_cap":
            time.sleep(0.2)  # both sessions' events land before either counts
        return out

    w.store.add_event = slow  # type: ignore[method-assign]
    barrier = threading.Barrier(2)

    def note() -> None:
        barrier.wait(5)
        w.runner._note_over_cap("docs", Role.REVIEWER, None, None, cap=0.40, cost=0.55)

    threads = [threading.Thread(target=note) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    pages = [p for p in _pages(w) if p["body"].get("kind") == "review_caps"]
    assert len(pages) == 1


# --- 10: a merge during the move to ready ---------------------------------------------------------


def test_a_merge_landing_during_the_move_to_ready_is_quiet(world: World) -> None:
    w = world
    real = w.runner._paused
    calls = {"n": 0}

    def merges_on_the_way(job: Any, task: str | None = None, *, building: bool = False) -> Any:
        calls["n"] += task is None  # the coord's check, then review's two: at its start, after it
        out = real(job, task, building=building)
        if task is None and calls["n"] == 3:  # after the state re-check, before ready
            w.runner.pr_state = lambda repo, url: "merged"
            w.runner.poll_prs()
        return out

    w.runner._paused = merges_on_the_way  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state is JobState.MERGED
    assert [p for p in _pages(w, job)] == []
    assert [e for e in w.store.events(job_id=job) if e.type == "review_dropped"]
