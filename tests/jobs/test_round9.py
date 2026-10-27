"""2a round 9: a reservation stays open while a call is in flight against it.

The sweep (round 8) settled a reservation once its session ended, even when
the session's last call had outlived the drain wait (round 8's deferred
settle). The late call was then metered but never booked, and its room was
reserved again: the reviewer metered $6.70 against a $5 cap. One rule now
decides whether an open reservation is live, for the sweep and reconcile
alike, and a call the gateway has in flight (a session's, or vigild's own)
keeps its reservation live for as long as the upstream read timeout allows.
"""

from __future__ import annotations

import functools
import json
import os
import random
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

import vigild.gateway.service as gsvc
import vigild.jobs.runner as rmod
from tests.conftest import auth, hold_usd
from tests.jobs.test_coord_loop import World, _approve, _flaky
from tests.jobs.test_coord_loop import world as world
from tests.jobs.test_round2_runner import _pages
from tests.jobs.test_round3_runner import _building, _scope
from tests.review.test_review_stage import CHANGES
from tests.test_budget import Clock
from vigild.breakers import GLOBAL, Breakers, Kind
from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.gateway.service import Gateway, GatewayError, Hold
from vigild.jobs.model import JobState, Role
from vigild.jobs.runner import JobRunner
from vigild.metering.calls import CallUsage, record_call
from vigild.metering.prices import PriceTable

EPS = 1e-6
PA = "thomasbardhi01/parkagent"


def _spend(store: Store, cfg: Config, session: str | None, dollars: float) -> None:
    record_call(store, PriceTable(cfg.prices), session_id=session, task_id=None, job_id=None,
                purpose="session", provider="anthropic", backend_preset="claude-sonnet",
                model="claude-sonnet-5-5", usage=CallUsage(tokens_out=int(dollars * 100_000)))  # fmt: skip


def _booked(store: Store) -> float:
    return float(store.q("SELECT COALESCE(SUM(amount_usd),0) n FROM ledger")[0]["n"])


def _metered(store: Store) -> float:
    return float(store.q("SELECT COALESCE(SUM(cost_usd),0) n FROM model_calls")[0]["n"])


class Up:
    """An upstream for vigild's own Haiku calls; `during` runs mid-call."""

    def __init__(self) -> None:
        self.tokens_in = 200
        self.during: Any = None

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        if self.during is not None:
            self.during()
        body = json.loads(req.content)
        return httpx2.Response(200, json={
            "model": body["model"], "content": [{"type": "text", "text": "{}"}],
            "usage": {"input_tokens": self.tokens_in, "output_tokens": 10}})  # fmt: skip


def _setup(
    cfg: Config, monkeypatch: Any, up: Up | None = None
) -> tuple[Store, Budget, Gateway, Any, Clock]:
    """A store, budget and gateway on one clock, and the runner's sweep (the
    probe's harness: the sweep needs only these parts of the runner)."""
    clock = Clock()
    store = Store.open(":memory:")
    budget = Budget(store, cfg.budgets, clock=clock)
    t = httpx2.MockTransport(up or Up())
    gw = Gateway(cfg, store, budget, Secrets({"ANTHROPIC_API_KEY": "fake-key-for-tests"}),
                 transport=t, async_transport=t)  # fmt: skip
    gw.breakers = Breakers(store, cfg, clock=clock)
    monkeypatch.setattr(rmod, "utcnow", clock)
    monkeypatch.setattr(gsvc, "check_decider_route", lambda *a, **k: None)
    fake = SimpleNamespace(store=store, budget=budget, gateway=gw, _stage_lock=threading.Lock(),
                           _review_stage_ids={})  # fmt: skip
    fake._reservation_live = functools.partial(JobRunner._reservation_live, fake)
    sweep = functools.partial(JobRunner.sweep_reservations, fake)
    return store, budget, gw, sweep, clock


