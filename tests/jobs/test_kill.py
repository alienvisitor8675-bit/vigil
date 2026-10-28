"""`vigil kill <job>` (issue #52): stop a job cleanly, from the API or the CLI.

Effects, in order: the job's live sessions and their sidecars are stopped and
their containers and networks removed (by vigil's labels); its gateway tokens
are revoked; metered spend is settled and unused holds released once the
gateway drains; the job moves to `abandoned` through the FSM with a
`job_killed` event. Killing a finished or already killed job is a no-op that
says so (HTTP 200, `already: <state>`)."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.jobs.test_coord_loop import (  # noqa: F401
    REPO,
    World,
    _request,
    auth,
    world,
)
from vigild.breakers import Kind
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.gateway.service import Hold
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import JobState, Role
from vigild.jobs.service import NotFound

S = JobState


@dataclass
class Hung:
    """The executor with a worker that never finishes on its own: it returns once
    `stop_job` is called, as a container does when it is killed. Everything else
    is the usual stand-in."""

    inner: Any
    want: int = 2
    entered: list[str] = field(default_factory=list)
    ready: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    stop_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    purge_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, spec: SessionSpec, gateway_token: str, on_event: EventSink) -> ExecOutcome:
        if spec.role is not Role.WORKER:
            return self.inner.run(spec, gateway_token, on_event)  # type: ignore[no-any-return]
        with self.lock:
            self.entered.append(spec.session_id)
            if len(self.entered) >= self.want:
                self.ready.set()
        self.stopped.wait(20)
        return ExecOutcome("killed", 137, {}, "stopped")

    def stop_job(self, job_id: str, session_ids: list[str]) -> int:
        self.stop_calls.append((job_id, list(session_ids)))
        self.stopped.set()
        return len(session_ids)

    def purge_job(self, job_id: str, session_ids: list[str]) -> dict[str, int]:
        self.purge_calls.append((job_id, list(session_ids)))
        return {"containers": 3, "networks": 1}


def _spec_item(w: World) -> dict[str, Any]:
    items: list[dict[str, Any]] = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()[
        "specs"
    ]
    return items[0]


def _kill(w: World, job: str, **body: Any) -> Any:
    return w.client.post(f"/api/jobs/{job}/kill", headers=auth(w.tokens.admin), json=body)


def _events(w: World, job: str, kind: str) -> list[Any]:
    return [e for e in w.store.events() if e.job_id == job and e.type == kind]


def _failures(w: World) -> int:
    """The repo breaker's failures-in-a-row count."""
    b = w.runner.breakers._row(Kind.REPO_FAILURES, f"repo:{REPO}")
    return b.count if b else 0


def _open_reservations(w: World) -> list[int]:
    return w.budget.open_reservation_ids()


def _midrun(w: World) -> tuple[str, Hung, threading.Thread]:
    """A job whose two workers are running (and hung); returns the job, the
    executor and the thread that approved it (it returns when the run ends)."""
    hung = Hung(w.standin)
    w.runner.executor = hung
    job = _request(w)
    item = _spec_item(w)
    t = threading.Thread(
        target=lambda: w.client.post(
            f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={}
        ),
        daemon=True,
    )
    t.start()
    assert hung.ready.wait(20), "both workers should be running"
    assert _open_reservations(w), "a running worker holds a reservation"
    return job, hung, t


# --- a job with nothing running ----------------------------------------------------------


def test_a_spec_pending_job_with_no_sessions_is_killed(world: World) -> None:
    w = world
    job = _request(w)
    assert w.store.get_job(job).state is S.SPEC_PENDING
    r = _kill(w, job, channel="cli")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == job and body["state"] == "abandoned" and body["killed"] is True
    assert body["already"] is None
    assert w.store.get_job(job).state is S.ABANDONED
    # the job's spec page is closed, its tasks are not left pending
    assert not [q for q in w.store.list_inbox() if q.job_id == job]
    (ev,) = _events(w, job, "job_killed")
    assert ev.data["actor"] == "owner" and ev.data["channel"] == "cli"
    # the FSM wrote its own state event, with the same actor and channel
    moves = [e.data for e in _events(w, job, "state") if e.data["to"] == "abandoned"]
    assert moves and moves[-1]["actor"] == "owner" and moves[-1]["channel"] == "cli"
    assert moves[-1]["evidence"]["action"] == "kill"


