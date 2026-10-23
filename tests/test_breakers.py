"""Circuit breakers (plan 2a, item 4): one test per breaker. Each trips it,
checks that dispatch for its scope pauses and that exactly one page opens,
and resets it from the inbox."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth
from tests.gateway.test_gateway import FAKE_KEY, Upstream, _msg, _session
from tests.jobs.test_coord_loop import World, _flaky, _request
from tests.jobs.test_coord_loop import world as world
from tests.review.test_review_stage import CHANGES, _rounds
from vigild.breakers import Breakers, Kind
from vigild.budget import Budget
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.gateway.app import create_gateway_app
from vigild.gateway.service import Gateway
from vigild.jobs.model import Role


def _pages(w: World, kind: str | None = None) -> list[dict[str, Any]]:
    qs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    return [q for q in qs if q["body"].get("kind") == "breaker"
            and (kind is None or q["body"]["breaker_kind"] == kind)]  # fmt: skip


def _approve(w: World, job: str) -> str:
    specs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
    (item,) = [x for x in specs if x["job_id"] == job]
    r = w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert r.status_code == 200, r.text
    return job


def _approve_next(w: World) -> str:
    return _approve(w, _request(w))


def _reset(w: World, page: dict[str, Any]) -> list[str]:
    r = w.client.post(f"/api/inbox/{page['id']}/reset", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    resumed: list[str] = r.json()["resumed"]
    return resumed


def _workers(w: World) -> int:
    return sum(1 for c in w.standin.calls if c[0] == "worker")


@pytest.mark.parametrize("cap", ["daily", "monthly"])
def test_the_daily_and_monthly_caps(world: World, cap: str) -> None:
    w = world
    if cap == "monthly":
        w.runner.cfg.budgets.daily_usd = 50.0
        w.runner.cfg.budgets.monthly_usd = 4.0
    a, b = _request(w), _request(w)  # both drafted before the day fills up
    # metered spend reaches the cap (2a round 2, review B1: not a reservation)
    spent = w.store.x(
        "INSERT INTO ledger (task_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
        ("other", 5.0 if cap == "daily" else 4.0, w.budget._day(), w.budget._month(), "x"),
    )
    _approve(w, a)
    _approve(w, b)
    assert _workers(w) == 0  # dispatch paused, everywhere
    assert {w.store.get_job(a).state.value, w.store.get_job(b).state.value} == {"blocked"}
    (page,) = _pages(w)  # one page for both jobs
    assert page["body"]["breaker_kind"] == f"{cap}_cap" and page["body"]["scope"] == "global"
    w.store.x("DELETE FROM ledger WHERE id=?", (spent,))  # say the owner raised the cap
    assert sorted(_reset(w, page)) == sorted([a, b])
    assert w.store.get_job(a).state.value == "ready"


def test_the_per_task_cap(world: World) -> None:
    w = world
    job = _request(w)
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][-1]
    w.store.x(
        "INSERT INTO ledger (task_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
        (f"{job}-w1", 1.99, "2000-01-01", "2000-01", "x"),
    )
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert _workers(w) == 0 and w.store.get_job(job).state.value == "blocked"
    (page,) = _pages(w, "per_task_cap")
    assert page["body"]["scope"] == f"task:{job}-w1" and page["job_id"] == job
    assert page["body"]["actions"] == ["reset", "replan", "abandon"]
    w.store.x("DELETE FROM ledger WHERE task_id=?", (f"{job}-w1",))  # say the owner raised it
    assert _reset(w, page) == [job]
    assert w.store.get_job(job).state.value == "ready"


def test_the_wip_limit(world: World) -> None:
    w = world
    w.runner.cfg.budgets.wip_per_repo = 2
    first = _approve_next(w)  # two PRs: the limit
    assert w.store.get_job(first).state.value == "ready"
    second, third = _approve_next(w), _approve_next(w)
    assert {w.store.get_job(j).state.value for j in (second, third)} == {"blocked"}
    (page,) = _pages(w, "wip")
    assert page["body"]["scope"] == "repo:o/fixture"
    assert len(page["body"]["detail"]["stack"]) == 2
    for t in w.store.list_tasks(first):
        w.store.update_task(t.id, state="merged")
    w.runner.cfg.budgets.wip_per_repo = 4
    assert sorted(_reset(w, page)) == sorted([second, third])
    assert {w.store.get_job(j).state.value for j in (second, third)} == {"ready"}


def test_escalations_per_day(world: World) -> None:
    w = world
    w.runner.cfg.vigild.breakers.max_escalations_per_day = 0
    w.standin.review = _rounds({"w1": [CHANGES, CHANGES]})
    other = _request(w)  # drafted before the breaker trips
    job = _approve_next(w)
    assert w.store.get_job(job).state.value == "blocked"
    (page,) = _pages(w, "escalations")
    assert page["body"]["scope"] == "global"
    assert len([s for s in w.standin.specs if s.role is Role.WORKER]) == 3  # no 4th: paused
    _approve(w, other)  # the scope is global: nothing else dispatches either
    assert w.store.get_job(other).state.value == "blocked" and len(_pages(w)) == 1
    w.runner.cfg.vigild.breakers.max_escalations_per_day = 5
    assert sorted(_reset(w, page)) == sorted([job, other])
    assert w.store.get_job(job).state.value == "ready"


def test_fix_job_depth(world: World) -> None:
    w = world
    root = _request(w)
    chain = [root]
    for _ in range(3):  # depth 3 is the limit
        child = w.runner.child_job(chain[-1], repo="o/fixture", base="main", title="fix",
                                   request="r", notes=[])  # fmt: skip
        assert child is not None
        chain.append(child.id)
    assert w.runner.child_job(chain[-1], repo="o/fixture", base="main", title="fix",
                              request="r", notes=[]) is None  # fmt: skip
    (page,) = _pages(w, "fix_depth")
    assert page["body"]["scope"] == f"tree:{root}"
    # dispatch pauses for the whole tree
    calls = len(w.standin.calls)
    w.runner.run_coord(chain[1])
    assert len(w.standin.calls) == calls and w.store.get_job(chain[1]).state.value == "blocked"
    assert w.runner.child_job(chain[-1], repo="o/fixture", base="main", title="fix",
                              request="r", notes=[]) is None and len(_pages(w)) == 1  # fmt: skip
    resumed = _reset(w, page)
    assert set(resumed) == {chain[1], chain[3]}
    assert w.store.get_job(chain[1]).state.value == "spec_pending"


def test_backend_errors(cfg: Config, store: Store) -> None:
    upstream = Upstream()
    upstream.status = 529
    transport = httpx2.MockTransport(upstream)
    budget = Budget(store, cfg.budgets)
    gw = Gateway(cfg, store, budget, Secrets({"ANTHROPIC_API_KEY": FAKE_KEY}),
                 transport=transport, async_transport=transport)  # fmt: skip
    now = [datetime(2026, 10, 23, 12, tzinfo=UTC)]
    pages: list[tuple[str, dict[str, Any]]] = []

    class Item:
        id = 1

    def pager(job: str | None, title: str, body: dict[str, Any]) -> Item:
        pages.append((title, body))
        return Item()

    gw.breakers = Breakers(store, cfg, pager, clock=lambda: now[0])
    sid, tok = _session(store, budget, gw)
    with TestClient(create_gateway_app(gw)) as c:
        for _ in range(5):
            c.post("/v1/messages", json=_msg(), headers={"authorization": f"Bearer {tok}"})
    scope = "backend:claude-sonnet"
    assert gw.breakers.is_open(Kind.BACKEND, scope) and len(pages) == 1
    # the route falls to the next preset; with none healthy, dispatch pauses
    from vigild.jobs.runner import JobRunner

    r = JobRunner.__new__(JobRunner)
    r.cfg, r.breakers = cfg, gw.breakers
    assert r._healthy("claude-sonnet") == "claude-opus"
    gw.breakers.trip(Kind.BACKEND, "backend:claude-opus", {})
    assert r._healthy("claude-sonnet") is None
    # after the cool-down, one half-open probe; its success closes the breaker
    now[0] += timedelta(seconds=cfg.vigild.breakers.backend_cooldown_seconds + 1)
    upstream.status = 200
    # a read moves nothing (2a review B6, A11): still open, and ready for its probe
    assert gw.breakers.is_open(Kind.BACKEND, scope)
    assert gw.breakers.probe_ready(Kind.BACKEND, scope)
    with TestClient(create_gateway_app(gw)) as c:
        ok = c.post("/v1/messages", json=_msg(), headers={"authorization": f"Bearer {tok}"})
    assert ok.status_code == 200
    assert gw.breakers._row(Kind.BACKEND, scope).state == "closed"  # type: ignore[union-attr]
    # and the owner resets one from the inbox
    opus = gw.breakers._row(Kind.BACKEND, "backend:claude-opus")
    assert opus is not None and gw.breakers.reset(opus.id) == []  # it held no job
    assert not gw.breakers.is_open(Kind.BACKEND, "backend:claude-opus")
    assert sid


def test_repo_failures(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1", "w2"} | {f"w{i}" for i in range(1, 9)})
    tries: dict[str, int] = {}

    def always_out_of_scope(spec: Any, task: dict[str, Any]) -> None:
        tid = str(spec.task_id)
        tries[tid] = tries.get(tid, 0) + 1
        (spec.clone / "README.md").write_text("out of scope\n")

    w.standin.worker_edit = always_out_of_scope
    k = w.runner.cfg.vigild.breakers.repo_failures
    nxt = _request(w)  # drafted before the breaker trips
    jobs = [_approve_next(w) for _ in range(k)]
    assert all(w.store.get_job(j).state.value == "blocked" for j in jobs)
    (page,) = _pages(w, "repo_failures")
    assert page["body"]["scope"] == "repo:o/fixture"
    workers = _workers(w)
    _approve(w, nxt)
    assert _workers(w) == workers and w.store.get_job(nxt).state.value == "blocked"
    assert len(_pages(w)) == 1
    w.standin.worker_edit = None
    assert _reset(w, page) == [nxt]
    assert w.store.get_job(nxt).state.value == "ready"


def test_verifier_unknowns(world: World) -> None:
    w = world
    w.runner.cfg.vigild.breakers.verifier_unknowns = 2
    real = w.tests.run

    def broken(image: str, source: Any, command: str, *, timeout_s: int, max_output: int) -> Any:
        from vigild.verifier import RunResult

        return RunResult(125, "vigil-prepare failed")  # a broken image: every run unknown

    w.tests.run = broken  # type: ignore[method-assign]
    nxt = _request(w)  # drafted before the breaker trips
    job = _approve_next(w)
    assert w.store.get_job(job).state.value == "blocked"
    (page,) = _pages(w, "verifier_unknowns")
    assert page["body"]["scope"] == "repo:o/fixture"
    _approve(w, nxt)
    assert w.store.get_job(nxt).state.value == "blocked" and len(_pages(w)) == 1
    w.tests.run = real  # type: ignore[method-assign]
    assert _reset(w, page) == [nxt]
    assert w.store.get_job(nxt).state.value == "ready"