# --- 1 (HIGH): a call in flight keeps its reservation ----------------------------------------


def test_breach_via_sweep(cfg: Config, monkeypatch: Any) -> None:
    """The reviewer's probe: the late call's room was reserved again."""
    store, budget, gw, sweep, clock = _setup(cfg, monkeypatch)
    a = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    b = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    store.update_session(a.id, status="running")
    store.update_session(b.id, status="running")
    ra = budget.reserve(session_id=a.id, task_id="ta", amount=2.0, purpose="worker")
    budget.reserve(session_id=b.id, task_id=None, amount=3.0, purpose="worker")
    _spend(store, cfg, b.id, 3.0)
    _spend(store, cfg, a.id, 0.2)
    h = hold_usd(gw, a.id, 1.7)
    clock.now += timedelta(minutes=30)
    gw.after_drain(a.id, functools.partial(budget.settle, ra))  # the drain wait timed out
    store.update_session(a.id, status="done")
    assert ra not in sweep()
    c = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    store.update_session(c.id, status="running")
    with pytest.raises(BudgetExceeded):  # the late call's room is still held
        budget.reserve(session_id=c.id, task_id="tc", amount=1.8, purpose="worker")
    _spend(store, cfg, a.id, 1.7)  # the late call lands
    gw.release(Hold(a.id, h, cfg.backends.presets["claude-sonnet"], {}))
    for rid in budget.open_reservation_ids():
        budget.settle(rid)
    assert _metered(store) <= cfg.budgets.daily_usd + EPS
    assert _booked(store) == pytest.approx(_metered(store))


def test_a_review_draw_whose_call_is_in_flight_is_not_swept(cfg: Config, monkeypatch: Any) -> None:
    store, budget, gw, sweep, clock = _setup(cfg, monkeypatch)
    s = store.create_session(None, Role.REVIEWER, backend_preset="claude-sonnet")
    store.update_session(s.id, status="running")
    stage = budget.reserve(session_id=None, task_id=None, amount=1.2, purpose="review_stage")
    d = budget.draw(stage, session_id=s.id, amount=0.4, purpose="reviewer")
    h = hold_usd(gw, s.id, 0.3)
    clock.now += timedelta(minutes=20)
    gw.after_drain(s.id, functools.partial(budget.settle, d))
    store.update_session(s.id, status="failed")
    budget.settle(stage, 0.0)  # the pass is over
    assert d not in sweep()
    _spend(store, cfg, s.id, 0.3)
    gw.release(Hold(s.id, h, cfg.backends.presets["claude-sonnet"], {}))
    assert _booked(store) == pytest.approx(_metered(store))


def test_a_hold_past_the_read_timeout_no_longer_keeps_its_reservation(
    cfg: Config, monkeypatch: Any
) -> None:
    """Bounded: a hold nothing has touched for longer than the upstream read
    timeout plus a margin can't be a live call (httpx would have given up)."""
    store, budget, gw, sweep, clock = _setup(cfg, monkeypatch)
    now = [1000.0]
    gw._now = lambda: now[0]  # type: ignore[method-assign]
    s = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    store.update_session(s.id, status="done")
    rid = budget.reserve(session_id=s.id, task_id=None, amount=1.0, purpose="worker")
    hold_usd(gw, s.id, 0.5)
    clock.now += timedelta(minutes=30)
    now[0] += gsvc.UPSTREAM_READ_S + gsvc.LIVE_MARGIN_S - 1
    assert sweep() == []
    gw._touch(s.id)  # a streamed chunk: the call is alive
    now[0] += gsvc.UPSTREAM_READ_S
    assert sweep() == []
    now[0] += gsvc.LIVE_MARGIN_S + 1
    assert sweep() == [rid]