def test_the_channel_defaults_to_api_and_is_checked(world: World) -> None:
    w = world
    job = _request(w)
    assert _kill(w, job, channel="bogus").status_code == 422
    assert w.store.get_job(job).state is S.SPEC_PENDING
    assert _kill(w, job).json()["state"] == "abandoned"
    (ev,) = _events(w, job, "job_killed")
    assert ev.data["channel"] == "api"


@pytest.mark.parametrize("scope", ["read", "none"])
def test_kill_is_an_admin_route(world: World, scope: str) -> None:
    w = world
    job = _request(w)
    headers = auth(w.tokens.read) if scope == "read" else {}
    r = w.client.post(f"/api/jobs/{job}/kill", headers=headers, json={})
    assert r.status_code in (401, 403)
    assert w.store.get_job(job).state is S.SPEC_PENDING


def test_an_unknown_job_is_404(world: World) -> None:
    assert _kill(world, "nope-9999").status_code == 404
    with pytest.raises(NotFound):
        world.runner.jobs.kill("nope-9999", Actor.OWNER, "api")  # type: ignore[union-attr]


# --- idempotence ---------------------------------------------------------------------------


def test_a_second_kill_is_a_no_op_that_says_so(world: World) -> None:
    w = world
    job = _request(w)
    assert _kill(w, job).json()["killed"] is True
    before = len(w.store.events())
    r = _kill(w, job)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["killed"] is False and body["already"] == "abandoned"
    assert "already" in body["message"] and job in body["message"]
    assert len(w.store.events()) == before  # nothing written, nothing stopped
    assert len(_events(w, job, "job_killed")) == 1


def test_a_done_job_is_not_killed(world: World) -> None:
    w = world
    job = _request(w)
    w.store._write_state(job, S.DONE, None)  # a finished job, however it got there
    r = _kill(w, job)
    assert r.status_code == 200 and r.json()["already"] == "done"
    assert w.store.get_job(job).state is S.DONE
    assert not _events(w, job, "job_killed")


def test_the_fsm_edge_is_the_owners_kill_and_nothing_else(world: World) -> None:
    w = world
    job = _request(w)
    for actor, channel, evidence in [
        (Actor.VIGILD, "api", {"action": "kill"}),
        (Actor.COORD, "api", {"action": "kill"}),
        (Actor.OWNER, "api", {}),  # no action=kill
        (Actor.OWNER, "somewhere", {"action": "kill"}),
    ]:
        with pytest.raises(TransitionError):
            transition(w.store, job, S.ABANDONED, actor, channel=channel, evidence=evidence)
    assert w.store.get_job(job).state is S.SPEC_PENDING


@pytest.mark.parametrize("state", [S.SPEC_PENDING, S.BUILDING, S.BLOCKED, S.ESCALATED])
@pytest.mark.parametrize(
    ("actor", "channel"),
    [(Actor.OWNER, "inbox"), (Actor.VIGILD, "api"), (Actor.VIGILD, "cli"), (Actor.COORD, "cli")],
)
def test_neither_the_inbox_nor_vigild_kills_a_job_with_action_kill(
    world: World, state: S, actor: Actor, channel: str
) -> None:
    """action=kill is the owner's, from the CLI or the API, and nobody else's: not the
    inbox channel, and not vigild itself; from a live state or from a hold."""
    w = world
    job = _request(w)
    w.store._write_state(job, S.BUILDING, None)
    if state in (S.BLOCKED, S.ESCALATED):
        transition(w.store, job, state, Actor.VIGILD, evidence={"reason": "test"})
    else:
        w.store._write_state(job, state, None)
    with pytest.raises(TransitionError):
        transition(w.store, job, S.ABANDONED, actor, channel=channel, evidence={"action": "kill"})
    assert w.store.get_job(job).state is state


@pytest.mark.parametrize("state", [S.APPROVED, S.PLANNING, S.BUILDING, S.IN_REVIEW, S.READY,
                                   S.MERGED, S.VERIFYING])  # fmt: skip
def test_every_live_state_can_be_killed(world: World, state: S) -> None:
    w = world
    job = _request(w)
    w.store._write_state(job, state, None)
    assert _kill(w, job).json()["state"] == "abandoned"
    assert w.store.get_job(job).state is S.ABANDONED