def test_reconcile_asks_the_same_rule(world: World) -> None:
    """After a restart nothing of the old process runs, so everything settles,
    except what the gateway (in this process) still has in flight."""
    w = world
    gw = w.runner.gateway
    s = w.store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    w.store.update_session(s.id, status="running")
    busy = w.budget.reserve(session_id=s.id, task_id=None, amount=1.0, purpose="worker")
    idle = w.budget.reserve(session_id=None, task_id="t", amount=1.0, purpose="worker")
    h = hold_usd(gw, s.id, 0.2)
    w.runner.reconcile()
    assert w.budget.open_reservation_ids() == [busy]
    assert idle not in w.budget.open_reservation_ids()
    gw.release(Hold(s.id, h, w.runner.cfg.preset("claude-sonnet"), {}))


def test_a_streamed_call_touches_its_session(world: World) -> None:
    from fastapi.testclient import TestClient

    from tests.gateway.test_gateway import FAKE_KEY, Upstream, _msg
    from vigild.gateway.app import create_gateway_app

    up = Upstream()
    t = httpx2.MockTransport(up)
    gw = Gateway(world.runner.cfg, world.store, world.budget, Secrets({"ANTHROPIC_API_KEY": FAKE_KEY}),
                 transport=t, async_transport=t)  # fmt: skip
    touched: list[str] = []
    real = gw._touch
    gw._touch = lambda sid: (touched.append(sid), real(sid))[1]  # type: ignore[method-assign]
    s = world.store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    world.budget.reserve(session_id=s.id, task_id="t", amount=1.0, purpose="worker")
    tok = gw.issue_token(s.id, PA, "claude-sonnet")
    with TestClient(create_gateway_app(gw)) as c:
        r = c.post("/v1/messages", json=_msg(stream=True), headers={"x-api-key": tok})
        assert r.status_code == 200
    assert s.id in touched  # each chunk keeps the call alive


def _walk(cfg: Config, monkeypatch: Any, seed: int) -> None:
    """The reviewer's random walk: sessions, calls (some outliving their
    session), the sweep, clock ticks across midnight and month end, metered
    calls and review passes. Booked plus recorded excess equals metered at
    the end; booked plus open never passes a cap."""
    rnd = random.Random(seed)  # noqa: S311 - a reproducible walk, not a secret
    if seed % 3 == 1:
        cfg.budgets.daily_usd, cfg.budgets.monthly_usd = 50.0, 7.0
    store, budget, gw, sweep, clock = _setup(cfg, monkeypatch)
    clock.now = datetime(2026, 10, 31, 21, 0, tzinfo=UTC) if seed % 2 else datetime(
        2026, 10, 30, 23, 0, tzinfo=UTC)  # fmt: skip
    preset = cfg.backends.presets["claude-sonnet"]
    sessions: dict[str, int] = {}
    calls: list[tuple[str, float]] = []
    stage: dict[str, Any] = {}
    excess, pending, n = 0.0, 0, 0
    for _ in range(300):
        op = rnd.choice(["res", "call", "finish", "end", "end_timeout", "sweep", "tick",
                         "metered", "review_start", "review_draw", "review_pass_end", "tick"])  # fmt: skip
        try:
            if op == "res":
                n += 1
                s = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
                store.update_session(s.id, status="running")
                sessions[s.id] = budget.reserve(session_id=s.id, task_id=f"t{n}",
                                                amount=round(rnd.uniform(0.2, 2.0), 2),
                                                purpose="worker")  # fmt: skip
            elif op == "call" and sessions:
                sid = rnd.choice(list(sessions))
                room = budget.session_headroom(sid) - gw._inflight.get(sid, 0.0)
                if room > 0.02:
                    calls.append((sid, hold_usd(gw, sid, round(rnd.uniform(0.01, room), 2))))
            elif op == "finish" and calls:
                sid, h = calls.pop(rnd.randrange(len(calls)))
                _spend(store, cfg, sid, round(rnd.uniform(0, h), 2))
                gw.release(Hold(sid, h, preset, {}))
            elif op in ("end", "end_timeout") and sessions:
                sid = rnd.choice(list(sessions))
                if op == "end" and sid in gw._inflight:
                    continue
                rid = sessions.pop(sid)
                gw.after_drain(sid, functools.partial(budget.settle, rid))
                store.update_session(sid, status=rnd.choice(["done", "failed", "pr_open"]))
            elif op == "sweep":
                sweep()
            elif op == "tick":
                clock.now += timedelta(minutes=rnd.choice([1, 7, 11, 30, 90, 600, 1440]))
            elif op == "metered":
                before = len([e for e in store.events() if e.type == "settle_excess"])
                gw.metered(repo="thomasbardhi01/vigil", route="jev-cloudflare",
                           provider="cloudflare", model="typesafe/jev", purpose="decider",
                           job_id=None, private=False, body_chars=rnd.choice([300, 3000]),
                           run=functools.partial(dict, usage={
                               "input_tokens": rnd.choice([1000, 5000, 400_000, 2_000_000]),
                               "output_tokens": 10}))  # fmt: skip
                evs = [e for e in store.events() if e.type == "settle_excess"]
                excess += sum(float(e.data["excess_usd"]) for e in evs[before:])
            elif op == "review_start" and not stage:
                if pending == 0:
                    pending = rnd.randint(1, 7)
                k = min(pending, 4)
                rid = budget.reserve(session_id=None, task_id=None, amount=k * 1.2,
                                     purpose="review_stage")  # fmt: skip
                sweep.args[0]._review_stage_ids["J"] = rid
                stage.update(rid=rid, k=k, sess=[])
            elif op == "review_draw" and stage and len(stage["sess"]) < 2 * stage["k"]:
                role = rnd.choice([Role.REVIEWER, Role.AUDITOR])
                s = store.create_session(None, role, backend_preset="claude-sonnet")
                store.update_session(s.id, status="running")
                amt = 0.4 if role is Role.REVIEWER else 0.8
                sessions[s.id] = budget.draw(stage["rid"], session_id=s.id, amount=amt,
                                             purpose=role.value)  # fmt: skip
                stage["sess"].append(s.id)
            elif op == "review_pass_end" and stage:
                if any(s in sessions for s in stage["sess"]):
                    continue
                sweep.args[0]._review_stage_ids.pop("J")
                budget.settle(stage["rid"], 0.0)
                pending -= stage["k"]
                stage.clear()
        except BudgetExceeded, GatewayError:
            pass
        assert budget.spent_today() + budget.open_today() <= cfg.budgets.daily_usd + EPS
        assert budget.spent_month() + budget.open_month() <= cfg.budgets.monthly_usd + EPS
    for sid, h in calls:
        _spend(store, cfg, sid, h)
        gw.release(Hold(sid, h, preset, {}))
    for rid in list(sessions.values()) + budget.open_reservation_ids():
        budget.settle(rid)
    assert _booked(store) + excess == pytest.approx(_metered(store), abs=1e-6)


@pytest.mark.parametrize("seed", range(int(os.environ.get("VIGIL_WALKS", "40"))))
def test_random_walk_books_every_late_call(cfg: Config, monkeypatch: Any, seed: int) -> None:
    _walk(cfg, monkeypatch, seed)


# --- 2 (LOW): vigild's own calls in flight are live too -------------------------------------


def test_a_metered_call_in_flight_is_not_swept(cfg: Config, monkeypatch: Any) -> None:
    store, _budget, gw, sweep, clock = _setup(cfg, monkeypatch)
    swept: list[int] = []

    def run() -> dict[str, Any]:
        clock.now += timedelta(minutes=30)  # a slow Jev call
        swept.extend(sweep())
        return {"usage": {"input_tokens": 1000, "output_tokens": 10}}

    gw.metered(repo="thomasbardhi01/vigil", route="jev-cloudflare", provider="cloudflare",
               model="typesafe/jev", purpose="decider", job_id=None, run=run, private=False)  # fmt: skip
    assert swept == []
    assert _booked(store) == pytest.approx(_metered(store)) and _metered(store) > 0