@pytest.mark.parametrize("state", [S.BLOCKED, S.ESCALATED])
def test_a_held_job_can_be_killed(world: World, state: S) -> None:
    w = world
    job = _request(w)
    w.store._write_state(job, S.BUILDING, None)
    transition(w.store, job, state, Actor.VIGILD, evidence={"reason": "test"})
    assert w.store.get_job(job).state is state
    assert _kill(w, job).json()["state"] == "abandoned"
    assert w.store.get_job(job).blocked_from is None


# --- a job with sessions running --------------------------------------------------------


def test_a_running_job_is_stopped_and_cleaned_up(world: World) -> None:
    w = world
    job, hung, t = _midrun(w)
    sids = sorted(hung.entered)
    before = _failures(w)
    r = _kill(w, job, channel="cli")
    t.join(20)
    assert not t.is_alive(), "the run must end once its sessions are stopped"
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "abandoned" and body["killed"] is True
    assert sorted(body["sessions_stopped"]) == sids

    # 1. sessions and sidecars: the executor was told which labels to stop and purge
    stopped_job, stopped_sids = hung.stop_calls[0]  # again once the run has ended
    assert stopped_job == job and sids == sorted(s for s in stopped_sids if s in sids)
    assert len(hung.purge_calls) == 1 and hung.purge_calls[0][0] == job
    assert set(sids) <= set(hung.purge_calls[0][1])
    # nothing started after the kill: no new session was made for a retry
    assert sorted(s.id for s in w.store.list_sessions(job) if s.role is Role.WORKER) == sids
    assert body["containers_removed"] == 3 and body["networks_removed"] == 1
    assert not [s for s in w.store.list_sessions(job) if s.status in ("created", "running")]

    # 2. every gateway token of the job is revoked
    for sid in sids:
        rows = w.store.q("SELECT revoked_at FROM gateway_tokens WHERE session_id=?", (sid,))
        assert rows and all(x["revoked_at"] for x in rows)
    assert body["tokens_revoked"] >= 1

    # 3. no open reservation is left for the job (the review stage's included)
    assert _open_reservations(w) == []
    assert body["reservations_open"] == []

    # 4. the state and the record
    assert w.store.get_job(job).state is S.ABANDONED
    assert all(x.state in ("abandoned", "pr_open", "reviewed", "merged")
               for x in w.store.list_tasks(job))  # fmt: skip
    (ev,) = _events(w, job, "job_killed")
    assert ev.data["actor"] == "owner" and ev.data["channel"] == "cli"
    assert sorted(ev.data["sessions_stopped"]) == sids
    # killing a job is the owner's choice: it is not a failure for the repo's breaker
    assert not [e for e in w.store.events() if e.type == "breaker_tripped"]
    assert _failures(w) == before, "a kill adds nothing to the repo's failures-in-a-row count"
    # and no new work came of it
    assert not w.publisher.prs


def test_a_session_waiting_for_a_slot_never_starts(world: World) -> None:
    """A session queued behind the slots when its job is killed must not run, and must
    not be given a token, once it gets its turn; its reservation is released."""
    w = world
    job = _request(w)
    minted: list[str] = []
    w.runner.gateway.issue_token = lambda *a, **k: minted.append("t") or "vgw_x"  # type: ignore[method-assign,return-value]
    _kill(w, job)
    s = w.store.create_session(job, Role.WORKER, task_id=None, backend_preset="claude-sonnet")
    here = w.runner.workspace.data_dir
    spec = SessionSpec(
        session_id=s.id, role=Role.WORKER, job_id=job, task_id=None, repo=REPO,
        preset="claude-sonnet", model="m", clone=here, session_dir=here, max_turns=1,
        max_minutes=1, max_budget_usd=0.1,
    )  # fmt: skip
    rid = w.budget.reserve(session_id=s.id, task_id=None, amount=0.1, purpose="worker")
    out = w.runner._run_session(s, spec, rid, {})
    assert out.status == "killed" and minted == [] and w.standin.calls == [("coord", job)]
    assert _open_reservations(w) == []


def _book(w: World, sid: str, job: str, cost: float) -> None:
    w.store.x(
        "INSERT INTO model_calls (session_id, task_id, job_id, purpose, provider, model,"
        " tokens_in, tokens_out, tokens_cache_read, tokens_cache_write_5m,"
        " tokens_cache_write_1h, cost_usd, ts, provenance) VALUES"
        " (?, NULL, ?, 'worker', 'anthropic', 'm', 1, 1, 0, 0, 0, ?, '2026-10-27T00:00:00Z',"
        " 'claude')",
        (sid, job, cost),
    )