def test_a_haiku_decider_call_in_flight_is_not_swept(cfg: Config, monkeypatch: Any) -> None:
    up = Up()
    store, _budget, gw, sweep, clock = _setup(cfg, monkeypatch, up)
    swept: list[int] = []

    def slow() -> None:
        clock.now += timedelta(minutes=30)
        swept.extend(sweep())

    up.during = slow
    gw.call(repo=PA, preset="claude-haiku", purpose="decider", job_id=None, private=False,
            body={"max_tokens": 100, "messages": [{"role": "user", "content": "q"}]})  # fmt: skip
    assert swept == []
    assert _booked(store) == pytest.approx(_metered(store)) and _metered(store) > 0


# --- 3 (LOW): both own-call paths trip the daily breaker on an excess ------------------------


def test_a_haiku_call_costing_more_than_reserved_trips_the_daily_breaker(
    cfg: Config, monkeypatch: Any
) -> None:
    up = Up()
    up.tokens_in = 10_000_000  # the provider says far more than the body could be
    store, _budget, gw, _sweep, _clock = _setup(cfg, monkeypatch, up)
    gw.call(repo=PA, preset="claude-haiku", purpose="decider", job_id=None, private=False,
            body={"max_tokens": 10, "messages": [{"role": "user", "content": "q"}]})  # fmt: skip
    assert [e for e in store.events() if e.type == "settle_excess"]
    assert gw.breakers is not None and gw.breakers.is_open(Kind.DAILY, GLOBAL)


# --- 4 (LOW): a breaker page always matches what the breaker holds ----------------------------


def test_a_breaker_page_that_still_holds_a_job_cant_be_dismissed(world: World) -> None:
    w = world
    job = _building(w)
    b = w.runner.breakers.trip(Kind.DAILY, _scope(Kind.DAILY, job), {"why": "a test"}, job_id=job)
    w.runner._hold_for(job, b)
    (page,) = [p for p in _pages(w) if p["body"].get("breaker") == b.id]
    r = w.client.post(f"/api/inbox/{page['id']}/dismiss", headers=auth(w.tokens.admin))
    assert r.status_code == 409, r.text
    assert [p for p in _pages(w) if p["body"].get("breaker") == b.id]


def test_a_job_its_own_page_takes_over_frees_the_breaker_page(world: World) -> None:
    w = world
    job = _building(w)
    b = w.runner.breakers.trip(Kind.BACKEND, "backend:claude-sonnet", {"preset": "claude-sonnet"})
    w.runner._hold_for(job, b)
    assert [p for p in _pages(w) if p["body"].get("breaker") == b.id]
    tid = w.store.list_tasks(job)[0].id
    w.store.update_task(tid, state="failed")
    w.runner._hold_failed(job, [tid])
    assert [p for p in _pages(w) if p["body"].get("breaker") == b.id] == []  # nothing held
    assert w.runner.breakers.get(b.id).state == "open"  # still needs its probe
    assert [p for p in _pages(w, job) if p["body"].get("kind") == "worker_failed"]


# --- 5: nits ---------------------------------------------------------------------------------


def test_a_late_settle_that_fills_the_day_trips_the_daily_breaker(world: World) -> None:
    w = world
    gw = w.runner.gateway
    late: dict[str, Any] = {}

    def a_slow_call(spec: Any) -> None:
        if spec.role is Role.WORKER and not late:
            late["sid"] = spec.session_id
            late["hold"] = Hold(spec.session_id, hold_usd(gw, spec.session_id, 0.01),
                                gw.cfg.preset("claude-sonnet"), {})  # fmt: skip

    w.standin.on_session = a_slow_call
    gw.drain = lambda sid, timeout=60.0: sid not in gw._inflight  # type: ignore[method-assign]
    _approve(w)
    assert not w.runner.breakers.is_open(Kind.DAILY, GLOBAL)
    _spend(w.store, w.runner.cfg, late["sid"], w.runner.cfg.budgets.daily_usd)  # the late call
    gw.release(late["hold"])
    assert w.runner.breakers.is_open(Kind.DAILY, GLOBAL)


def test_a_routing_error_puts_back_every_retry_it_rerouted(world: World) -> None:
    from vigild.router.rules import RoutingError

    w = world
    w.standin.worker_edit = _flaky({"w1", "w2"})
    job = _approve(w)
    (page,) = _pages(w, job)
    failed = sorted(t.id for t in w.store.list_tasks(job) if t.state == "failed")
    assert len(failed) == 2, [(t.id, t.state) for t in w.store.list_tasks(job)]
    before = {t: w.store.get_task(t) for t in failed}
    real = w.runner._reroute

    def second_fails(job_id: str, task_id: str, **kw: Any) -> None:
        if task_id == failed[1]:
            raise RoutingError("no route for the retry")
        real(job_id, task_id, **kw)

    w.runner._reroute = second_fails  # type: ignore[method-assign]
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    for tid, t in before.items():
        now = w.store.get_task(tid)
        assert now.contract.get("attempt") == t.contract.get("attempt"), tid
        assert (now.tier, now.backend_preset) == (t.tier, t.backend_preset), tid


def _abandon_now(w: World, job_id: str) -> None:
    w.runner._block(job_id, "the owner stops it")
    w.runner._page(job_id, "stop", {"actions": ["abandon"]})
    (pg,) = [p for p in _pages(w, job_id) if p["title"] == "stop"]
    r = w.client.post(f"/api/inbox/{pg['id']}/abandon", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text


def test_a_job_abandoned_before_its_review_moves_it_keeps_no_fix_task(world: World) -> None:
    """R2's `_move_on` guard on its own (a plain job, not a deps job): the
    owner abandons after the pass has read its outcomes, before the move."""
    w = world
    w.standin.review = lambda spec: CHANGES
    real = w.runner._paused
    done: list[str] = []

    def abandoned_then(job: Any, task: str | None = None, *, building: bool = False) -> Any:
        fix = [t for t in w.store.list_tasks(job.id) if t.state == "fix"]
        if (
            task is None
            and not done
            and fix
            and w.store.get_job(job.id).state is JobState.IN_REVIEW
        ):
            done.append(job.id)
            _abandon_now(w, job.id)
            # the abandon's own sweep ran before the pass wrote its fix round
            w.store.update_task(fix[0].id, state="fix")
        return real(job, task, building=building)

    w.runner._paused = abandoned_then  # type: ignore[method-assign]
    job = _approve(w)
    assert done == [job]
    assert w.store.get_job(job).state is JobState.ABANDONED
    left = {t.state for t in w.store.list_tasks(job)}
    assert not left & {"fix", "queued", "running", "waiting"}, left


def test_a_job_abandoned_as_its_review_asks_for_a_fix_keeps_no_fix_task(world: World) -> None:
    """The pass's own check: the abandon lands while a task's review writes
    its fix round."""
    w = world
    w.standin.review = lambda spec: CHANGES
    real = w.runner._reroute_for_round
    done: list[str] = []

    def abandoned_first(job_id: str, task_id: str, rounds: int) -> None:
        if not done:
            done.append(job_id)
            _abandon_now(w, job_id)
        real(job_id, task_id, rounds)

    w.runner._reroute_for_round = abandoned_first  # type: ignore[method-assign]
    job = _approve(w)
    assert done == [job]
    assert w.store.get_job(job).state is JobState.ABANDONED
    left = {t.state for t in w.store.list_tasks(job)}
    assert not left & {"fix", "queued", "running", "waiting"}, left