def _hold(sid: str, amount: float) -> Hold:
    return Hold(session_id=sid, amount=amount, preset=None, body={})  # type: ignore[arg-type]


def _flight(w: World, sid: str, amount: float) -> Hold:
    gw = w.runner.gateway
    with gw._inflight_lock:
        gw._inflight[sid] = gw._inflight.get(sid, 0.0) + amount
    return _hold(sid, amount)


def _ledger(w: World, sid: str) -> list[float]:
    return [float(r["amount_usd"]) for r in
            w.store.q("SELECT amount_usd FROM ledger WHERE session_id=?", (sid,))]  # fmt: skip


def _only_kill_settles(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's own end-of-run settle does nothing, so what settles a reservation
    here is kill_job's: these tests fail if its drain and settle are taken out."""
    monkeypatch.setattr(w.runner, "_settle_checked", lambda *a, **k: None)


def test_in_flight_calls_drain_and_their_spend_is_booked_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    job, hung, t = _midrun(w)
    _only_kill_settles(w, monkeypatch)
    sid = sorted(hung.entered)[0]
    _book(w, sid, job, 0.07)
    hold = _flight(w, sid, 0.5)
    threading.Timer(0.4, w.runner.gateway.release, args=(hold,)).start()
    r = _kill(w, job)
    t.join(20)
    assert r.status_code == 200, r.text
    assert r.json()["drained"] is True and r.json()["reservations_open"] == []
    assert _open_reservations(w) == []
    assert _ledger(w, sid) == [pytest.approx(0.07)]  # the metered spend, once


def test_a_call_that_outlives_the_wait_settles_when_it_lands(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    job, hung, t = _midrun(w)
    _only_kill_settles(w, monkeypatch)
    sid = sorted(hung.entered)[0]
    _book(w, sid, job, 0.07)
    hold = _flight(w, sid, 0.5)
    r = w.runner.jobs.kill(job, Actor.OWNER, "api", wait=0.5)  # type: ignore[union-attr]
    assert r["state"] == "abandoned" and r["drained"] is False
    assert r["pending_calls"] == [sid]
    assert _open_reservations(w), "the reservation stays open (and counted) until the call lands"
    assert _ledger(w, sid) == []
    w.runner.gateway.release(hold)  # the streamed call finishes
    t.join(20)
    assert not t.is_alive()
    assert _open_reservations(w) == []
    assert _ledger(w, sid) == [pytest.approx(0.07)]
    w.runner.gateway.release(hold)  # a second release changes nothing
    assert _ledger(w, sid) == [pytest.approx(0.07)]


def test_the_review_stage_and_its_draws_are_released(world: World) -> None:
    w = world
    job = _request(w)
    w.store._write_state(job, S.IN_REVIEW, None)
    # what a review pass holds: the stage, and a reviewer's draw out of it
    stage = w.budget.reserve(session_id=None, task_id=None, amount=1.0, purpose="review_stage",
                             job_id=job)  # fmt: skip
    s = w.store.create_session(job, Role.REVIEWER, task_id=None, backend_preset="claude-opus")
    draw = w.budget.draw(stage, session_id=s.id, amount=0.4, purpose="reviewer")
    w.store.update_session(s.id, status="running")
    _book(w, s.id, job, 0.05)
    with w.runner._stage_lock:
        w.runner._review_stage_ids[job] = stage
    assert sorted(_open_reservations(w)) == sorted([stage, draw])
    r = _kill(w, job).json()
    assert _open_reservations(w) == [] and r["reservations_open"] == []
    assert sorted(r["reservations_settled"]) == sorted([stage, draw])
    assert _ledger(w, s.id) == [pytest.approx(0.05)]  # what the reviewer metered
    assert sum(float(x["amount_usd"]) for x in w.store.q("SELECT amount_usd FROM ledger")) == (
        pytest.approx(0.05)
    )  # the stage's unused part went back to the day
    assert w.store.get_session(s.id).status not in ("created", "running")


def test_only_the_jobs_own_reservations_are_touched(world: World) -> None:
    w = world
    job = _request(w)
    other = w.budget.reserve(session_id=None, task_id="elsewhere-w1", amount=0.5, purpose="worker")
    _kill(w, job)
    assert _open_reservations(w) == [other]


# --- the tail of a run that lands after the kill (round 2) --------------------------------


class Late:
    """The kill lands in the middle of a worker's tail, at `where`: `session` is just
    after its session ended `done` (before the verifier), `verified` just after the
    verifier and before the acceptance check and the publish."""

    def __init__(self, w: World, where: str) -> None:
        self.ready, self.go = threading.Event(), threading.Event()
        self.where = where
        if where == "session":
            inner = w.standin

            def run(spec: SessionSpec, token: str, on_event: EventSink) -> ExecOutcome:
                out: ExecOutcome = inner.run(spec, token, on_event)
                if spec.role is Role.WORKER:
                    self.pause()
                return out

            w.runner.executor = type("E", (), {"run": staticmethod(run)})()  # type: ignore[assignment]
        else:
            real = w.runner.verifier.verify

            def verify(**kw: Any) -> Any:
                out = real(**kw)
                self.pause()
                return out

            w.runner.verifier.verify = verify  # type: ignore[method-assign]

    def pause(self) -> None:
        self.ready.set()
        self.go.wait(30)


def _killed_before_the_tail(w: World, where: str) -> str:
    """A worker is done; the owner kills the job; then the worker's run goes on."""
    late = Late(w, where)
    job = _request(w)
    item = _spec_item(w)
    t = threading.Thread(
        target=lambda: w.client.post(
            f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={}
        ),
        daemon=True,
    )
    t.start()
    assert late.ready.wait(30), "a worker should have finished"
    r = w.runner.jobs.kill(job, Actor.OWNER, "api", wait=0.3)  # type: ignore[union-attr]
    assert r["killed"] is True
    late.go.set()
    t.join(60)
    assert not t.is_alive()
    return job


@pytest.mark.parametrize("where", ["session", "verified"])
def test_a_worker_done_just_before_the_kill_never_publishes(world: World, where: str) -> None:
    w = world
    job = _killed_before_the_tail(w, where)
    assert w.publisher.prs == [], "no push and no PR after a kill"
    assert not _events(w, job, "pr_open")
    if where == "verified":  # the verifier had finished: the publish itself is refused
        skipped = _events(w, job, "publish_skipped")
        assert skipped and all("kill" in e.data["reason"] for e in skipped)
    assert all(t.state != "pr_open" for t in w.store.list_tasks(job))


@pytest.mark.parametrize("where", ["session", "verified"])
def test_the_tail_of_a_run_after_the_kill_changes_nothing(world: World, where: str) -> None:
    w = world
    before = _failures(w)
    job = _killed_before_the_tail(w, where)
    tasks = w.store.list_tasks(job)
    assert tasks and all(t.state == "abandoned" for t in tasks)
    assert not [q for q in w.store.list_inbox() if q.job_id == job], "no page, no item"
    assert _failures(w) == before
    assert _open_reservations(w) == []
    assert w.store.get_job(job).state is S.ABANDONED


def test_fail_and_hold_failed_do_nothing_on_a_killed_job(world: World) -> None:
    w = world
    job, _hung, t = _midrun(w)
    tid = w.store.list_tasks(job)[0].id
    _kill(w, job)
    t.join(20)
    before = _failures(w)
    assert w.store.get_task(tid).state == "abandoned"
    assert w.runner._fail(job, None, tid, "late", {"why": "x"}) is False
    assert w.store.get_task(tid).state == "abandoned", "not reset to failed"
    assert not _events(w, job, "task_failed")
    w.runner._hold_failed(job, [tid])
    assert not [q for q in w.store.list_inbox() if q.job_id == job]
    assert _failures(w) == before


def test_a_spec_is_not_filed_for_a_job_killed_while_its_author_ran(world: World) -> None:
    from tests.jobs.test_coord_loop import _runner_world

    w = world
    _runner_world(w)
    killed: list[str] = []

    def kill(spec: SessionSpec) -> None:
        if spec.role is Role.HIDDEN_TEST_AUTHOR:
            w.runner.jobs.kill(spec.job_id, Actor.OWNER, "api", wait=0.2)  # type: ignore[union-attr]
            killed.append(spec.job_id)

    w.standin.on_session = kill
    job = _request(w)
    assert killed == [job]
    assert w.store.get_job(job).state is S.ABANDONED
    assert not [q for q in w.store.list_inbox() if q.job_id == job]


def test_every_publish_checks_the_kill_first(world: World) -> None:
    """The one publish step is shared by the worker, the deps job and the PR re-send."""
    from vigild.jobs.runner import PublishSkipped

    w = world
    job = _request(w)
    _kill(w, job)
    with pytest.raises(PublishSkipped):
        w.runner._publish(
            w.store.get_job(job), repo=REPO, head="0" * 40, base_sha="1" * 40, branch="b",
            base="main", title="t", body="b", scope=[],
        )  # fmt: skip
    assert w.publisher.prs == []
    assert _events(w, job, "publish_skipped")


# --- containers outside a session carry the job (round 2) ---------------------------------


def test_verifier_containers_run_in_their_jobs_scope(world: World) -> None:
    from tests.jobs.test_coord_loop import _approve
    from vigild.executors.runtime import current_job

    w = world
    seen: list[str] = []
    real = w.tests.run

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(current_job())
        return real(*a, **k)

    w.tests.run = spy  # type: ignore[method-assign]
    job = _approve(w)
    assert seen and set(seen) == {job}
    assert current_job() == "", "the scope ends with the call"


def test_the_deps_build_runs_in_its_jobs_scope(world: World) -> None:
    from types import SimpleNamespace

    from tests.jobs.test_coord_loop import _approve
    from vigild.executors.runtime import current_job
    from vigild.provenance import Provenance

    w = world
    job = _approve(w)
    tid = w.store.list_tasks(job)[0].id
    dep = {"ecosystem": "pypi", "package": "six", "version": "1.17.0", "why": "x"}
    w.store.add_artifact(job, "deps.json", json.dumps({"deps": [dep]}), Provenance.HUMAN)
    seen: list[str] = []

    class Deps:
        def run(self, *a: Any) -> Any:
            seen.append(current_job())
            return SimpleNamespace(errors=["stop here"], added=[], files={}, output="")

    w.runner.deps = Deps()  # type: ignore[assignment]
    w.runner._deps_worker(job, tid, {})
    assert seen == [job]


def test_a_spec_pending_kill_still_stops_and_purges_by_the_job(world: World) -> None:
    """Hidden-test probes run while the job is `spec_pending`, with no session of theirs."""
    w = world
    job = _request(w)
    hung = Hung(w.standin)
    w.runner.executor = hung
    assert _kill(w, job).json()["killed"] is True
    assert [c[0] for c in hung.stop_calls] and {c[0] for c in hung.stop_calls} == {job}
    assert [c[0] for c in hung.purge_calls] == [job]


# --- a kill that fails partway can be retried (round 2) -----------------------------------


def test_a_kill_that_fails_partway_is_finished_by_the_next(world: World) -> None:
    w = world
    job = _request(w)
    w.store._write_state(job, S.IN_REVIEW, None)
    stage = w.budget.reserve(session_id=None, task_id=None, amount=1.0, purpose="review_stage",
                             job_id=job)  # fmt: skip
    with w.runner._stage_lock:
        w.runner._review_stage_ids[job] = stage
    w.store.create_inbox_item("questions", "page", job, "a page", {"kind": "x"})
    real = w.runner.kill_job
    calls: list[str] = []

    def flaky(job_id: str, **kw: Any) -> dict[str, Any]:
        calls.append(job_id)
        if len(calls) == 1:
            raise RuntimeError("docker is gone; Bearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123")
        return real(job_id, **kw)

    w.runner.kill_job = flaky  # type: ignore[method-assign]
    r = _kill(w, job)
    assert r.status_code == 500
    assert "kill_job" in r.text and "sk-ant-api03" not in r.text  # names the step, redacted
    assert w.store.get_job(job).state is S.ABANDONED
    assert not _events(w, job, "job_killed")
    assert _open_reservations(w) == [stage]

    again = _kill(w, job)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["already"] == "abandoned" and body["completed"] is True
    assert _open_reservations(w) == []
    assert [q for q in w.store.list_inbox() if q.job_id == job] == []
    assert len(_events(w, job, "job_killed")) == 1
    # and now it is done: a third kill stops nothing and writes nothing
    n = len(w.store.events())
    assert _kill(w, job).json()["already"] == "abandoned"
    assert calls == [job, job] and len(w.store.events()) == n
