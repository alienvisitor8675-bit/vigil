"""The job runner: drives a job through stages 1-3 of the protocol.

- on_job_created (a request with no spec): the coord drafts spec.md and
  plan.md; vigild runs the spec gate; up to three drafts, each its own session,
  then it pages you (resumable: resuming drafts again).
- on_approved (only from the inbox): planning turns plan.md's contracts into
  routed tasks; building runs them as a dependency graph (each task once its
  dependencies have published), in parallel, each in its own container, clone
  and branch.
- After any session, the harness has left a git bundle in the session's out
  dir and named its head on its result line. vigild imports the bundle into
  its own vault (refusing a tip that isn't that head) and does every read,
  check and push from there: host git never runs in a clone a session has
  touched, and no file a session wrote is read except through
  `safe_read_bytes` (review round 1, blockers 1-3). For a worker, the verifier
  then runs test_cmd on fresh vault checkouts in containers vigild owns; its
  record is the only test result (owner direction, round 2). Acceptance fails
  closed (blocker 4), and vigild publishes the draft PR itself (P2).
- on_resume (only from the inbox): a held job picks up its remaining work.
- A failed worker (its session, its result, acceptance, publish, a question
  for its coord, a stale image) blocks the job after its wave with one inbox
  item offering retry, re-plan or abandon (review round 2, N5). A retry runs
  only the failed and remaining tasks, each reserving what's left of its own
  cap; re-plan sends the job back to the coord with the failures in its packet.
- reconcile (at startup): nothing from before a restart keeps running or
  keeps money held (review round 1, item 7).

Every session reserves its budget before it starts, gets a gateway token that
expires and is revoked when it ends, runs in the executor image (its tests
run in the repo's validated dev image, through the verifier),
and writes its events (secrets redacted).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from vigild.breakers import CAP_KIND, GLOBAL, Breaker, Breakers, Kind, escalations_today
from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config
from vigild.contract.models import (
    BLOCKING,
    Audit,
    AuditOutput,
    Limits,
    Review,
    ReviewOutput,
    Spec,
    Task,
    TestOutcome,
    Verify,
    WorkerContract,
    WorkerReport,
)
from vigild.db import SessionRoleError, Store, iso, utcnow
from vigild.devimage import DevImages, SessionImages, StaleDevImage, missing_tools
from vigild.executors.base import ExecOutcome, Executor, Resume, SessionSpec
from vigild.gateway.service import TOKEN_TTL_MARGIN_MIN, Gateway
from vigild.github.publish import LegacyBranch, PublishError, artifact_paths, vigild_paths
from vigild.hidden import ARTIFACT as HIDDEN_ARTIFACT
from vigild.hidden import AUTHOR_ATTEMPTS, HiddenError, Place, entry_suite, load_suite, place_for
from vigild.intake.github import issue_text
from vigild.intake.spec_gate import test_cmd_problem
from vigild.jobs.acceptance import NO_HIDDEN, check_worker, escalates
from vigild.jobs.artifacts import parse_plan, render
from vigild.jobs.deps import infer_deps, is_lockfile
from vigild.jobs.fixfindings import selectable
from vigild.jobs.fsm import HAS_OPEN_PR, Actor, TransitionError, plan_gaps, transition
from vigild.jobs.model import FixDepthExceeded, Job, JobState, Role, SessionRow
from vigild.jobs.ownernote import author_contract, spec_body, spec_diff
from vigild.metering.calls import last_call_truncated, session_cost
from vigild.packets.assemble import (
    assemble_audit,
    assemble_coord,
    assemble_hidden_test_author,
    assemble_review,
    assemble_worker,
)
from vigild.packets.fence import fence
from vigild.provenance import Provenance
from vigild.redact import redact
from vigild.repos import (
    RepoPolicyError,
    check_backend,
    check_hidden_author,
    live_private,
    repo_rule_errors,
)
from vigild.review.ladder import Ladder, LadderChange, classify_shape
from vigild.router.rules import RoutingError, route
from vigild.scheduler import wip
from vigild.scheduler.graph import Slots, run_graph
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier, runner_like, runner_overlap
from vigild.workspace import Workspace, WorkspaceError

log = logging.getLogger("vigild.runner")
# the draft PR title of a runner-file task while hidden tests are off (host policy)
OWNER_REVIEW_TITLE = "Owner review required: runner file changed; hidden tests disabled"
COORD_DRAFTS = 3
COORD_MINUTES = 30
WORKER_MINUTES = 40
REVIEW_MINUTES = 20
REVIEW_TURNS = 30
# A review or an audit answers in a few thousand tokens. Capping each call's
# output keeps the gateway's worst-case hold inside the session's reservation.
REVIEW_OUTPUT_TOKENS = 8192
# a review pass reviews at most this many tasks, each pass reserving for its
# own (owner, 2026-10-23); as the WIP limit, so a pass is $4.80 at most
REVIEW_PASS_TASKS = 4
LIVE_SESSION = ("created", "running")
DEAD_TASK = ("superseded", "abandoned")  # out of the job: re-planned away or given up
QUEUE_POLL_SECONDS = 15.0  # how often a queued session looks for room in the day
PUBLISHED = ("pr_open", "reviewed")  # a task whose draft PR is open (reviewed: clean at its head)
MIN_RETRY_USD = 0.05  # less than this left of a task's cap: no retry is offered


class Publisher(Protocol):
    def publish(
        self,
        *,
        repo: str,
        head: str,
        base_sha: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        scope: list[str],
        existing: str | None = None,
    ) -> str: ...

    def close(self, repo: str, url: str) -> None: ...


class Pager(Protocol):
    def page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> Any: ...
    def file_for_approval(self, job_id: str) -> list[str]: ...
    def flagged_tasks(self, job_id: str) -> dict[str, list[str]]: ...
    def needs_hidden(self, job_id: str) -> dict[str, list[str]]: ...
    def hidden_enabled(self, job_id: str) -> bool: ...
    def file_item(self, job_id: str) -> None: ...
    def open_spec_item(self, job_id: str) -> Any: ...


SPEC_WAIT = frozenset({JobState.SPEC_PENDING})  # where a coord or an author waits
BUILD_WAIT = frozenset({JobState.BUILDING})  # where a worker waits
REVIEW_WAIT = frozenset({JobState.IN_REVIEW})  # where a review reserve waits


class QueueCancelled(Exception):
    """A queued reservation's job moved on while it waited (abandoned, blocked)."""


class Refused(Exception):
    """A session could not start (budget, policy); dispatch halts."""


def as_outcome(run: dict[str, Any] | None) -> TestOutcome:
    """A verifier run as the report's outcome; a timeout is a failure."""
    got = (run or {}).get("outcome")
    if got == "pass":
        return TestOutcome.PASS
    if got in ("fail", "timeout"):
        return TestOutcome.FAIL
    return TestOutcome.UNKNOWN


class JobRunner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        budget: Budget,
        gateway: Gateway,
        executor: Executor,
        workspace: Workspace,
        publisher: Publisher,
        telemetry: Telemetry,
        dev_images: DevImages,
        verifier: Verifier,
        *,
        session_images: SessionImages | None = None,
        background: bool = True,
        reaper: Callable[[], int] | None = None,
        breakers: Breakers | None = None,
        deps: Any = None,
        pr_state: Callable[[str, str], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.budget = budget
        self.gateway = gateway
        self.executor = executor
        stop = getattr(executor, "stop", None)
        if stop is not None:  # the gateway stops a session whose metering failed
            self.gateway.stop_session = stop
        self.workspace = workspace
        self.publisher = publisher
        self.telemetry = telemetry
        self.dev_images = dev_images
        self.session_images = session_images  # None: sessions use the generic executor image
        self.verifier = verifier
        self.background = background
        self.reaper = reaper
        self.jobs: Pager | None = None
        self._why: dict[str, dict[str, Any]] = {}  # task id -> why its last attempt failed
        self._why_lock = threading.Lock()
        self.deferred: list[str] = []  # approved builds reconcile found, not yet started
        self._running: set[str] = set()
        self._running_lock = threading.Lock()
        self.sleep: Callable[[float], None] = time.sleep  # tests replace it
        # a resume that arrives while the job's run is going: run it after (B4)
        self._queued: dict[str, tuple[Callable[[str], None], JobState | None]] = {}
        # reservations waiting for the day's headroom, oldest first (the budget queue)
        self._queue: list[tuple[str, str, str]] = []
        self._wait_cancelled: set[str] = set()  # tasks whose wait ended with their job
        self._queue_lock = threading.Lock()
        # the WIP room check and the move to running, for every job at once (B10)
        self._wip_lock = threading.Lock()
        # each job's review-stage reservation while its pass runs (owner, 2026-10-23)
        self._review_stage_ids: dict[str, int] = {}
        self._stage_lock = threading.Lock()
        self.closers: list[Callable[[], None]] = []  # e.g. the docker client (item 0)
        self.ladder = Ladder(store, cfg)
        self.deps = deps  # the deps executor (item 5); None: deps jobs fail
        self.pr_state = pr_state  # (repo, pr url) -> open | merged | closed
        # one set of breakers, shared with the gateway (item 4)
        self.breakers = breakers or Breakers(store, cfg)
        self.breakers.pager = self._breaker_page
        self.breakers.on_close = self._probe_closed  # a probe closed a backend breaker
        self._sdk_seen: set[tuple[str, str]] = set()
        self.slots = Slots(cfg.vigild.scheduler.sessions)  # across every job (item 2)

    def close(self) -> None:
        for close in self.closers:
            close()
        self.closers = []

    # --- queueing for the day's headroom ---------------------------------------------------

    def _queueable(self, e: BudgetExceeded, amount: float) -> bool:
        """Only a full day waits: its money frees up (a reservation settles, or
        the UTC day rolls over). A task's cap, the month, or an amount larger
        than the whole day never will, and neither does a day whose metered spend
        has reached the cap (that trips the daily breaker instead)."""
        b = self.cfg.budgets
        return (
            e.cap == "daily"
            and amount <= b.daily_usd
            and self.budget.spent_today() < b.daily_usd - 1e-9
        )

    def _queue_event(
        self, kind: str, job_id: str, what: str, task_id: str | None, **data: Any
    ) -> None:
        self.store.add_event(
            kind, {"for": what, **data}, Provenance.HUMAN, job_id=job_id, task_id=task_id
        )

    # The queue is first in, first out: a waiter takes a ticket when it first finds the
    # day full, only the oldest ticket may reserve, and a new request queues behind any
    # ticket rather than taking money a waiter is owed (2a, from the phase 1 run).

    def _queue_key(self, job_id: str, what: str, task_id: str | None) -> tuple[str, str, str]:
        return (job_id, task_id or "", what)

    def _queue_join(self, key: tuple[str, str, str]) -> None:
        with self._queue_lock:
            if key not in self._queue:
                self._queue.append(key)

    def _queue_leave(self, key: tuple[str, str, str]) -> None:
        with self._queue_lock:
            if key in self._queue:
                self._queue.remove(key)

    def _queue_turn(self, key: tuple[str, str, str]) -> bool:
        """Whether `key` may reserve now: it is the oldest ticket, or there are none."""
        with self._queue_lock:
            return not self._queue or self._queue[0] == key

    def _queue_cancelled(self, job_id: str, expect: frozenset[JobState]) -> bool:
        """A waiter looks at its job on every poll: one that is no longer where
        the waiter was queued for (abandoned, blocked, gone) has nothing to wait for."""
        try:
            return self.store.get_job(job_id).state not in expect
        except KeyError:
            return True

    def _reserve_queued(
        self,
        job_id: str,
        what: str,
        *,
        session_id: str | None,
        task_id: str | None,
        amount: float,
        purpose: str,
        queued: bool = False,
        expect: frozenset[JobState] = SPEC_WAIT,
    ) -> int:
        """Reserve, waiting while the day is full: the one waiting mechanism.
        Emits `queued` once and `dequeued` when it gets the money; not a
        failure, no page, no attempt used. Waiters are served in arrival order,
        and each poll re-checks the job: if it left `expect` (abandoned, say) the
        wait ends with `QueueCancelled`. Any other refusal raises."""
        key = self._queue_key(job_id, what, task_id)
        try:
            while True:
                if queued and self._queue_cancelled(job_id, expect):
                    self._queue_event("dequeued", job_id, what, task_id, cancelled=True)
                    raise QueueCancelled(job_id)
                detail = "behind an older waiter"
                if self._queue_turn(key):
                    try:
                        rid = self.budget.reserve(
                            session_id=session_id, task_id=task_id, amount=amount, purpose=purpose
                        )
                    except BudgetExceeded as e:
                        if not self._queueable(e, amount):
                            raise
                        detail = e.detail
                    else:
                        if queued:
                            self._queue_event("dequeued", job_id, what, task_id, amount_usd=amount)
                        return rid
                if not queued:
                    queued = True
                    self._queue_event(
                        "queued", job_id, what, task_id, amount_usd=amount, detail=detail
                    )
                self._queue_join(key)  # a no-op for a ticket the wave already took
                self.sleep(QUEUE_POLL_SECONDS)
        finally:
            self._queue_leave(key)

    # --- triggers ---------------------------------------------------------------------

    def running(self) -> list[str]:
        with self._running_lock:
            return sorted(self._running)

    def _spawn(self, fn: Callable[[str], None], job_id: str, expect: JobState | None = None) -> str:
        """One run per job at a time, whatever starts it (round 3, F7). A run
        asked for while one is going is queued, not dropped (review B4): it
        starts when the current one ends, and only if the job is still in
        the state it was asked for (`expect`); otherwise it is dropped with a
        `queued_run_dropped` event, and the job's own page stays the owner's
        way to act (2a round 3). Returns "started" or "queued"."""
        with self._running_lock:
            if job_id in self._running:
                self._queued[job_id] = (fn, expect)  # the latest ask wins
                log.info("%s is running; its next run is queued", job_id)
                return "queued"
            self._running.add(job_id)

        def next_run(j: str) -> Callable[[str], None] | None:
            """The queued run, if the job is still where it was asked for.
            Called with the running lock held."""
            while (queued := self._queued.pop(j, None)) is not None:
                queued_fn, want = queued
                if want is None:
                    return queued_fn
                try:
                    now = self.store.get_job(j).state
                except KeyError:
                    return None
                if now is want:
                    return queued_fn
                self.store.add_event(
                    "queued_run_dropped",
                    {"expected": want.value, "state": now.value,
                     "why": "the job moved while the run waited; its page is the way on"},
                    Provenance.HUMAN, job_id=j,
                )  # fmt: skip
            return None

        def safe(j: str) -> None:
            run: Callable[[str], None] | None = fn
            try:
                while run is not None:
                    try:
                        run(j)
                    except Exception as e:  # never die silently: page; the owner chooses
                        log.exception("runner failed on %s", j)
                        self._block(j, f"vigild error: {type(e).__name__}")
                        self._page(
                            j, f"vigild error on {j}",
                            {"kind": "vigild_error", "error": f"{type(e).__name__}: {e}",
                             "actions": ["retry", "abandon"]},
                            resumable=True,
                        )  # fmt: skip
                    with self._running_lock:
                        run = next_run(j)
                        if run is None:
                            self._running.discard(j)  # in the same lock: no ask is lost
            finally:
                if run is not None:  # vigild itself is going down mid-run
                    with self._running_lock:
                        self._queued.pop(j, None)
                        self._running.discard(j)

        if self.background:
            threading.Thread(target=safe, args=(job_id,), daemon=True).start()
        else:
            safe(job_id)
        return "started"

    def on_job_created(self, job_id: str, needs_spec: bool) -> None:
        if needs_spec:
            self._spawn(self.run_coord, job_id, JobState.SPEC_PENDING)
        elif self.jobs is not None and self.jobs.needs_hidden(job_id):
            # a submitted task that names a runner file: its hidden tests are
            # written in the background, and then the spec is filed
            self._spawn(self._finish_spec, job_id, JobState.SPEC_PENDING)

    def on_approved(self, job_id: str) -> None:
        self._spawn(self.build, job_id, JobState.APPROVED)

    def _author_only(self, job_id: str) -> bool:
        """A spec_pending job that has its spec and needs only the hidden-test
        step: a human-written spec (a coord never rewrites it), or a coord's
        whose latest session is an author's (the job was interrupted mid-author)."""
        spec = self.store.get_artifact(job_id, "spec.md")
        if spec is None or self.store.get_artifact(job_id, "plan.md") is None:
            return False
        if spec.provenance == Provenance.HUMAN.value:
            return True
        sessions = self.store.list_sessions(job_id)
        return bool(sessions) and sessions[-1].role is Role.HIDDEN_TEST_AUTHOR

    def on_resume(self, job_id: str, *, replan: bool = False) -> str | None:
        """Pick a job's work up where its state says. "started", "queued"
        (its current run finishes first), or None (nothing to run)."""
        state = self.store.get_job(job_id).state
        fn: Callable[[str], None] | None = {
            JobState.SPEC_PENDING: self.run_coord,
            JobState.APPROVED: self.build,
            JobState.PLANNING: self._plan_and_dispatch,
            JobState.BUILDING: self.dispatch,
            JobState.IN_REVIEW: self.review,
        }.get(state)
        if state is JobState.SPEC_PENDING and not replan and self._author_only(job_id):
            # a retry never sends a finished draft back to the coord (hotfix 5);
            # only a replan asks it for a new one
            fn = self._finish_spec
        return self._spawn(fn, job_id, state) if fn is not None else None

    # --- startup reconcile (item 7) ------------------------------------------------------

    def reconcile(self) -> dict[str, int]:
        """After a restart: revoke every gateway token, remove leftover session
        containers, fail sessions that were live, settle every open reservation,
        hold jobs that were mid-flight (paged, with retry and the other actions),
        offer retry or cancel for a draft whose coord it failed (N6), and queue
        approved jobs whose build never started. They start from
        `start_deferred`, which vigild calls once the gateway is listening
        (N11); each session then checks its repo's dev image itself."""
        out = {
            "tokens": self.gateway.revoke_all(),
            "containers": 0,
            "sessions": 0,
            "reservations": 0,
            "held": 0,
            "rebuilt": 0,
        }
        if self.reaper is not None:
            out["containers"] = self.reaper()
        failed_coord: set[str] = set()
        failed_author: set[str] = set()
        live = [s for s in self.store.list_sessions() if s.status in LIVE_SESSION]
        for r in self.store.q("SELECT * FROM reservations WHERE status='open'"):
            # first: what's left of each task's cap is known. Nothing of the
            # last process runs, so only the liveness rule's gateway part can
            # hold one (2a round 9)
            if self._reservation_live(r, set(), restarted=True):
                continue
            self.budget.settle(int(r["id"]))
            out["reservations"] += 1
        safe = {s.job_id for s in live if s.job_id and self._safe_to_resume(s)}
        for s in live:
            resumable = s.job_id in safe and s.role is Role.WORKER
            self.store.update_session(
                s.id, status="crashed" if resumable else "failed", ended_at=iso()
            )
            out["sessions"] += 1
            if s.role is Role.COORD and s.job_id:
                failed_coord.add(s.job_id)
            elif s.role is Role.HIDDEN_TEST_AUTHOR and s.job_id:
                failed_author.add(s.job_id)
        out["requeued"] = 0
        for job in self.store.list_jobs():
            if job.state is JobState.BUILDING and job.id in safe:
                # item 3: same image, budget left, under the cap: resume
                for t in self.store.list_tasks(job.id):
                    if t.state == "running":
                        self.store.update_task(t.id, state="queued")
                self.deferred.append(job.id)
                out["requeued"] += 1
            elif job.state in (JobState.PLANNING, JobState.BUILDING):
                for t in self.store.list_tasks(job.id):
                    if t.state == "running":
                        self.store.update_task(t.id, state="failed")
                self._block(job.id, "vigild restarted mid-flight")
                self._page(
                    job.id,
                    f"{job.id}: vigild restarted while it was {job.state.value}",
                    {"state": job.state.value, "actions": ["retry", "replan", "abandon"]},
                    resumable=True,
                )
                out["held"] += 1
            elif (
                job.state is JobState.SPEC_PENDING
                and job.id in failed_author
                and job.id not in failed_coord
                and self._author_only(job.id)
                and (self.jobs is None or self.jobs.open_spec_item(job.id) is None)
            ):
                # interrupted mid-author: retry runs only the author step, never the coord
                self._page(
                    job.id,
                    f"{job.id}: vigild restarted while the hidden-test author was writing",
                    {"state": job.state.value, "actions": ["retry", "abandon"]},
                    resumable=True,
                )
                out["held"] += 1
            elif job.state is JobState.SPEC_PENDING and job.id in (failed_coord | failed_author):
                self._page(
                    job.id,
                    f"{job.id}: vigild restarted while the coord was drafting",
                    {"state": job.state.value, "actions": ["retry", "abandon"]},
                    resumable=True,
                )
                out["held"] += 1
            elif job.state is JobState.IN_REVIEW and (
                gaps := plan_gaps(self.store, job.id, (*HAS_OPEN_PR, "merged"))
            ):
                # stuck in review with a plan task that has no PR (finding #4): page it
                self._hold_without_pr(job.id, gaps, "found at start")
                out["held"] += 1
            elif job.state is JobState.IN_REVIEW:
                # review B3: reviews a restart cut off run again; a review
                # session is read-only, so nothing of it needs keeping
                self.deferred.append(job.id)
                out["requeued"] += 1
            elif job.state is JobState.APPROVED:
                self.deferred.append(job.id)
                out["rebuilt"] += 1
        out["stranded"] = len(self.page_stranded_drafts())
        self.store.add_event("reconcile", out, Provenance.HUMAN)
        return out

    def page_stranded_drafts(self, grace_seconds: int = 0) -> list[str]:
        """Page every `spec_pending` job with no open inbox item and no run going: its coord
        failed before writing a spec and its failure page was resolved, so nothing offers
        the owner a way out (pa-0001). One page each, with replan (the coord runs again on
        the original request) and abandon; a job with any open item is not paged again.
        Runs at start and on the periodic sweep (which leaves a job younger than
        `grace_seconds` alone: its first coord may be about to start). Returns the jobs."""
        cutoff = utcnow().timestamp() - grace_seconds
        paged: list[str] = []
        for job in self.store.list_jobs():
            if job.state is not JobState.SPEC_PENDING or self.busy(job.id):
                continue
            if datetime.fromisoformat(job.created_at).timestamp() > cutoff:
                continue
            if any(i.job_id == job.id for i in self.store.list_inbox()):
                continue
            self._page(
                job.id,
                f"{job.id} is waiting for a spec that was never drafted",
                {"kind": "stranded_draft", "state": job.state.value,
                 "actions": ["replan", "abandon"]},
            )  # fmt: skip
            paged.append(job.id)
        return paged

    def _reservation_live(
        self, r: sqlite3.Row, stages: set[int], *, restarted: bool = False
    ) -> bool:
        """The one rule for whether an open reservation still backs something;
        the sweep and reconcile both ask it (2a round 9). Live: a call the
        gateway has in flight against it, or a settle waiting for one (bounded
        by the upstream read timeout), a session's or vigild's own; then,
        unless vigild just restarted (nothing of the last process runs), a
        session still created or running, a review stage whose pass is
        running, a worker reservation whose task is running."""
        if self.gateway.holds(r["session_id"], int(r["id"])):
            return True
        if restarted:
            return False
        if r["session_id"]:
            try:
                return self.store.get_session(r["session_id"]).status in LIVE_SESSION
            except KeyError:
                return False
        if r["purpose"] == "review_stage":
            return int(r["id"]) in stages
        if r["task_id"]:
            try:
                return self.store.get_task(r["task_id"]).state == "running"
            except KeyError:
                return False
        return False

    def sweep_reservations(self, grace_seconds: int = 600) -> list[int]:
        """Settle every open reservation nothing live holds (2a round 8): a
        leaked one would otherwise block every later day, since open rows
        count against today whatever day they began. Live is
        `_reservation_live`'s rule. Anything younger than the grace period is
        left alone (it may be about to be used). Returns the ids."""
        cutoff = utcnow().timestamp() - grace_seconds
        with self._stage_lock:
            stages = set(self._review_stage_ids.values())
        swept: list[int] = []
        for r in self.store.q("SELECT * FROM reservations WHERE status='open'"):
            if datetime.fromisoformat(r["created_at"]).timestamp() > cutoff:
                continue
            if self._reservation_live(r, stages):
                continue
            self.budget.settle(int(r["id"]))
            swept.append(int(r["id"]))
        if swept:
            self.store.add_event("reservation_swept", {"reservations": swept}, Provenance.HUMAN)
        return swept

    def _safe_to_resume(self, s: SessionRow) -> bool:
        """A worker a restart cut off may resume (item 3) when its job was
        building, its image key is unchanged, it has budget left and it's
        under the resume cap; every live session of the job must be such a
        worker."""
        if s.job_id is None:
            return False
        try:
            job = self.store.get_job(s.job_id)
        except KeyError:
            return False
        if job.state is not JobState.BUILDING:
            return False
        for other in self.store.list_sessions(job.id):
            if other.status not in LIVE_SESSION:
                continue
            if other.role is not Role.WORKER or not other.task_id:
                return False
            try:
                base = self.workspace.base_sha(other.task_id)
                same = other.detail.get("image") == self._expected_image(job.repo, base)
            except Exception:
                return False
            crashes = self._crashes(other.task_id) + 1  # this one
            left = self.remaining(other.task_id)[2]
            if not same or left < MIN_RETRY_USD:
                return False
            if crashes > self.cfg.vigild.scheduler.resume_attempts:
                return False
        return True

    def dev_image_failed(self, repo: str, title: str, body: dict[str, Any]) -> None:
        """DevImages' pager: one open item per failed image key."""
        assert self.jobs is not None
        for item in self.store.list_inbox():
            if item.body.get("kind") == "dev_image" and item.body.get("key") == body.get("key"):
                return
        self._page(None, title, body)

    def on_rebuild(self, repo: str, base_sha: str) -> None:
        """The owner asked for a failed dev image to be built again."""

        def go(_: str) -> None:
            if self.session_images is not None:
                self.session_images.ensure_fresh(repo, base_sha, force=True)
            else:
                self.dev_images.ensure_fresh(repo, base_sha, force=True)

        def safe(j: str) -> None:
            try:
                go(j)
            except StaleDevImage:
                pass  # DevImages paged the owner with what failed
            except Exception as e:
                log.exception("dev image rebuild failed for %s", repo)
                self._page(None, f"{repo}: dev image rebuild error", {"error": str(e)[:1000]})

        if self.background:
            threading.Thread(target=safe, args=(repo,), daemon=True).start()
        else:
            safe(repo)

    def _session_image(self, repo: str, base_sha: str) -> str | None:
        """The repo's session image (owner direction, round 3), built when its
        dev image or the executor layer changed; the dev image comes first."""
        if self.session_images is None:
            self.dev_images.ensure_fresh(repo, base_sha)
            return None
        return self.session_images.ensure_fresh(repo, base_sha)

    def start_deferred(self) -> int:
        """Start the builds reconcile queued. vigild calls this only once its
        gateway is listening (N11); each session checks its repo's dev image
        itself, building it when the lockfiles changed."""
        started, self.deferred = self.deferred, []
        for job_id in started:
            if self.store.get_job(job_id).state in (JobState.BUILDING, JobState.IN_REVIEW):
                self.on_resume(job_id)  # workers or reviews a restart cut off (item 3, B3)
            else:
                self.on_approved(job_id)
        return len(started)

    # --- helpers ------------------------------------------------------------------------

    # --- circuit breakers (item 4) --------------------------------------------------------

    def _breaker_page(self, job_id: str | None, title: str, body: dict[str, Any]) -> Any:
        assert self.jobs is not None
        return self.jobs.page(job_id, title, body)

    def _tree_root(self, job: Job) -> str:
        root = job
        while root.parent_job_id:
            root = self.store.get_job(root.parent_job_id)
        return root.id

    def _paused(
        self, job: Job, task: str | None = None, *, building: bool = False
    ) -> Breaker | None:
        """An open breaker whose scope covers this job (or task): dispatch
        pauses. The WIP breaker pauses only builds, which open PRs; a coord
        drafting or a review goes on."""
        return self.breakers.blocking(
            repo=job.repo, task=task, tree=self._tree_root(job), wip=building
        )

    def _refused_by_visibility(self, job: Job) -> bool:
        """The private-repo rule on the repo's live visibility, read at every
        dispatch (owner, 2026-10-23). A repo that went private while its
        backends break the rule is refused and paged: no fallback."""
        private = live_private(job.repo)
        errors = repo_rule_errors(self.cfg, job.repo, private)
        if not errors:
            return False
        self._block(job.id, "the private-repo rule: " + "; ".join(errors)[:300])
        self._page(
            job.id,
            f"{job.repo} is private now; its backends break the private-repo rule",
            {"kind": "repo_policy", "repo": job.repo, "errors": errors,
             "actions": ["abandon"]},
        )  # fmt: skip
        return True

    def _hold_for(self, job_id: str, b: Breaker) -> None:
        """Pause a job for a breaker: blocked, with no page of its own (the
        breaker paged once); resetting the breaker resumes it."""
        self._block(job_id, f"breaker {b.kind.value} ({b.scope})")
        self.breakers.hold(b, job_id)

    def on_breaker_reset(self, breaker_id: int, item_id: int) -> dict[str, list[str]]:
        """The owner reset a breaker from the inbox: every job it held goes
        back to where it was and picks up its work. A job whose last run is
        still finishing is `queued`, not `resumed` (review B4)."""
        return self._resume_held(
            self.breakers.reset(breaker_id), Actor.OWNER,
            {"inbox_item_id": item_id, "action": "resume", "breaker": breaker_id},
        )  # fmt: skip

    def _probe_closed(self, held: list[str], item_id: int) -> None:
        """A backend breaker's probe succeeded (B6): its page is done, and the
        jobs it held resume."""
        if item_id:
            self.store.resolve_inbox_item(item_id, "vigild")
        self._resume_held(held, Actor.VIGILD, {"action": "probe_closed"})

    def _resume_held(
        self, job_ids: list[str], actor: Actor, evidence: dict[str, Any]
    ) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {"resumed": [], "queued": []}
        for job_id in job_ids:
            try:
                job = self.store.get_job(job_id)
                if job.state is not JobState.BLOCKED or job.blocked_from is None:
                    continue
                transition(
                    self.store, job_id, job.blocked_from, actor,
                    channel="inbox" if actor is Actor.OWNER else None, evidence=evidence,
                )  # fmt: skip
            except TransitionError as e:
                log.warning("could not resume %s (%s)", job_id, evidence)
                self._page_if_gaps(job_id, e)
                continue
            except KeyError:
                continue
            status = self.on_resume(job_id)
            out["queued" if status == "queued" else "resumed"].append(job_id)
        return out

    def _refused_budget(
        self, job_id: str, e: BudgetExceeded, task_id: str | None
    ) -> Breaker | None:
        """A reservation refused at a cap (review B1). A task's own cap
        exhausted trips its per-task breaker; the global daily or monthly
        breaker trips only when the day's (month's) metered spend has
        actually reached the cap. Anything else (open reservations fill the
        day, a wave too big for what's left) is the job's own problem: None,
        and the caller pages the job."""
        kind = CAP_KIND.get(e.cap, Kind.DAILY)
        if kind is Kind.PER_TASK and task_id and self.budget.task_spent(task_id) > 0:
            return self.breakers.trip(
                kind, f"task:{task_id}", {"detail": e.detail}, job_id=job_id,
                actions=("reset", "replan", "abandon"),
            )  # fmt: skip
        return self._spend_check()

    def _spend_check(self) -> Breaker | None:
        """Trip the global daily (monthly) breaker once the day's (month's)
        metered spend reaches its cap (B14's hard stops; review B1)."""
        b = self.cfg.budgets
        for kind, spent, cap in (
            (Kind.DAILY, self.budget.spent_today(), b.daily_usd),
            (Kind.MONTHLY, self.budget.spent_month(), b.monthly_usd),
        ):
            if spent >= cap - 1e-9:
                return self.breakers.trip(
                    kind, GLOBAL, {"spent_usd": round(spent, 4), "cap_usd": cap}
                )
        return None

    def _budget_page(self, job_id: str, e: BudgetExceeded, tasks: list[str]) -> None:
        """A job whose reservation didn't fit gets its own page (review B1):
        retry (each task on what's left of its cap), re-plan or abandon."""
        b = self.cfg.budgets
        self._block(job_id, f"budget: {e}")
        self._page(
            job_id, f"{job_id}: the budget can't fit this work now",
            {"kind": "budget", "cap": e.cap, "detail": e.detail, "tasks": tasks,
             "day": {"spent_usd": round(self.budget.spent_today(), 4),
                     "held_usd": round(self.budget.open_today(), 4), "cap_usd": b.daily_usd},
             "actions": ["retry", "replan", "abandon"],
             "next": "retry once reservations settle or tomorrow (UTC), re-plan with smaller "
             "max_cost_usd, or abandon"},
            resumable=True,
        )  # fmt: skip

    def _healthy(self, preset: str) -> str | None:
        """The preset itself, or while its backend breaker is open the first
        healthy fallback (routing.yaml `fallbacks`); None pauses dispatch."""
        for p in [preset, *self.cfg.routing.fallbacks.get(preset, [])]:
            scope = f"backend:{p}"
            if not self.breakers.is_open(Kind.BACKEND, scope):
                return p
            if self.breakers.probe_ready(Kind.BACKEND, scope):
                return p  # cooled down: its first call is the probe (the gateway claims it)
        return None

    def child_job(self, parent_id: str, **kw: Any) -> Job | None:
        """A child job (a deps job now, a fix job in phase 6). Past depth 3 the
        job tree's fix-depth breaker trips and no job is made."""
        parent = self.store.get_job(parent_id)
        try:
            return self.store.create_job(parent_job_id=parent_id, **kw)
        except FixDepthExceeded as e:
            b = self.breakers.trip(
                Kind.FIX_DEPTH, f"tree:{self._tree_root(parent)}", {"detail": str(e)},
                job_id=parent_id,
            )  # fmt: skip
            self._hold_for(parent_id, b)
            return None

    def _page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> None:
        assert self.jobs is not None
        self.jobs.page(job_id, title, body, resumable=resumable)

    def _block(
        self,
        job_id: str,
        reason: str,
        blocked_on: str | None = None,
        *,
        state: JobState = JobState.BLOCKED,
    ) -> None:
        try:
            job = self.store.get_job(job_id)
            if job.state in (JobState.BLOCKED, JobState.ESCALATED, JobState.DONE):
                return
            evidence: dict[str, Any] = {"reason": reason}
            if blocked_on is not None:
                evidence["blocked_on"] = blocked_on
            transition(self.store, job_id, state, Actor.VIGILD, evidence=evidence)
        except TransitionError, KeyError:
            log.warning("could not block %s", job_id)

    def _event_sink(self, s: SessionRow) -> Callable[[dict[str, Any]], None]:
        def sink(e: dict[str, Any]) -> None:
            if e.get("type") == "transcript":
                self._transcript(s, e.get("data") or {})
                return
            if e.get("type") == "checkpoint":
                self._checkpoint(s, e.get("data") or {})
                return
            self.store.add_event(
                str(e.get("type", "state")),
                redact(e.get("data") or {}),
                Provenance.CLAUDE,
                job_id=s.job_id,
                session_id=s.id,
                task_id=s.task_id,
            )

        return sink

    def _transcript(self, s: SessionRow, data: dict[str, Any]) -> None:
        """Transcript lines the harness mirrored from the SDK: kept for resume
        and forks (phase 2a), redacted like every event."""
        sdk = data.get("session_id")
        entries = data.get("entries")
        if not isinstance(sdk, str) or not sdk or not isinstance(entries, list):
            return
        sub = data.get("subpath")
        self.store.add_transcript(
            s.id, sdk[:100], str(sub)[:200] if sub else None, redact(entries)[:5000]
        )
        if not sub and (s.id, sdk) not in self._sdk_seen:
            self._sdk_seen.add((s.id, sdk))  # every session's SDK id is stored (item 3)
            self.store.update_session(s.id, detail={"sdk_session_id": sdk[:100]})

    def _checkpoint(self, s: SessionRow, data: dict[str, Any]) -> None:
        """A running worker's checkpoint (item 3): imported into the vault at
        once, bound to the commit its line names, and recorded as the
        session's last bundled commit. One that doesn't match is refused."""
        head, turns = data.get("head"), data.get("turns")
        if s.role is not Role.WORKER or not s.task_id or not s.job_id or not isinstance(head, str):
            return
        try:
            job = self.store.get_job(s.job_id)
            got = self.workspace.import_result(
                job.repo,
                s.task_id,
                self._session_dir(s.id) / "out",
                str(s.branch),
                self.workspace.base_sha(s.task_id),
                head=head,
                checkpoint=True,
            )
        except (WorkspaceError, KeyError, OSError) as e:
            self.store.add_event(
                "checkpoint_refused", {"error": str(e)[:500]}, Provenance.HUMAN,
                job_id=s.job_id, session_id=s.id, task_id=s.task_id,
            )  # fmt: skip
            return
        n = int(turns) if isinstance(turns, int) else 0
        self.store.update_session(s.id, detail={"checkpoint": got, "turns": n})
        self.store.add_event(
            "checkpoint", {"head": got, "turns": n}, Provenance.HUMAN,
            job_id=s.job_id, session_id=s.id, task_id=s.task_id,
        )  # fmt: skip

    def _turns_used(self, resume: Resume | None) -> int:
        """Turns a worker has had, across the sessions its transcript carries:
        its distinct assistant messages."""
        if resume is None:
            return 0
        ids = {
            str((e.get("message") or {}).get("id") or e.get("uuid"))
            for e in resume.entries
            if isinstance(e, dict) and e.get("type") == "assistant"
        }
        return len(ids)

    def _crashes(self, task_id: str) -> int:
        """Crashed or killed worker sessions in a row, latest first."""
        rows = self.store.q(
            "SELECT status FROM sessions WHERE task_id=? AND role='worker' ORDER BY rowid DESC",
            (task_id,),
        )
        n = 0
        for r in rows:
            if r["status"] != "crashed":
                break
            n += 1
        return n

    def _resumable(self, task_id: str) -> bool:
        """Under the resume cap, with budget left (item 3)."""
        return (
            self._crashes(task_id) <= self.cfg.vigild.scheduler.resume_attempts
            and self.remaining(task_id)[2] >= MIN_RETRY_USD
        )

    def _expected_image(self, repo: str, base_sha: str) -> str:
        """The image a session for this base would run in now, computed
        without building anything (reconcile's "image key unchanged")."""
        dev = self.dev_images.tag(repo, self.dev_images.key(repo, base_sha))
        if self.session_images is None:
            return dev
        return self.session_images.tag(repo, self.session_images.key(dev))

    def _resume_from(self, session_id: str | None, *, fork: bool = True) -> Resume | None:
        """A Resume of a vigil session's stored transcript, if it has one."""
        if session_id is None:
            return None
        sdk, entries = self.store.transcript(session_id)
        if sdk is None or not entries:
            return None
        return Resume(session_id=sdk, entries=entries, fork=fork, from_session=session_id)

    def _last_worker(self, task_id: str) -> SessionRow | None:
        rows = self.store.q(
            "SELECT id FROM sessions WHERE task_id=? AND role='worker' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        )
        return self.store.get_session(rows[0]["id"]) if rows else None

    def _run_session(
        self,
        s: SessionRow,
        spec: SessionSpec,
        reservation: int,
        attrs: dict[str, Any],
        *,
        cap: float | None = None,
    ) -> ExecOutcome:
        """A session slot, then token, telemetry, run, revoke, settle. The
        reservation is already made; the token is issued once the slot is
        taken, so waiting for one doesn't eat its life."""
        if s.role is not spec.role:
            # role separation, enforced at dispatch: a session row never runs as
            # another role, so an author's session is never a worker or a reviewer
            raise SessionRoleError(f"session {s.id} is a {s.role.value}, not a {spec.role.value}")
        with self.slots.session():
            out: ExecOutcome | None = None
            try:
                # inside the try: a token refused (the repo went private) still
                # settles the reservation (2a round 8)
                token = self.gateway.issue_token(
                    s.id, spec.repo, spec.preset,
                    ttl_minutes=spec.max_minutes + TOKEN_TTL_MARGIN_MIN,
                )  # fmt: skip
                self.store.update_session(
                    s.id, status="running", started_at=iso(), clone_path=str(spec.clone)
                )
                with self.telemetry.session(s.id, attrs) as tr:
                    spec.traceparent = tr.traceparent
                    self.store.update_session(s.id, detail={"trace_id": tr.trace_id})
                    with self.telemetry.span(tr, "vigil.spawn", {"vigil.image": spec.image or ""}):
                        out = self.executor.run(spec, token, self._event_sink(s))
                    tr.set("vigil.status", out.status)
                    if cap is not None:  # a review or audit: did it go past its cap?
                        self.gateway.drain(s.id)
                        over = session_cost(self.store, s.id) - cap
                        tr.set("vigil.review.over_cap", over > 1e-9)
                        tr.set("vigil.review.overage_usd", round(max(0.0, over), 6))
                    if out.status == "done" and last_call_truncated(self.store, s.id):
                        # never a finished turn: it was cut off at the output cap
                        out.status = "failed"
                        out.error = (
                            "the final turn was truncated at the output cap "
                            f"({self.cfg.budgets.session_max_output_tokens} tokens per call)"
                        )
                        tr.set("vigil.truncated", True)
                        tr.set("vigil.status", out.status)
                    failed_metering = self.store.q(
                        "SELECT 1 FROM events WHERE session_id=? AND type='metering_failed'",
                        (s.id,),
                    )
                    if failed_metering:  # a call's spend is unbooked: never a finished session
                        out.status = "failed"
                        out.error = "metering failed; see the inbox page"
                        tr.set("vigil.status", out.status)
                    if out.status != "done":
                        refused = self.store.q(
                            "SELECT data FROM events WHERE session_id=? AND type='gateway_refused'"
                            " ORDER BY id DESC",
                            (s.id,),
                        )
                        for row in refused:
                            reason = str(json.loads(row["data"]).get("reason", ""))
                            if reason.startswith("budget:"):
                                # name the cap and the shortfall on the page
                                out.error = f"{out.error or ''} | {reason}".strip(" |")
                                break
                return out
            finally:
                self.gateway.revoke(s.id)
                if self.gateway.drain(s.id):  # its last calls booked first
                    self._settle_checked(s, spec, reservation, out)
                else:
                    # a call outlived the wait: the reservation stays open (and
                    # counted, and live to the sweep) until it is booked, then
                    # settles (2a rounds 8-9)
                    log.warning("%s: a call was still in flight at settling", s.id)
                    self.gateway.after_drain(
                        s.id, functools.partial(self._settle_checked, s, spec, reservation, out)
                    )
                self.store.update_session(s.id, ended_at=iso())

    def _settle_checked(
        self, s: SessionRow, spec: SessionSpec, reservation: int, out: ExecOutcome | None
    ) -> None:
        """Settle a session's reservation (failing closed if that raises), then
        the spend check: a late settle can fill the day too (2a round 9)."""
        self._settle_fail_closed(s, spec, reservation, out)
        self._spend_check()

    def _settle_fail_closed(
        self, s: SessionRow, spec: SessionSpec, reservation: int, out: ExecOutcome | None
    ) -> None:
        """Settle the session's reservation. If that raises, its spend is unknown:
        book the whole reservation as spent (never $0), fail the session, page."""
        try:
            self.budget.settle(reservation)
            return
        except Exception as e:
            err = redact(f"{type(e).__name__}: {e}")
        log.error("settle failed for session %s: %s", s.id, err)
        # the session's whole open money (its reservation plus an over-cap extension or a
        # draw's top-up) is booked at once, so the sweep can't book those rows again
        rows = self.store.q(
            "SELECT COALESCE(SUM(amount_usd), 0) n FROM reservations"
            " WHERE status='open' AND (id=? OR session_id=?)",
            (reservation, s.id),
        )
        reserved = float(rows[0]["n"])
        steps: list[Callable[[], Any]] = [
            lambda: self.budget.settle(reservation, reserved),
            lambda: self.store.update_session(s.id, status="failed"),
            lambda: self.store.add_event(
                "metering_failed",
                {"session_id": s.id, "error": err, "booked_usd": reserved},
                Provenance.HUMAN,
                job_id=spec.job_id,
                session_id=s.id,
                task_id=spec.task_id,
            ),
            lambda: self.store.create_inbox_item(
                "questions",
                "page",
                spec.job_id,
                f"metering failed: {err}; spend for session {s.id} not booked",
                {
                    "session_id": s.id,
                    "booked_usd": reserved,
                    "note": "the whole reservation was booked as spent; reconcile with provider",
                    "resumable": False,
                },
            ),
        ]
        for step in steps:
            try:
                step()
            except Exception:
                log.exception("settle-failure step failed for %s", s.id)
        if out is not None:
            out.status = "failed"
            out.error = "metering failed; see the inbox page"

    def _replan_context(self, job_id: str) -> dict[str, Any]:
        """The failures the owner sent back with the latest re-plan, if any."""
        rows = self.store.q(
            "SELECT data FROM events WHERE job_id=? AND type='replan' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        if not rows:
            return {}
        tasks = json.loads(rows[0]["data"]).get("tasks")
        return tasks if isinstance(tasks, dict) else {}

    def busy(self, job_id: str) -> bool:
        """Whether a run already holds the job (a second start would be dropped)."""
        with self._running_lock:
            return job_id in self._running

    def _replan_notes(self, job_id: str) -> list[str]:
        """The owner's notes, verbatim, oldest first: every note since the last
        approval, if the latest re-plan carried one (a plain re-plan has none, and an
        approval closes the notes before it)."""
        rows = self.store.q(
            "SELECT data FROM events WHERE job_id=? AND type='replan' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        if not rows or not json.loads(rows[0]["data"]).get("note_sha256"):
            return []
        return self.store.notes_since_approval(job_id)

    def _amend_context(self, job_id: str) -> tuple[str, str, list[str]] | None:
        """(the spec to amend, its baseline kind, the owner's notes) when the latest
        re-plan has a note. The kind is "approved", "approved-pre-versioning" or "draft"."""
        notes = self._replan_notes(job_id)
        previous = self.store.get_artifact(job_id, "spec_previous.md")
        if not notes or previous is None:
            return None
        rows = self.store.q(
            "SELECT data FROM events WHERE job_id=? AND type='replan' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        baseline = str(json.loads(rows[0]["data"]).get("baseline") or "draft")
        return spec_body(previous.content), baseline, notes

    def _session_dir(self, session_id: str) -> Path:
        return self.cfg.vigild.data_path / "sessions" / session_id

    # --- stage 1: the coord drafts spec.md and plan.md ------------------------------------

    def run_coord(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if self._refused_by_visibility(job):
            return
        b = self._paused(job)
        if b is not None:
            self._hold_for(job_id, b)
            return
        preset = self._healthy(self.cfg.routing.coord)
        if preset is None:
            self._hold_for(job_id, self._backend_breaker(self.cfg.routing.coord))
            return
        try:
            # the repo's dev image must be fresh before any work starts on it;
            # a new lockfile builds one here (owner direction, round 2)
            session_image = self._session_image(
                job.repo, self.workspace.head_sha(job.repo, job.base)
            )
            check_backend(self.cfg, job.repo, preset, private=live_private(job.repo))
        except (StaleDevImage, RepoPolicyError) as e:
            why = "dev image not fresh" if isinstance(e, StaleDevImage) else "backend refused"
            self._page(
                job_id, f"{job_id}: {why}; coord not started", {"error": str(e)}, resumable=True
            )
            return
        intake = self.store.get_artifact(job_id, "intake.json")
        meta = json.loads(intake.content) if intake else {}
        branch = f"vigil/{job_id}-spec"
        gate_errors: list[str] | None = None
        previous: dict[str, str] = {}
        for attempt in range(1, COORD_DRAFTS + 1):
            # One session per draft, so each draft's spend is booked once (item 8).
            coord = self.store.create_session(
                job_id, Role.COORD, backend_preset=preset, branch=branch
            )
            self.store.set_coord_session(job_id, coord.id)
            try:
                rid = self._reserve_queued(
                    job_id,
                    "coord",
                    session_id=coord.id,
                    task_id=None,
                    amount=self.cfg.budgets.coord_session_usd,
                    purpose="coord",
                )
            except QueueCancelled:  # abandoned while it waited for the day's room
                self.store.update_session(coord.id, status="refused")
                return
            except BudgetExceeded as e:
                self.store.update_session(coord.id, status="refused")
                b = self._refused_budget(job_id, e, None)
                if b is not None:
                    self._hold_for(job_id, b)
                else:
                    self._budget_page(job_id, e, [])
                return
            try:
                draft = self._coord_attempt(
                    job, coord, rid, attempt, branch, meta, gate_errors, previous, session_image
                )
            except Exception:
                self.budget.settle(rid)
                self.store.update_session(coord.id, status="failed")
                raise
            if draft is None:
                return
            gate_errors, previous = draft
            if not gate_errors:
                self.store.update_session(coord.id, status="done")
                self._finish_spec(job_id)
                return
            self.store.update_session(coord.id, status="revise")
            self.store.add_event(
                "spec_gate",
                {"attempt": attempt, "errors": gate_errors},
                Provenance.HUMAN,
                job_id=job_id,
                session_id=coord.id,
            )
        self._page(
            job_id,
            f"{job_id}: the spec gate refused {COORD_DRAFTS} drafts",
            {"errors": gate_errors or []},
            resumable=True,
        )

    def _coord_attempt(
        self,
        job: Any,
        coord: SessionRow,
        rid: int,
        attempt: int,
        branch: str,
        meta: dict[str, Any],
        gate_errors: list[str] | None,
        previous: dict[str, str],
        session_image: str | None = None,
    ) -> tuple[list[str], dict[str, str]] | None:
        job_id = job.id
        # A fresh clone per draft: vigild writes only into clones no session has touched.
        clone_id = f"{job_id}-coord{attempt}"
        clone = self.workspace.clone(job.repo, clone_id, branch=branch, base=job.base)
        base_sha = self.workspace.base_sha(clone_id)
        replan = self._replan_context(job_id)
        amend = self._amend_context(job_id)
        request = {
            "job_id": job_id,
            "repo": job.repo,
            "base": job.base,
            "base_sha": base_sha,
            "branch": branch,
            "title": job.title,
            "request": job.request,
            "notes": job.notes,
            "why": meta.get("why"),
            "value": meta.get("value"),
            "risk": meta.get("risk"),
            **({"amending": True} if amend else {}),
            # a re-plan names every worker with a new id (phase 1 run finding #4)
            "existing_task_ids": [t.id for t in self.store.list_tasks(job_id)],
            **{f"previous_{k}": v for k, v in previous.items()},
        }
        untrusted: dict[str, str] = {}
        issue = issue_text(self.store, job_id)
        if issue is not None:
            untrusted["github issue"] = issue  # label intake: the issue's own text
        if replan:
            # vigild's summary goes in the contract; the failure details carry
            # worker and test output, so they go in the untrusted section
            request["replan"] = {
                tid: {"title": t.get("title"), "state": t.get("state")} for tid, t in replan.items()
            }
            untrusted["previous build failures"] = json.dumps(replan, indent=1)[:12000]
        self.workspace.write_task(clone, request)
        packet = assemble_coord(
            clone, request, untrusted=untrusted, gate_errors=gate_errors, amend=amend
        )
        # Re-plan (item 3): the coord resumes its own last session, its whole
        # context, with the failures in this packet. Phase 6's fix jobs reuse it.
        resume = None
        if replan:
            prior = [
                x
                for x in self.store.list_sessions(job_id)
                if x.role is Role.COORD and x.id != coord.id
            ]
            resume = self._resume_from(prior[-1].id) if prior else None
            if resume is not None:
                self.store.add_event(
                    "fork",
                    {"from_session": resume.from_session, "sdk_session_id": resume.session_id,
                     "entries": len(resume.entries), "replan": True},
                    Provenance.HUMAN, job_id=job_id, session_id=coord.id,
                )  # fmt: skip
        self.store.add_event(
            "packet",
            {"role": "coord", "attempt": attempt, "dropped": packet.dropped},
            Provenance.HUMAN,
            job_id=job_id,
            session_id=coord.id,
        )
        spec = SessionSpec(
            session_id=coord.id,
            role=Role.COORD,
            job_id=job_id,
            task_id=None,
            repo=job.repo,
            preset=coord.backend_preset or "",
            model=self.cfg.preset(coord.backend_preset or "").model or "",
            clone=clone,
            session_dir=self._session_dir(coord.id),
            max_turns=40,
            max_minutes=COORD_MINUTES,
            max_budget_usd=self.cfg.budgets.coord_session_usd,
            mounts=self.workspace.overlays(clone),
            packet=packet.text,
            image=session_image,  # the executor layer over the repo's dev image
            max_output_tokens=self.cfg.budgets.session_max_output_tokens,
            resume=resume,
        )
        out = self._run_session(
            coord,
            spec,
            rid,
            {
                "vigil.job_id": job_id,
                "vigil.role": "coord",
                "vigil.repo": job.repo,
                "vigil.attempt": attempt,
            },
        )
        if out.status != "done":
            self.store.update_session(coord.id, status="failed")
            self._page(
                job_id,
                f"{job_id}: coord session {out.status}",
                {"error": out.error or "", "attempt": attempt},
                resumable=True,
            )
            return None
        try:
            head = self.workspace.import_result(
                job.repo,
                clone_id,
                spec.session_dir / "out",
                branch,
                base_sha,
                head=str(out.result.get("head") or ""),
            )
        except WorkspaceError as e:
            self.store.update_session(coord.id, status="failed")
            self._page(
                job_id, f"{job_id}: coord result unusable", {"error": str(e)}, resumable=True
            )
            return None
        drafts: dict[str, str] = {}
        kept = self.store.get_artifact(job_id, "spec.md")
        human = kept is not None and kept.provenance == Provenance.HUMAN.value
        for name in ("spec.md", "plan.md"):
            if name == "spec.md" and human and amend is None:
                # a coord never rewrites a human-written spec: a replan re-plans only
                # (unless the owner's note asks it to amend)
                assert kept is not None
                drafts["spec"] = kept.content
                continue
            text = self.workspace.read_blob(job.repo, head, f".vigil/{job_id}/{name}") or ""
            drafts[name.removesuffix(".md")] = text
            self.store.add_artifact(
                job_id, name, render(name, text, Provenance.CLAUDE), Provenance.CLAUDE
            )
        assert self.jobs is not None
        if amend is not None:
            missing = self._store_amendment(job, head, drafts["spec"], amend[1])
            if missing:
                return [missing], drafts
        return self.jobs.file_for_approval(job_id), drafts

    def _store_amendment(
        self, job: Any, head: str, amended: str, baseline: str = "approved"
    ) -> str | None:
        """An amendment's record: vigild's diff of the previously approved spec against
        the amended one, and the coord's changelog, which it must have written (the
        error to feed back when it did not)."""
        previous = self.store.get_artifact(job.id, "spec_previous.md")
        assert previous is not None
        label = {
            "approved": "previously approved",
            "approved-pre-versioning": "approved, pre-versioning",
        }.get(baseline, "current draft, never approved")
        diff = redact(spec_diff(previous.content, amended, label))
        self.store.add_artifact(job.id, "spec_diff.md", diff, Provenance.CLAUDE)
        log = self.workspace.read_blob(job.repo, head, f".vigil/{job.id}/spec_changelog.md") or ""
        if not log.strip():
            return (
                f"spec_changelog.md is missing or empty: write .vigil/{job.id}/spec_changelog.md "
                "listing each change you made to the approved spec"
            )
        self.store.add_artifact(
            job.id, "spec_changelog.md", redact(log.strip()[:20_000]) + "\n", Provenance.CLAUDE
        )
        return None

    # --- stage 1b: hidden tests for a task whose scope names a runner file --------------------

    def _finish_spec(self, job_id: str) -> None:
        """The spec passed the gate. For each runner-file task a fresh author
        session writes its hidden tests (the job stays `spec_pending` and no
        inbox item exists meanwhile); then the spec is filed. An author that
        fails twice leaves the task marked "hidden tests missing"."""
        assert self.jobs is not None
        flagged = self.jobs.needs_hidden(job_id)
        if not flagged:
            if self.jobs.flagged_tasks(job_id):  # hidden tests off: no author, just file it
                self.jobs.file_item(job_id)
            return
        if self.jobs.open_spec_item(job_id) is not None:
            return  # an ordinary spec is already filed, or this one is: one item per job
        self._author_hidden_tests(job_id, flagged)
        self.jobs.file_item(job_id)

    def _author_hidden_tests(self, job_id: str, flagged: dict[str, list[str]]) -> None:
        job = self.store.get_job(job_id)
        spec = self.store.get_artifact(job_id, "spec.md")
        plan = self.store.get_artifact(job_id, "plan.md")
        assert spec is not None and plan is not None
        tasks: dict[str, Any] = {}
        for w in parse_plan(plan.content).workers:
            if w.task_id not in flagged:
                continue
            contract = self._task(job_id, job.repo, job.base, job.source, w).model_dump(mode="json")
            try:
                entry = self._author_one(job, spec.content, contract, flagged[w.task_id])
            except Exception as e:  # never leaves the spec unfiled
                log.exception("hidden-test author failed on %s", w.task_id)
                entry = {"status": "missing", "reason": f"vigild error: {type(e).__name__}"}
            tasks[w.task_id] = entry
            self.store.add_event(
                "hidden_tests",
                {
                    "task": w.task_id,
                    "status": entry["status"],
                    "attempts": entry.get("attempts"),
                    "reason": entry.get("reason"),
                    "tests": len(entry.get("tests", [])),
                },
                Provenance.HUMAN,
                job_id=job_id,
            )
        self.store.add_artifact(
            job_id,
            HIDDEN_ARTIFACT,
            json.dumps({"provenance": "claude", "tasks": tasks}),
            Provenance.CLAUDE,
        )

    def _author_one(
        self, job: Any, spec_md: str, contract: dict[str, Any], overlap: list[str]
    ) -> dict[str, Any]:
        """Up to AUTHOR_ATTEMPTS sessions: the second is the one regeneration,
        after a session that failed or tests that failed the wrong way."""
        preset = self.cfg.routing.reviewer  # the reviewer tier (Sonnet)
        if self.verifier.host_key is None:
            # no key, no tag, no hidden test that can count: the task can only escalate
            return {"status": "missing", "reason": "VIGIL_HOST_KEY is not set", "attempts": 0}
        try:
            private = live_private(job.repo)
            check_backend(self.cfg, job.repo, preset, private=private)
            check_hidden_author(self.cfg, job.repo, preset, private=private)
        except RepoPolicyError as e:
            return {"status": "missing", "reason": f"backend refused: {e}", "attempts": 0}
        config = [f for f in overlap if runner_like(f)]
        if config:
            # the flagged files include the hidden runner's own config: no black-box
            # test can stand apart from it, so no author runs and only the owner decides
            return {
                "status": "missing",
                "reason": f"the overlap includes the hidden runner's own config {config}; "
                "no hidden tests are written, owner review only",
                "attempts": 0,
            }
        try:
            base_now = self.workspace.head_sha(job.repo, job.base)  # a fresh mirror
            test_cmd = str(contract["spec"].get("test_cmd") or "")

            def probe(files: list[str]) -> set[str] | None:
                image = self.dev_images.ensure_fresh(job.repo, base_now)
                return self.verifier.collect_probe(job.repo, image, base_now, test_cmd, files)

            place = place_for(
                job.id, str(contract["task_id"]), test_cmd,
                lambda p: self.workspace.mirror_file(job.repo, job.base, p), probe,
            )  # fmt: skip
        except HiddenError as e:
            # refused before any session: no path the repo's runner would collect
            return {"status": "missing", "reason": str(e)[:300], "attempts": 0}
        feedback: str | None = None
        reason = "no session ran"
        task_id = str(contract["task_id"])
        cap = self.cfg.budgets.hidden_tests_session_usd
        for attempt in range(1, AUTHOR_ATTEMPTS + 1):
            # one cap for the whole stage, regeneration included (owner, 2026-10-23)
            left = cap - self._author_spent(job.id, task_id)
            if left < MIN_RETRY_USD:
                return {
                    "status": "missing",
                    "reason": f"the author cap (${cap:.2f} per task) is spent",
                    "attempts": attempt - 1,
                }
            got, reason = self._author_attempt(
                job, spec_md, contract, overlap, preset, attempt, feedback, place, left
            )
            if got is not None:
                return {**got, "attempts": attempt}
            feedback = reason
        return {"status": "missing", "reason": reason, "attempts": AUTHOR_ATTEMPTS}

    def _plan_key(self, job_id: str) -> str:
        """Which plan a stage belongs to. A replan reuses task ids but writes a new
        plan.md, so the author's $1 cap is per task per plan (owner to confirm)."""
        plan = self.store.get_artifact(job_id, "plan.md")
        return hashlib.sha256((plan.content if plan else "").encode()).hexdigest()[:12]

    def _author_spent(self, job_id: str, task_id: str) -> float:
        """What this task's author stage has spent so far under the current plan."""
        key = self._plan_key(job_id)
        return sum(
            session_cost(self.store, s.id)
            for s in self.store.list_sessions(job_id)
            if s.role is Role.HIDDEN_TEST_AUTHOR
            and (s.detail or {}).get("for_task") == task_id
            and (s.detail or {}).get("plan") == key
        )

    def _author_attempt(
        self,
        job: Any,
        spec_md: str,
        contract: dict[str, Any],
        overlap: list[str],
        preset: str,
        attempt: int,
        feedback: str | None,
        place: Place,
        amount: float,
    ) -> tuple[dict[str, Any] | None, str]:
        """One author session. Any exception, in the session or in vigild's
        checks of its output (a verifier crash, say), settles its reservation and
        fails its session before it propagates: `_author_hidden_tests` then files
        the task as "hidden tests missing; this task can only escalate", and no
        session is left `running` for a restart to mistake for a coord."""
        held: dict[str, Any] = {}
        try:
            return self._author_attempt_inner(
                job, spec_md, contract, overlap, preset, attempt, feedback, place, amount, held
            )
        except Exception:
            if (rid := held.get("rid")) is not None:
                self.budget.settle(rid)
            if (sess := held.get("s")) is not None:
                self.store.update_session(sess.id, status="failed", ended_at=iso())
            raise

    def _author_attempt_inner(
        self,
        job: Any,
        spec_md: str,
        contract: dict[str, Any],
        overlap: list[str],
        preset: str,
        attempt: int,
        feedback: str | None,
        place: Place,
        amount: float,
        held: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        job_id, task_id = job.id, str(contract["task_id"])
        s = self.store.create_session(
            job_id,
            Role.HIDDEN_TEST_AUTHOR,
            backend_preset=preset,
            branch=f"vigil/{task_id}-hidden{attempt}",
        )  # no parent: a fresh context, never the coord's or a worker's
        held["s"] = s
        # the task has no row yet at spec time, so the session names it in its detail
        self.store.update_session(
            s.id, detail={"for_task": task_id, "attempt": attempt, "plan": self._plan_key(job_id)}
        )
        try:
            rid = self._reserve_queued(
                job_id, "hidden_test_author", session_id=s.id, task_id=None,
                amount=amount, purpose="hidden_tests",
            )  # fmt: skip
        except QueueCancelled:
            self.store.update_session(s.id, status="refused")
            return None, "the job moved on while the author waited for the day's room"
        except BudgetExceeded as e:
            self.store.update_session(s.id, status="refused")
            return None, f"{e.cap} cap reached"
        held["rid"] = rid
        clone_id, branch = f"{task_id}-ht{attempt}", f"vigil/{task_id}-hidden{attempt}"
        try:
            clone = self.workspace.clone(job.repo, clone_id, branch=branch, base=job.base)
            base_sha = self.workspace.base_sha(clone_id)
            image = self.dev_images.ensure_fresh(job.repo, base_sha)
            session_image = self._session_image(job.repo, base_sha)
            # the author's own contract: it may write exactly its placement path (the
            # harness commits only what is in this scope or under .vigil/)
            own = {
                **contract,
                "spec": {**contract["spec"], "files_in_scope": [f"{place.folder}/**"]},
            }
            self.workspace.write_task(clone, {**own, "branch": branch, "base_sha": base_sha})
            packet = assemble_hidden_test_author(
                clone,
                contract,
                spec_md,
                feedback=feedback,
                folder=place.folder,
                vitest=place.vitest,
                suffix=place.suffix,
                owner_contract="\n\n".join(author_contract(n) for n in self._replan_notes(job_id))
                or None,
            )
            self.store.add_event(
                "packet",
                {"role": "hidden_test_author", "attempt": attempt, "dropped": packet.dropped},
                Provenance.HUMAN, job_id=job_id, session_id=s.id,
            )  # fmt: skip
            spec = SessionSpec(
                session_id=s.id, role=Role.HIDDEN_TEST_AUTHOR, job_id=job_id, task_id=task_id,
                repo=job.repo, preset=preset, model=self.cfg.preset(preset).model or "",
                clone=clone, session_dir=self._session_dir(s.id), max_turns=40,
                max_minutes=COORD_MINUTES,
                max_budget_usd=amount,
                mounts=self.workspace.overlays(clone), packet=packet.text, image=session_image,
                max_output_tokens=self.cfg.budgets.session_max_output_tokens,
            )  # fmt: skip
        except Exception as e:
            self.budget.settle(rid, 0.0)
            self.store.update_session(s.id, status="failed")
            return None, f"setup failed: {type(e).__name__}"
        out = self._run_session(
            s, spec, rid,
            {"vigil.job_id": job_id, "vigil.task_id": task_id, "vigil.role": "hidden_test_author",
             "vigil.repo": job.repo, "vigil.attempt": attempt},
        )  # fmt: skip
        if out.status != "done":
            self.store.update_session(s.id, status="failed")
            return None, f"the author session {out.status}"
        try:
            sha = self.workspace.import_result(
                job.repo, clone_id, spec.session_dir / "out", branch, base_sha,
                head=str(out.result.get("head") or ""),
            )  # fmt: skip
            suite, manifest = load_suite(
                self.workspace,
                job.repo,
                sha,
                base_sha,
                job_id,
                task_id,
                test_cmd_problem,
                place,
                scope=list(contract["spec"].get("files_in_scope") or []),
            )
        except (WorkspaceError, HiddenError) as e:
            self.store.update_session(s.id, status="failed")
            return None, str(e)[:300]
        check = self.verifier.check_hidden_on_base(
            repo=job.repo, image=image, task_id=task_id, base_sha=base_sha, hidden=suite,
            runner_config=self.cfg.repo(job.repo).runner_config, overlap=overlap,
            scope=list(contract["spec"].get("files_in_scope") or []),
        )  # fmt: skip
        if not check["ok"]:
            self.store.update_session(s.id, status="failed")
            return None, str(check.get("why") or "the hidden tests did not fail on the base")
        self.store.update_session(s.id, status="done")
        return {
            "status": "ok",
            "sha": suite.sha,
            "prefix": suite.prefix,
            "project": suite.project,
            "vitest": suite.vitest,
            "files": list(suite.files),
            "test_cmd": suite.test_cmd,
            "tests": [t.model_dump() for t in manifest.tests],
            "author_session": s.id,
            "base_check": {k: v for k, v in check.items() if k != "why"},
        }, ""

    # --- stages 2-3: planning and building -----------------------------------------------

    def _task(self, job_id: str, repo: str, base: str, source: str, w: WorkerContract) -> Task:
        cap = self.cfg.budgets.per_task_usd
        return Task(
            task_id=w.task_id,
            repo=repo,
            base=base,
            branch=f"vigil/{w.task_id}",
            why=w.why,
            value=w.value,
            risk=w.risk,
            job_id=job_id,
            source=source,  # type: ignore[arg-type]
            spec=Spec(
                goal=w.goal,
                test_cmd=w.test_cmd,
                done_when=w.done_when,
                must_fail_first=w.must_fail_first,
                must_fail_first_reason=w.must_fail_first_reason,
                files_in_scope=w.files_in_scope,
                out_of_scope=w.out_of_scope,
                verify=Verify(level=w.verify_level),
            ),
            limits=Limits(max_cost_usd=min(w.max_cost_usd or cap, cap)),
            depends_on=w.depends_on,
            labels=w.labels,
            size=w.size,
            runner_file_change=runner_overlap(
                w.files_in_scope, self.cfg.repo(repo).runner_config, w.test_cmd
            ),
        )

    def build(self, job_id: str) -> None:
        transition(self.store, job_id, JobState.PLANNING, Actor.VIGILD)
        self._plan_and_dispatch(job_id)

    def _plan_and_dispatch(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if self.store.get_artifact(job_id, "deps.json") is not None:
            self._plan_deps(job)
            return
        art = self.store.get_artifact(job_id, "plan.md")
        assert art is not None
        workers = parse_plan(art.content).workers
        planned = {w.task_id for w in workers}
        for old in self.store.list_tasks(job_id):
            if old.id not in planned and old.state not in (*PUBLISHED, *DEAD_TASK):
                self.store.update_task(old.id, state="superseded")  # re-planned away (N5)
        for w in workers:
            if not w.task_id.startswith(f"{job_id}-w"):
                raise RuntimeError(f"plan.md task id {w.task_id} isn't vigild's")  # item 16
            task = self._task(job_id, job.repo, job.base, job.source, w)
            lacking = missing_tools(self.cfg, job.repo, w.test_cmd)
            try:
                if lacking:
                    raise RoutingError(f"the dev image lacks {lacking}")
                r = route(task, self.cfg)
            except RoutingError as e:
                self._block(job_id, f"routing: {e}")
                self._page(
                    job_id, f"{job_id}: {w.task_id} can't run",
                    {"kind": "cant_run", "error": str(e),
                     "actions": ["retry", "replan", "abandon"]},
                    resumable=True,
                )  # fmt: skip
                return
            contract = task.model_copy(
                update={"tier": r.tier, "tier_reason": r.tier_reason, "backend": r.preset}
            ).model_dump(mode="json")
            try:
                existing = self.store.get_task(w.task_id)
                if existing.job_id != job_id:
                    raise RuntimeError(f"task {w.task_id} belongs to {existing.job_id}")
                if existing.state in PUBLISHED:
                    continue  # after a re-plan, a published task's PR stands as it is
                self.store.update_task(w.task_id, contract=contract)
            except KeyError:
                self.store.create_task(job_id, contract, Provenance.CLAUDE)
            self.store.route_task(
                w.task_id, tier=r.tier, tier_reason=r.tier_reason, backend_preset=r.preset
            )
        transition(self.store, job_id, JobState.BUILDING, Actor.VIGILD)
        self.dispatch(job_id)

    def dispatch(self, job_id: str) -> None:
        """Run every live task not yet published as a dependency graph (plan
        2a, item 2): each starts once its own dependencies are published, and
        tasks that become ready together reserve their budgets together. A
        task that would open a new PR waits while the repo is at its WIP
        limit. After a failure nothing new starts, and the job blocks once
        what's running finishes; the owner chooses (N5)."""
        job = self.store.get_job(job_id)
        tasks = {t.id: t for t in self.store.list_tasks(job_id) if t.state not in DEAD_TASK}
        done = {t for t, row in tasks.items() if row.state in PUBLISHED}
        # an escalated task is held out of a retry: it would build the same thing
        # (a new plan and new hidden tests are what help), but the job's other
        # failed tasks still retry (hotfix 5)
        escalated = [t for t, row in tasks.items() if row.state == "escalated"]
        deps = {
            t: list(row.contract.get("depends_on", []))
            for t, row in tasks.items()
            if t not in escalated
        }
        limit = self.cfg.budgets.wip_per_repo
        wip_held: set[str] = set()

        def launch(batch: list[str]) -> dict[str, Callable[[], bool]] | None:
            if self._refused_by_visibility(job):
                return None
            for tid in batch:
                # the WIP breaker holds only work that would open a new PR: a
                # fix round pushes to its PR and drains the stack (review B5)
                b = self._paused(job, tid, building=wip.opens_a_pr(self.store, tid))
                routed = self.store.get_task(tid).backend_preset or self.cfg.routing.fallback
                if b is None and self._healthy(routed) is None:
                    b = self._backend_breaker(routed)  # no healthy backend for it
                if b is not None:
                    self._hold_for(job_id, b)
                    return None
            # the room check, the reservations and the move to running are one
            # step for every job's dispatch at once (review B10)
            with self._wip_lock:
                free = wip.room(self.store, job.repo, limit)
                admitted: list[str] = []
                for tid in batch:
                    if wip.opens_a_pr(self.store, tid):
                        if free <= 0:
                            wip_held.add(tid)
                            continue
                        free -= 1
                    admitted.append(tid)
                if not admitted:
                    return {}
                retrying = [t for t in admitted if self.store.get_task(t).state == "failed"]
                try:
                    held = self._reserve_wave(job_id, admitted)
                except Refused:
                    return None
                for tid in admitted:
                    self.store.update_task(tid, state="running")  # its place in the stack
            routed_before = {tid: self.store.get_task(tid) for tid in retrying}
            try:
                for tid in retrying:
                    # another attempt; the third is routed by rule 5 (escalated)
                    attempt = int(self.store.get_task(tid).contract.get("attempt") or 1)
                    self._reroute(job_id, tid, attempt=attempt + 1)
            except RoutingError as e:
                # nothing reserved is left behind (2a round 8): release the
                # wave, put its tasks back as they were (their attempt and
                # route too, 2a round 9), and page the job
                for rid, _amt in held.values():
                    if rid:
                        self.budget.settle(rid, 0.0)
                for tid, t in routed_before.items():
                    self.store.update_task(tid, contract=t.contract)
                    if t.tier and t.tier_reason and t.backend_preset:  # it ran: it was routed
                        self.store.route_task(tid, tier=t.tier, tier_reason=t.tier_reason,
                                              backend_preset=t.backend_preset)  # fmt: skip
                for tid in admitted:
                    self.store.update_task(tid, state="failed" if tid in retrying else "waiting")
                    self._queue_leave(self._queue_key(job_id, "worker", tid))  # no thread will wait
                self._block(job_id, f"routing: {e}")
                self._page(
                    job_id, f"{job_id}: a retry can't be routed",
                    {"kind": "cant_run", "error": str(e),
                     "actions": ["retry", "replan", "abandon"]},
                    resumable=True,
                )  # fmt: skip
                return None
            # a worker the day had no room for waits in its own thread (the budget
            # queue), so the others run and free the money
            return {
                tid: functools.partial(self._run_worker, job_id, tid, *held[tid])
                if tid in held
                else functools.partial(self._run_queued_worker, job_id, tid)
                for tid in admitted
            }

        result = run_graph(deps, done, launch)
        if cancelled := [t for t in result.failed if t in self._wait_cancelled]:
            # a worker was waiting for the day's room when its job moved on (abandoned, or
            # held by a restart): that move owns the job's page, and nothing failed
            self._wait_cancelled.difference_update(cancelled)
            return
        if result.failed:
            self._hold_failed(
                job_id, result.failed + [t for t in escalated if t not in result.failed]
            )
            return
        if result.halted:
            return  # a budget refusal blocked the job and paged
        waiting = [t for t in result.waiting if t in wip_held]
        if waiting:
            for tid in waiting:
                self.store.update_task(tid, state="waiting")
            self._wip_hold(job_id, job.repo, waiting)
            return
        if escalated:
            self._hold_failed(job_id, escalated)  # what's left waits behind them
            return
        if result.waiting:
            # Nothing can start: a dependency is missing from the plan (item 17).
            stuck = {t: self.store.get_task(t).state for t in result.waiting}
            self._page(
                job_id,
                f"{job_id}: work is stuck behind unfinished dependencies",
                {"pending": stuck, "actions": ["replan", "abandon"]},
            )
            self._block(job_id, f"stuck: {sorted(stuck)}")
            return
        live = [t for t in self.store.list_tasks(job_id) if t.state not in DEAD_TASK]
        if all(t.state in PUBLISHED for t in live) and self._enter_review(job_id):
            self.review(job_id)

    def _enter_review(self, job_id: str) -> bool:
        """The one way vigild moves a job to `in_review`. The FSM's guard refuses it
        unless every task in the current plan has an open PR for that plan's work; the
        job then blocks with a page naming the tasks that have none (finding #4).
        True when the job is in review."""
        try:
            transition(self.store, job_id, JobState.IN_REVIEW, Actor.VIGILD)
        except TransitionError as e:
            gaps = plan_gaps(self.store, job_id)
            if not gaps:
                raise
            self._hold_without_pr(job_id, gaps, str(e))
            return False
        return True

    def _page_if_gaps(self, job_id: str, why: TransitionError) -> None:
        """A move the invariant refused (a resume into review, say) must not leave the job
        blocked with no page: if a plan task has no open PR, page it as reconcile does."""
        try:
            gaps = plan_gaps(self.store, job_id)
        except KeyError:
            return
        if gaps:
            self._hold_without_pr(job_id, gaps, str(why))

    def _hold_without_pr(self, job_id: str, gaps: dict[str, str], why: str) -> None:
        self._block(job_id, f"no open PR for plan task(s) {sorted(gaps)}")
        self._page(
            job_id,
            f"{job_id}: no open PR for {', '.join(sorted(gaps))}; replan or abandon",
            {
                "kind": "plan_without_pr",
                "tasks": {t: {"state": w} for t, w in gaps.items()},
                "error": why,
                "actions": ["replan", "abandon"],
            },
        )

    def _wip_hold(self, job_id: str, repo: str, waiting: list[str]) -> None:
        """The repo is at its WIP limit: its WIP breaker trips (one page, with
        the stack; design "Intake", rule 2) and the job waits on it. Merge or
        close one, then reset it from the inbox."""
        limit = self.cfg.budgets.wip_per_repo
        b = self.breakers.trip(
            Kind.WIP, f"repo:{repo}",
            {"limit": limit, "stack": wip.stack(self.store, repo), "waiting": waiting},
        )  # fmt: skip
        self._hold_for(job_id, b)

    def _verifier_verdict(self, repo: str, job_id: str, tests: dict[str, Any]) -> None:
        """M `unknown` verdicts in a row trip the repo's breaker: a broken
        image or runner, not the worker (item 4). Only runs that couldn't be
        prepared or run count (exit 125-127, or a signal); a worker whose test
        is missing or doesn't collect is the worker's failure, not the image's."""
        runs = [tests.get(r) or {} for r in ("base", "before", "after")]
        outcomes = [r.get("outcome") for r in runs]

        def broken(r: dict[str, Any]) -> bool:
            code = r.get("exit")
            return (
                r.get("outcome") == "unknown"
                and isinstance(code, int)
                and (code in (125, 126, 127) or code >= 128)
            )

        unknown = any(broken(r) for r in runs if r)
        if unknown:
            self.breakers.failure(
                Kind.VERIFIER_UNKNOWNS, f"repo:{repo}", self.cfg.vigild.breakers.verifier_unknowns,
                {"job": job_id, "outcomes": outcomes},
            )  # fmt: skip
        else:
            self.breakers.success(Kind.VERIFIER_UNKNOWNS, f"repo:{repo}")

    def _backend_breaker(self, preset: str) -> Breaker:
        b = self.breakers._row(Kind.BACKEND, f"backend:{preset}")
        return b or self.breakers.trip(Kind.BACKEND, f"backend:{preset}", {"preset": preset})

    def job_failed(self, job: Job) -> None:
        """K failed or abandoned jobs in a row trip the repo's breaker (item 4)."""
        self.breakers.failure(
            Kind.REPO_FAILURES, f"repo:{job.repo}", self.cfg.vigild.breakers.repo_failures,
            {"job": job.id},
        )  # fmt: skip

    def remaining(self, task_id: str) -> tuple[float, float, float]:
        """(cap, spent, left) for a task: a retry gets what's left, never a fresh cap."""
        # the contract's cap, and never more than the task cap in budgets.yaml
        cap = min(
            float(self.store.get_task(task_id).contract["limits"]["max_cost_usd"]),
            self.cfg.budgets.per_task_usd,
        )
        spent = self.budget.task_spent(task_id)
        return cap, spent, max(0.0, cap - spent)

    def _needs_deps(
        self, repo: str, base_sha: str, head: str, report: WorkerReport
    ) -> dict[str, Any] | None:
        """What the worker needs, from its report or from the manifests and
        lockfiles it changed; None when it needs nothing."""
        locks = set(self.cfg.repo(repo).dev_lockfiles)
        touched = sorted({p for _, p in self.workspace.changed(repo, base_sha, head)} & locks)
        deps = [d.model_dump() for d in report.needs_deps]
        inferred: list[dict[str, Any]] = []
        for path in touched:
            inferred += [
                d.model_dump()
                for d in infer_deps(
                    path,
                    self.workspace.read_blob(repo, base_sha, path),
                    self.workspace.read_blob(repo, head, path),
                )
            ]
        # round 4, item 8: only dependency changes (a manifest's dependency
        # fields, or a lockfile proper) are a deps request; a `scripts` edit
        # goes the runner-config way (rejected unless the spec names the file)
        dep_changes = bool(inferred) or any(is_lockfile(p) for p in touched)
        if not deps and not dep_changes:
            return None
        deps = deps or inferred
        diff = "\n".join(self.workspace.diff(repo, base_sha, head, p) for p in touched)
        return {
            "needs_deps": deps,
            "lockfiles_changed": touched,
            "diff": fence("worker manifest and lockfile diff", diff[:20000] or "(no changes)"),
        }

    def _hold_failed(self, job_id: str, task_ids: list[str]) -> None:
        """Block the job with one item: retry (when every failed task has budget
        left), re-plan, or abandon. It counts toward the repo's failures. A task
        that needs a dependency instead gets a child deps job (item 5)."""
        tasks: dict[str, dict[str, Any]] = {}
        lefts: list[float] = []  # budget left of each task a retry would run
        for tid in task_ids:
            cap, spent, left = self.remaining(tid)
            with self._why_lock:
                why = self._why.pop(tid, {})
            tasks[tid] = {
                "state": self.store.get_task(tid).state,
                "cap_usd": round(cap, 4),
                "spent_usd": round(spent, 4),
                "remaining_usd": round(left, 4),
                **why,
            }
            if self.store.get_task(tid).state != "escalated":
                lefts.append(left)
        # retry is offered while every task it would run has budget left
        can_retry = bool(lefts) and all(x >= MIN_RETRY_USD for x in lefts)
        deps = any("needs_deps" in t for t in tasks.values())
        if deps:
            # item 5: a child deps job; this job resumes on its own when it merges
            wanted = [d for t in tasks.values() for d in t.get("needs_deps", [])]
            child = self._spawn_deps(
                job_id, [tid for tid, t in tasks.items() if "needs_deps" in t], wanted
            )
            if child is None:
                return  # the job tree's fix-depth breaker paged and holds it
            self._block(job_id, "needs_deps", blocked_on=child)
            self._page(
                job_id,
                f"{job_id}: needs dependencies: "
                + ", ".join(f"{d['package']}@{d['version']}" for d in wanted)[:250],
                {
                    "kind": "needs_deps",
                    "tasks": tasks,
                    "deps_job": child,
                    "actions": ["abandon"],
                    "next": f"deps job {child} waits in Specs to approve. It changes only the "
                    "manifests and lockfiles and opens its own PR; when that merges, the dev "
                    "image is rebuilt from it and this job resumes on its own.",
                },
            )
            return
        escalated = [t for t in task_ids if self.store.get_task(t).state == "escalated"]
        self.job_failed(self.store.get_job(job_id))
        actions = (["retry"] if can_retry else []) + ["replan", "abandon"]
        self._block(
            job_id,
            f"workers failed: {sorted(task_ids)}",
            state=JobState.ESCALATED if escalated else JobState.BLOCKED,
        )
        # its own page holds it now: no breaker resumes it behind that page's
        # back when it closes (2a round 3); a breaker it leaves holding nothing
        # resolves its page, as abandoning does (2a round 9)
        self.breakers.unhold(job_id, by=Actor.VIGILD.value)
        titles = "; ".join(str(t.get("title") or tid) for tid, t in tasks.items())
        self._page(
            job_id,
            f"{job_id}: {len(task_ids)} worker(s) need you: {titles}"[:300],
            {
                "kind": "escalated" if escalated else "worker_failed",
                "tasks": tasks,
                "actions": actions,
            },
            resumable=can_retry,
        )

    def _run_queued_worker(self, job_id: str, task_id: str) -> bool:
        """A worker the day had no room for: wait for it, then run as any other."""
        _, _, amount = self.remaining(task_id)
        try:
            rid = self._reserve_queued(
                job_id,
                "worker",
                session_id=None,
                task_id=task_id,
                amount=amount,
                purpose="worker",
                queued=True,  # `queued` was emitted when the wave was reserved
                expect=BUILD_WAIT,
            )
        except QueueCancelled:  # the job was abandoned (or moved on) while this waited
            if self.store.get_task(task_id).state == "running":
                self.store.update_task(task_id, state="waiting")
            self._wait_cancelled.add(task_id)
            return False
        except BudgetExceeded as e:  # the month, say: it ran out while this waited
            return self._fail(
                job_id, None, task_id, f"{task_id}: {e.cap} cap reached", {"error": str(e)}
            )
        return self._run_worker(job_id, task_id, rid, amount)

    def _reserve_wave(self, job_id: str, task_ids: list[str]) -> dict[str, tuple[int, float]]:
        """Reserve every worker of a wave before any starts, each for what's
        left of its own cap. A worker the day has no room for is left out of
        the result and queued (it waits in its own thread, so the others run
        and free the money); on any other refusal, release them, block the job
        and page: dispatch halts."""
        held: dict[str, tuple[int, float]] = {}
        for tid in task_ids:
            if self.store.get_task(tid).contract.get("shape") == "deps":
                held[tid] = (0, 0.0)  # a deps job runs no model: nothing to reserve
                continue
            _, _, amount = self.remaining(tid)
            key = self._queue_key(job_id, "worker", tid)
            try:
                if amount < MIN_RETRY_USD:
                    raise BudgetExceeded(
                        "per_task", f"task {tid} has ${amount:.2f} of its cap left"
                    )
                if not self._queue_turn(key):  # an older waiter is owed the day's room first
                    self._queue_join(key)
                    self._queue_event(
                        "queued", job_id, "worker", tid, amount_usd=amount,
                        detail="behind an older waiter",
                    )  # fmt: skip
                    continue
                held[tid] = (
                    self.budget.reserve(
                        session_id=None, task_id=tid, amount=amount, purpose="worker"
                    ),
                    amount,
                )
            except BudgetExceeded as e:
                if self._queueable(e, amount):
                    self._queue_join(key)
                    self._queue_event(
                        "queued", job_id, "worker", tid, amount_usd=amount, detail=e.detail
                    )
                    continue
                for rid, _amt in held.values():
                    if rid:
                        self.budget.settle(rid, 0.0)
                for t2 in task_ids:
                    self._queue_leave(self._queue_key(job_id, "worker", t2))
                for t2 in task_ids:
                    self.store.update_task(t2, state="waiting")
                b = self._refused_budget(job_id, e, tid)
                if b is not None:
                    self._hold_for(job_id, b)
                else:
                    self._budget_page(job_id, e, task_ids)
                raise Refused(str(e)) from e
        return held

    def _fail(
        self,
        job_id: str,
        s: SessionRow | None,
        task_id: str,
        title: str,
        body: dict[str, Any],
        state: str = "failed",
    ) -> bool:
        if s is not None:
            self.store.update_session(s.id, status=state)
        self.store.update_task(task_id, state=state)
        why = redact({"title": title, **body})
        with self._why_lock:
            self._why[task_id] = why
        self.store.add_event("task_failed", why, Provenance.HUMAN, job_id=job_id, task_id=task_id)
        return False

    def _hidden_entry(self, job_id: str, task_id: str) -> dict[str, Any] | None:
        art = self.store.get_artifact(job_id, HIDDEN_ARTIFACT)
        if art is None:
            return None
        entry = json.loads(art.content).get("tasks", {}).get(task_id)
        return entry if isinstance(entry, dict) else None

    def _runner_note(
        self,
        repo: str,
        base_sha: str,
        head: str,
        overlap: list[str],
        tests: dict[str, Any] | None,
        hidden_on: bool = True,
    ) -> str:
        """The PR body's plain statement for a runner-file task: the class is
        durable (`runner_file_change`), the owner looks, hidden tests decided."""
        if not overlap:
            return ""
        changed = {path for _, path in self.workspace.changed(repo, base_sha, head)}
        moved = [p for p in overlap if p in changed]
        files = ", ".join(f"`{p}`" for p in moved or overlap)
        what = "runner file changed" if moved else "runner file in scope, unchanged"
        if not hidden_on:
            return (
                f"**Owner review required: {what}: {files}.** Hidden tests are disabled "
                "(host policy), so the verifier's visible-test result is not enough on its "
                "own: read the runner-file diff yourself. The verifier ran the head's copy "
                "of only that file and the base's copy of every other runner file.\n\n"
            )
        hidden = (tests or {}).get("hidden") or {}
        count = (hidden.get("head") or {}).get("tests")
        return (
            f"**Owner review required: {what}: {files}.** The verifier ran the head's copy of "
            "only that file and the base's copy of every other runner file. Hidden tests "
            "(kept by vigild, not on this branch) failed on the base and pass on the head "
            f"({count} test(s)); they decide, and the visible test is reported but is not "
            "enough.\n\n"
        )

    def _run_worker(self, job_id: str, task_id: str, reservation: int, amount: float) -> bool:
        held: dict[str, SessionRow] = {}
        try:
            if self.store.get_task(task_id).contract.get("shape") == "deps":
                return self._deps_worker(job_id, task_id, held)
            return self._worker(job_id, task_id, reservation, amount, held)
        except Exception as e:  # the reservation never leaks (item 7)
            log.exception("worker %s failed", task_id)
            self.budget.settle(reservation)
            return self._fail(
                job_id,
                held.get("s"),
                task_id,
                f"{task_id}: vigild error",
                {"error": f"{type(e).__name__}: {e}"},
            )

    def _worker(
        self,
        job_id: str,
        task_id: str,
        reservation: int,
        amount: float,
        held: dict[str, SessionRow],
    ) -> bool:
        job = self.store.get_job(job_id)
        t = self.store.get_task(task_id)
        contract = t.contract
        routed = t.backend_preset or self.cfg.routing.fallback
        healthy = self._healthy(routed)
        if healthy is None:
            self.budget.settle(reservation, 0.0)
            self._hold_for(job_id, self._backend_breaker(routed))
            return self._fail(
                job_id, None, task_id, f"{task_id}: no healthy backend", {"preset": routed},
                state="waiting",
            )  # fmt: skip
        preset = healthy  # the route falls to the next preset while a backend's breaker is open
        branch = contract["branch"]
        prev = self._last_worker(task_id)
        s = self.store.create_session(
            job_id,
            Role.WORKER,
            task_id=task_id,
            parent_session_id=job.coord_session_id,
            backend_preset=preset,
            branch=branch,
        )
        held["s"] = s
        # the gateway sees this session's headroom through its reservation
        self.store.x("UPDATE reservations SET session_id=? WHERE id=?", (s.id, reservation))
        # a published task (a fix round, or a retry after one) goes on from its
        # PR's head, on its own base (phase 2a, item 1); a crashed or killed
        # worker from its last checkpoint (item 3)
        published = self._task_head(task_id)
        pr_url = self._task_pr(task_id) if published else None
        crashed = prev is not None and prev.status == "crashed"
        start = (prev.detail.get("checkpoint") if crashed and prev else None) or published
        if start:
            clone = self.workspace.clone(
                job.repo, task_id, branch=branch, base=job.base, start=start,
                record_as=self.workspace.base_sha(task_id),
            )  # fmt: skip
        else:
            clone = self.workspace.clone(job.repo, task_id, branch=branch, base=job.base)
        base_sha = self.workspace.base_sha(task_id)
        try:
            image = self.dev_images.ensure_fresh(job.repo, base_sha)  # what the verifier runs
            session_image = self._session_image(job.repo, base_sha)  # what the worker runs in
        except StaleDevImage as e:
            self.budget.settle(reservation, 0.0)
            return self._fail(
                job_id,
                s,
                task_id,
                f"{task_id}: dev image not fresh",
                {"error": str(e)},
                state="waiting",
            )
        paths = []
        session_base = base_sha  # worker changes are counted from vigild's own commit
        for name in ("spec.md", "plan.md", "approval.json"):
            art = self.store.get_artifact(job_id, name)
            if art is not None:
                path = clone / ".vigil" / job_id / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(art.content)
                paths.append(f".vigil/{job_id}/{name}")
        if paths and not start:
            session_base = self.workspace.commit_before_session(
                clone, paths, f"vigil: {job_id} spec and plan"
            )
        elif start and prev is not None and prev.detail.get("session_base"):
            # a resume or a fix round goes on from earlier work: changes still count from
            # vigild's own commit of the spec and plan, made before the first session
            session_base = str(prev.detail["session_base"])
        after_deps: list[str] = []
        if prev is not None and not start and prev.detail.get("needs_deps_head"):
            # item 5: its dependency has landed; it goes on from the new base, its
            # earlier changes laid over (manifests and lockfiles as main has them)
            locks = set(self.cfg.repo(job.repo).dev_lockfiles)
            after_deps = self.workspace.overlay(
                clone, job.repo, str(prev.detail.get("base_sha")),
                str(prev.detail["needs_deps_head"]), skip=locks,
            )  # fmt: skip
        self.workspace.write_task(clone, {**contract, "base_sha": base_sha})
        approval = self.store.get_artifact(job_id, "approval.json")
        edits = json.loads(approval.content).get("edits") if approval else None
        packet = assemble_worker(
            clone, contract, untrusted={}, approval_edits=edits, fix_round=self._fix_round(t)
        )
        # The same worker goes on: a fix round (or a retry after one) forks its
        # last session's transcript, and so does an escalated attempt, from
        # the failed attempt's whole context, not a summary (item 2).
        # A crashed worker resumes its own transcript, and its turn count
        # carries on (item 3).
        escalated = str(t.tier_reason or "").startswith("rule5")
        resume = (
            self._resume_from(prev.id)
            if prev and (published or escalated or crashed or after_deps)
            else None
        )
        if after_deps:
            packet.parts["contract"] += (
                "\n**Your dependency has landed on the base.** Your earlier changes are in "
                "the work tree, uncommitted: "
                + ", ".join(f"`{p}`" for p in after_deps[:30])
                + ". Finish the task with the dependency available; don't change manifests "
                "or lockfiles.\n"
            )
        max_turns = int(contract.get("limits", {}).get("max_turns") or 60)
        if crashed:
            max_turns = max(1, max_turns - self._turns_used(resume))
        if resume is not None:
            self.store.add_event(
                "fork",
                {"from_session": resume.from_session, "sdk_session_id": resume.session_id,
                 "entries": len(resume.entries), "escalated": escalated, "crashed": crashed,
                 "start": start},
                Provenance.HUMAN, job_id=job_id, session_id=s.id, task_id=task_id,
            )  # fmt: skip
        self.store.add_event(
            "packet",
            {"role": "worker", "dropped": packet.dropped},
            Provenance.HUMAN,
            job_id=job_id,
            session_id=s.id,
            task_id=task_id,
        )
        self.store.update_task(task_id, state="running")
        spec = SessionSpec(
            session_id=s.id,
            role=Role.WORKER,
            job_id=job_id,
            task_id=task_id,
            repo=job.repo,
            preset=preset,
            model=self.cfg.preset(preset).model or "",
            clone=clone,
            session_dir=self._session_dir(s.id),
            max_turns=max_turns,
            max_minutes=WORKER_MINUTES,
            max_budget_usd=amount,  # what's left of the task's cap (N5)
            mounts=self.workspace.overlays(clone),
            packet=packet.text,
            image=session_image,  # the verifier runs the plain dev image, `image`
            max_output_tokens=self.cfg.budgets.session_max_output_tokens,
            resume=resume,
        )
        attrs = {
            "vigil.job_id": job_id,
            "vigil.task_id": task_id,
            "vigil.role": "worker",
            "vigil.repo": job.repo,
            "vigil.tier": t.tier or "",
            "vigil.branch": branch,
            "vigil.model": spec.model,
            "vigil.attempt": int(contract.get("attempt", 1)),
        }
        self.store.update_session(
            s.id, detail={"image": session_image, "session_base": session_base}
        )
        out = self._run_session(s, spec, reservation, attrs)
        if out.status in ("failed", "killed") and not out.result.get("status"):
            # no result line: the container crashed or was killed (item 3)
            self.store.update_session(s.id, status="crashed")
            self.store.add_event(
                "crashed", {"error": (out.error or "")[:500], "exit": out.exit_code},
                Provenance.HUMAN, job_id=job_id, session_id=s.id, task_id=task_id,
            )  # fmt: skip
            if self._resumable(task_id):
                _, _, left = self.remaining(task_id)
                try:
                    rid = self.budget.reserve(
                        session_id=None, task_id=task_id, amount=left, purpose="worker"
                    )
                except BudgetExceeded as e:
                    return self._fail(
                        job_id, None, task_id, f"{task_id}: crashed", {"error": str(e)}
                    )
                return self._worker(job_id, task_id, rid, left, held)
            return self._fail(
                job_id, None, task_id, f"{task_id}: worker crashed",
                {"error": out.error or "", "resumes": self._crashes(task_id)},
            )  # fmt: skip
        hook: dict[str, Any] = {
            k: out.result[k] for k in ("stop_hook_blocks", "stop_hook_exhausted") if k in out.result
        }
        if hook:
            self.store.update_session(s.id, detail=hook)  # the Stop hook's record (item 6)
        if out.status != "done":
            return self._fail(
                job_id,
                s,
                task_id,
                f"{task_id}: worker session {out.status}",
                {"error": out.error or ""},
            )
        try:
            head = self.workspace.import_result(
                job.repo,
                task_id,
                spec.session_dir / "out",
                branch,
                base_sha,
                head=str(out.result.get("head") or ""),
            )
        except WorkspaceError as e:
            return self._fail(job_id, s, task_id, f"{task_id}: result unusable", {"error": str(e)})
        art_dir = f".vigil/{job_id}/{task_id}"
        ask = self.workspace.read_blob(job.repo, head, f"{art_dir}/ask_coord.json", 16 << 10)
        if ask is not None:
            try:
                question = str(json.loads(ask).get("question", ""))[:4000]
            except json.JSONDecodeError, AttributeError:
                question = "(unreadable ask_coord.json)"
            self.store.create_inbox_item(
                "questions",
                "ask_coord",
                job_id,
                f"{task_id} asks its coord",
                {
                    "question": question,
                    "task_id": task_id,
                    "coord_session_id": job.coord_session_id,
                    "resumable": False,
                },
                Provenance.CLAUDE,
            )
            return self._fail(
                job_id,
                s,
                task_id,
                f"{task_id} asks its coord",
                {"question": question},
                state="waiting",
            )
        raw = self.workspace.read_blob(job.repo, head, f"{art_dir}/worker_report.json", 256 << 10)
        try:
            report = WorkerReport.model_validate_json(raw or "")
        except ValidationError as e:
            return self._fail(
                job_id,
                s,
                task_id,
                f"{task_id}: no valid worker_report.json",
                {"error": str(e)[:1000]},
            )
        needs = self._needs_deps(job.repo, base_sha, head, report)
        if needs is not None:
            # owner direction, round 3: a dependency change is its own (deps) job;
            # this task waits for it instead of being verified against the base
            if s is not None:
                self.store.update_session(
                    s.id, status="waiting", detail={"needs_deps_head": head, "base_sha": base_sha}
                )
            self.store.update_task(task_id, state="needs_deps")
            with self._why_lock:
                self._why[task_id] = {"title": f"{task_id} needs dependencies", **needs}
            self.store.add_event(
                "needs_deps", redact(needs), Provenance.HUMAN, job_id=job_id, task_id=task_id
            )
            return False
        tests = None
        test_cmd = str(contract["spec"].get("test_cmd") or "")
        # owner ruling, 2026-10-23: vigild computes the class itself; the contract's
        # marker is the durable copy, not the authority
        overlap = runner_overlap(
            contract["spec"].get("files_in_scope") or [],
            self.cfg.repo(job.repo).runner_config,
            test_cmd,
        )
        # host policy: off (the default), nothing hidden is looked up, run or tagged, and
        # the task is verified on its visible tests and goes to the owner as a draft PR
        hidden_on = (
            self.jobs.hidden_enabled(job_id)
            if self.jobs is not None
            else self.cfg.repo(job.repo).hidden_tests.enabled
        )
        hidden_entry = self._hidden_entry(job_id, task_id) if overlap and hidden_on else None
        if overlap and hidden_on and self.verifier.host_key is None:
            # without the key nothing hidden can count: escalate, never retry forever
            hidden_entry = {"status": "missing", "reason": "VIGIL_HOST_KEY is not set"}
        if test_cmd.strip():
            try:
                tests = self.verifier.verify(
                    repo=job.repo,
                    image=image,
                    task_id=task_id,
                    base_sha=base_sha,
                    head=head,
                    test_cmd=test_cmd,  # the approved spec's, never the worker's tree
                    must_fail_first=bool(contract["spec"].get("must_fail_first", True)),
                    runner_config=self.cfg.repo(job.repo).runner_config,
                    runner_overlap=overlap,
                    hidden=entry_suite(hidden_entry),
                    scope=list(contract["spec"].get("files_in_scope") or []),
                )
            except Exception as e:  # docker or git trouble: no verdict, nothing accepted
                log.exception("verifier failed on %s", task_id)
                return self._fail(
                    job_id, s, task_id, f"{task_id}: verifier error", {"error": str(e)[:1000]}
                )
            if overlap and not hidden_on:
                tests["hidden_disabled"] = True
            elif overlap and "hidden" not in tests:
                reason = (hidden_entry or {}).get("reason") or "none were written"
                tests["hidden"] = {"present": False, "reason": reason}
            self.store.add_artifact(
                job_id, "tests.json", json.dumps(redact(tests)), Provenance.HUMAN, task_id=task_id
            )
            self._verifier_verdict(job.repo, job_id, tests)
        # the report keeps its narrative; its test fields are the verifier's
        report = report.model_copy(
            update={
                "test_before": as_outcome((tests or {}).get("before")),
                "test_after": as_outcome((tests or {}).get("after")),
            }
        )
        self.store.add_artifact(
            job_id,
            "worker_report.json",
            report.model_dump_json(),
            Provenance.CLAUDE,
            task_id=task_id,
        )
        errors = check_worker(
            self.workspace,
            job.repo,
            base_sha,
            head,
            contract,
            report,
            tests,
            cost=session_cost(self.store, s.id),
            runner_config=self.cfg.repo(job.repo).runner_config,
            session_base=session_base,
        )
        if errors and escalates(tests) and all(e.startswith(NO_HIDDEN) for e in errors):
            # built and verified, but with no hidden tests it can never pass
            self.store.add_event(
                "escalated",
                {"task": task_id, "runner_files": overlap, "reason": errors[0]},
                Provenance.HUMAN,
                job_id=job_id,
                task_id=task_id,
            )
            return self._fail(
                job_id,
                s,
                task_id,
                f"{task_id}: escalated: runner file task without hidden tests",
                {"escalate": errors[0], "runner_files": overlap},
                state="escalated",
            )
        if errors:
            return self._fail(
                job_id, s, task_id, f"{task_id}: stage 3 acceptance failed", {"errors": errors}
            )
        runner_note = self._runner_note(job.repo, base_sha, head, overlap, tests, hidden_on)
        body = (
            f"{report.summary}\n\n{runner_note}Job `{job_id}`, task `{task_id}` (tier `{t.tier}`: "
            f"{t.tier_reason}).\n\nTest: `{contract['spec']['test_cmd']}` "
            f"{report.test_before.value} → {report.test_after.value} (run by vigild's verifier)."
            f"\n\nSelf-check: would flag: {report.self_check.reviewer_would_flag}; not done: "
            f"{report.self_check.not_done}.\n\nOpened by vigild; a human merges."
        )
        try:
            url = self.publisher.publish(
                repo=job.repo,
                head=head,
                base_sha=base_sha,
                branch=branch,
                base=job.base,
                title=(
                    OWNER_REVIEW_TITLE
                    if overlap and not hidden_on
                    else f"[vigil {job_id}] {contract['spec']['goal']}"
                ),
                body=body,
                scope=[
                    *contract["spec"]["files_in_scope"],
                    *artifact_paths(job_id, task_id),
                    *vigild_paths(job_id),
                ],
                existing=pr_url,
            )
        except PublishError as e:
            return self._fail(job_id, s, task_id, f"{task_id}: publish refused", {"error": str(e)})
        self.store.update_session(s.id, status="pr_open", pr_url=url, detail={"head": head})
        self.store.update_task(task_id, state="pr_open")
        self.store.add_event(
            "pr_open",
            {
                "pr_url": url,
                "branch": branch,
                "head": head,
                "runner_file_change": overlap,
                **self._pub_facts(job.repo, branch, base_sha),
            },
            Provenance.HUMAN,
            job_id=job_id,
            session_id=s.id,
            task_id=task_id,
        )
        return True

    # --- deps jobs (phase 2a, item 5; B39) ------------------------------------------------

    def _spawn_deps(
        self, parent_id: str, tasks: list[str], wanted: list[dict[str, Any]]
    ) -> str | None:
        """A child deps job for what a job's tasks need, filed for the owner's
        approval like any spec. Past the fix depth, its breaker trips instead."""
        from vigild.contract.models import DepRequest

        parent = self.store.get_job(parent_id)
        reqs: list[DepRequest] = []
        for d in wanted:
            try:
                r = DepRequest.model_validate(d)
            except ValidationError:
                continue
            if all((r.ecosystem, r.package) != (x.ecosystem, x.package) for x in reqs):
                reqs.append(r)
        names = ", ".join(f"{r.package}=={r.version}" for r in reqs)[:150]
        child = self.child_job(
            parent_id, repo=parent.repo, base=parent.base, title=f"deps: {names}",
            request=f"Add {names} for {', '.join(tasks)} (from {parent_id}).", notes=[],
            source="self", provenance=Provenance.CLAUDE,
        )  # fmt: skip
        if child is None:
            return None
        assert self.jobs is not None
        self.jobs.file_deps(child.id, reqs, parent_id, tasks)  # type: ignore[attr-defined]
        self.store.add_event(
            "deps_job", {"child": child.id, "deps": [r.model_dump() for r in reqs]},
            Provenance.HUMAN, job_id=parent_id,
        )  # fmt: skip
        return child.id

    def _plan_deps(self, job: Job) -> None:
        """Planning for a deps job: one task, shape deps, no model."""
        doc = json.loads(self.store.get_artifact(job.id, "deps.json").content)  # type: ignore[union-attr]
        tid = f"{job.id}-w1"
        lockfiles = list(self.cfg.repo(job.repo).dev_lockfiles)
        task = Task(
            task_id=tid,
            repo=job.repo,
            base=job.base,
            branch=f"vigil/{tid}",
            why=job.request,
            value=3,
            risk=2,
            job_id=job.id,
            source="self",  # type: ignore[arg-type]
            spec=Spec(
                goal=job.title,
                files_in_scope=lockfiles,
                done_when="the manifests and lockfiles hold the requested versions; the dev "
                "image rebuilds and its suites pass offline",
            ),
            limits=Limits(max_cost_usd=0.01),
            labels=["deps"],
            shape="deps",  # type: ignore[arg-type]
            parent_task=",".join(doc.get("parent_tasks") or []) or None,
        )
        contract = task.model_dump(mode="json")
        try:
            self.store.get_task(tid)
            self.store.update_task(tid, contract=contract)
        except KeyError:
            self.store.create_task(job.id, contract, Provenance.HUMAN)
        self.store.route_task(
            tid, tier="deps", tier_reason="shape:deps (no model; B39)", backend_preset="none"
        )
        transition(self.store, job.id, JobState.BUILDING, Actor.VIGILD)
        self.dispatch(job.id)

    def _deps_worker(self, job_id: str, task_id: str, held: dict[str, SessionRow]) -> bool:
        """The deps executor (B39): no model. The lockfile-only update in a
        registry-only build container, the checks, vigild's own commit, the
        dev image rebuilt and validated on the new lockfiles, and the PR."""
        from vigild.contract.models import DepRequest

        if self.deps is None:
            return self._fail(job_id, None, task_id, f"{task_id}: no deps executor", {})
        job = self.store.get_job(job_id)
        t = self.store.get_task(task_id)
        doc = json.loads(self.store.get_artifact(job_id, "deps.json").content)  # type: ignore[union-attr]
        reqs = [DepRequest.model_validate(d) for d in doc["deps"]]
        branch = str(t.contract["branch"])
        s = self.store.create_session(
            job_id, Role.WORKER, task_id=task_id, backend_preset="none", branch=branch,
            provenance=Provenance.HUMAN,
        )  # fmt: skip
        held["s"] = s
        self.store.update_session(s.id, status="running", started_at=iso(), detail={"deps": True})
        self.store.update_task(task_id, state="running")
        clone = self.workspace.clone(job.repo, task_id, branch=branch, base=job.base)
        base_sha = self.workspace.base_sha(task_id)
        lockfiles = list(self.cfg.repo(job.repo).dev_lockfiles)
        try:
            image = self.dev_images.ensure_fresh(job.repo, base_sha)
        except StaleDevImage as e:
            return self._fail(
                job_id, s, task_id, f"{task_id}: dev image not fresh", {"error": str(e)}
            )
        res = self.deps.run(job.repo, base_sha, lockfiles, reqs, image)
        self.store.add_artifact(
            job_id, "deps_report.json",
            json.dumps({"added": res.added, "errors": res.errors, "output": res.output}),
            Provenance.HUMAN, task_id=task_id,
        )  # fmt: skip
        if res.errors:
            return self._fail(
                job_id, s, task_id, f"{task_id}: deps job refused",
                {"errors": res.errors, "added": res.added},
            )  # fmt: skip
        names = ", ".join(f"{r.package}=={r.version}" for r in reqs)
        head = self.workspace.commit_files(clone, task_id, res.files, f"deps: {names}")
        # No dev image is built from these lockfiles before the owner merges
        # them (review A5): building one runs the package manager with the
        # build network. The parent's next session builds it from the merged
        # base and validates it offline; a failure pages (rebuild or dismiss).
        report = WorkerReport.model_validate({
            "task_id": task_id, "job_id": job_id, "files_changed": sorted(res.files),
            "summary": f"Adds {names}. New in the lockfiles: {', '.join(res.added) or 'none'}.",
            "self_check": {"test_failed_first": False, "outside_scope": [], "not_done": "nothing",
                           "reviewer_would_flag": "the new packages", "could_delete": "nothing"},
            "provenance": "human",
        })  # fmt: skip
        self.store.add_artifact(
            job_id, "worker_report.json", report.model_dump_json(), Provenance.HUMAN,
            task_id=task_id,
        )  # fmt: skip
        body = (
            f"Deps job `{job_id}` (no model; B39). Adds {names} for `{job.parent_job_id}`.\n\n"
            f"New in the lockfiles, each at least 14 days old by the newest upload time of "
            f"the files the lock pins: {', '.join(res.added) or 'none'}. No source builds: "
            "every package is a prebuilt wheel or tarball.\n\nThe dev image is rebuilt from "
            "these lockfiles only after you merge, and its suites run offline then. Shape "
            "`deps`, rung 1: **the owner reviews every deps PR.** Opened by vigild; a human "
            "merges."
        )
        try:
            url = self.publisher.publish(
                repo=job.repo, head=head, base_sha=base_sha, branch=branch, base=job.base,
                title=f"[vigil {job_id}] deps: {names}"[:200], body=body, scope=lockfiles,
            )  # fmt: skip
        except PublishError as e:
            return self._fail(job_id, s, task_id, f"{task_id}: publish refused", {"error": str(e)})
        self.store.update_session(
            s.id, status="pr_open", pr_url=url, ended_at=iso(), detail={"head": head}
        )
        self.store.update_task(task_id, state="pr_open")
        self.store.add_event(
            "pr_open", {"pr_url": url, "branch": branch, "head": head,
                        **self._pub_facts(job.repo, branch, base_sha)},
            Provenance.HUMAN, job_id=job_id, session_id=s.id, task_id=task_id,
        )  # fmt: skip
        return True

    def poll_prs(self) -> list[str]:
        """Merged and closed fleet PRs (vigild's PR poll, item 5): a ready job
        whose PRs have all merged moves to merged, and every job waiting on it
        (a deps job's parent) resumes on its own. Returns the jobs merged."""
        if self.pr_state is None:
            return []
        merged_jobs: list[str] = []
        for job in self.store.list_jobs():
            if job.state in (JobState.ABANDONED, JobState.DONE, JobState.MERGED):
                continue
            states: dict[str, str] = {}
            closed_tasks: list[str] = []
            for t in self.store.list_tasks(job.id):
                url = self._task_pr(t.id)
                if not url or t.state in ("merged", "closed", *DEAD_TASK):
                    if t.state == "merged":
                        states[t.id] = "merged"
                    continue
                try:
                    st = self.pr_state(job.repo, url)
                except Exception:
                    log.warning("pr poll: %s failed", url)
                    continue
                states[t.id] = st
                if st in ("merged", "closed"):
                    self.store.update_task(t.id, state=st)
                    self.store.add_event(
                        f"pr_{st}", {"pr_url": url}, Provenance.HUMAN, job_id=job.id, task_id=t.id
                    )
                    if st == "closed" and t.contract.get("shape") != "deps":
                        closed_tasks.append(t.id)
            if closed_tasks and self.store.get_job(job.id).state in (
                JobState.BUILDING,
                JobState.IN_REVIEW,
                JobState.READY,
            ):
                # a live job's PR was closed by hand: replan or abandon, not drift
                gaps = plan_gaps(self.store, job.id)
                if gaps:
                    self._hold_without_pr(job.id, gaps, f"PR closed: {sorted(closed_tasks)}")
                continue
            live = [t for t in self.store.list_tasks(job.id) if t.state not in DEAD_TASK]
            if not live or any(t.state != "merged" for t in live):
                continue
            # merged however it happened (review B7): through vigild's review
            # (ready), by the owner while the review ran (in_review), or by
            # hand while a page held the job (blocked)
            evidence: dict[str, Any] = {"merge_commits": [self._task_head(t.id) for t in live]}
            if job.state in (JobState.BLOCKED, JobState.ESCALATED):
                evidence["action"] = "prs_merged"
            elif job.state not in (JobState.READY, JobState.IN_REVIEW):
                continue
            try:
                transition(self.store, job.id, JobState.MERGED, Actor.VIGILD, evidence=evidence)
            except TransitionError as e:
                log.warning("pr poll: %s not moved to merged: %s", job.id, e)
                continue
            for item in self.store.list_inbox():
                if item.job_id == job.id:
                    self.store.resolve_inbox_item(item.id, "vigild")  # nothing left to act on
            merged_jobs.append(job.id)
            self._resume_dependents(job.id)
        return merged_jobs

    def _resume_dependents(self, dep_id: str) -> None:
        """Every job blocked on this (deps) job goes back to work (B39)."""
        for job in self.store.list_jobs():
            if job.state is not JobState.BLOCKED or job.blocked_on != dep_id:
                continue
            assert job.blocked_from is not None
            try:
                transition(
                    self.store, job.id, job.blocked_from, Actor.VIGILD,
                    evidence={"action": "deps_merged", "deps_job": dep_id},
                )  # fmt: skip
            except TransitionError as e:
                log.warning("could not resume %s after %s merged: %s", job.id, dep_id, e)
                self._page_if_gaps(job.id, e)
                continue
            for item in self.store.list_inbox():
                if item.job_id == job.id and item.body.get("kind") == "needs_deps":
                    self.store.resolve_inbox_item(item.id, "vigild")
            self.on_resume(job.id)

    # --- stages 5-6: review and audit (phase 2a, item 1) ---------------------------------

    def _task_head(self, task_id: str) -> str | None:
        """The head a task's draft PR last published, from vigild's own record."""
        rows = self.store.q(
            "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return str(json.loads(rows[0]["data"])["head"]) if rows else None

    def _pub_facts(self, repo: str, branch: str, base_sha: str) -> dict[str, Any]:
        """What reached the target repo, from vigild's own record and the vault: the
        commit sent, and whether its `.vigil` differs from the base's (None: unknown)."""
        try:
            sha = self.workspace.published_sha(repo, branch)
            leak = None if sha is None else self.workspace.has_vigil(repo, sha, base_sha)
        except WorkspaceError:
            sha, leak = None, None
        return {"base_sha": base_sha, "published": sha, "vigil_in_pr": leak}

    def _live_pr_rows(self, job_id: str) -> dict[str, dict[str, Any] | None]:
        """Each live, unmerged task's latest `pr_open` event data (None: no row)."""
        out: dict[str, dict[str, Any] | None] = {}
        for t in self.store.list_tasks(job_id):
            if t.state in (*DEAD_TASK, "merged"):
                continue
            rows = self.store.q(
                "SELECT data FROM events WHERE task_id=? AND type='pr_open' "
                "ORDER BY id DESC LIMIT 1",
                (t.id,),
            )
            out[t.id] = json.loads(rows[0]["data"]) if rows else None
        return out

    def _pr_heads(self, job_id: str) -> dict[str, dict[str, Any]]:
        """The `ready` guard's evidence, read now from the vault: each task's last
        commit sent and whether its `.vigil` differs from the base's."""
        job = self.store.get_job(job_id)
        heads: dict[str, dict[str, Any]] = {}
        for tid, data in self._live_pr_rows(job_id).items():
            if data is None:
                continue
            try:
                base_sha = data.get("base_sha") or self.workspace.base_sha(tid)
                facts = self._pub_facts(job.repo, data["branch"], base_sha)
            except WorkspaceError, KeyError, OSError, ValueError:
                continue
            heads[tid] = {"published": facts["published"], "vigil": facts["vigil_in_pr"]}
        return heads

    def _unstripped(self, job: Job) -> tuple[dict[str, str], set[str]]:
        """Tier 1: each live task whose PR may not match what vigild means to have
        sent (no record, a record behind the vault head, `.vigil/` on it), as
        {task: why}, and the ones only the owner can fix. A task that needs sending
        again is sent, fast-forward only, to the same PR; one whose event is merely
        stale gets a fresh event."""
        left: dict[str, str] = {}
        legacy: set[str] = set()
        for tid, data in self._live_pr_rows(job.id).items():
            if data is None:
                left[tid] = "no PR record"
                continue
            t = self.store.get_task(tid)
            try:
                base_sha = data.get("base_sha") or self.workspace.base_sha(tid)
                facts = self._pub_facts(job.repo, data["branch"], base_sha)
                want = self.workspace.public_commit(
                    job.repo, data["head"], base_sha, data["branch"]
                )
                if facts["published"] != want or facts["vigil_in_pr"] is not False:
                    spec = t.contract.get("spec") or {}
                    self.publisher.publish(
                        repo=job.repo, head=data["head"], base_sha=base_sha, branch=data["branch"],
                        base=job.base, title="", body="", existing=data["pr_url"],
                        scope=[*(spec.get("files_in_scope") or []), *artifact_paths(job.id, tid),
                               *vigild_paths(job.id)],
                    )  # fmt: skip
                    facts = self._pub_facts(job.repo, data["branch"], base_sha)
            except LegacyBranch as e:
                left[tid] = str(e)[:300]
                legacy.add(tid)
                continue
            except (PublishError, WorkspaceError, KeyError, OSError, ValueError) as e:
                left[tid] = f"its PR could not be brought up to date without .vigil/: {e}"[:300]
                continue
            if facts["vigil_in_pr"] is not False or facts["published"] != want:
                left[tid] = ".vigil/ is still on its PR head"
                continue
            if data.get("published") != facts["published"] or data.get("vigil_in_pr") is not False:
                self.store.add_event(
                    "pr_open", {**data, **facts}, Provenance.HUMAN, job_id=job.id, task_id=tid
                )
        return left, legacy

    def _task_pr(self, task_id: str) -> str | None:
        rows = self.store.q(
            "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return str(json.loads(rows[0]["data"])["pr_url"]) if rows else None

    def _fix_round(self, t: Any) -> dict[str, Any] | None:
        """The findings a task's latest review round sent back, if any are open."""
        rounds = int(t.contract.get("review_rounds") or 0)
        art = self.store.get_artifact(t.job_id, "findings.json", t.id)
        if not rounds or art is None:
            return None
        doc = json.loads(art.content)
        if int(doc.get("round", 0)) != rounds:
            return None
        out: dict[str, Any] = {"round": rounds, "findings": doc.get("findings") or []}
        if doc.get("source") == "owner":  # "fix with findings": the owner's pick and note
            out["owner"] = True
            note = self.store.get_artifact(t.job_id, "fix_note.json", t.id)
            if note is not None and int(json.loads(note.content).get("round", 0)) == rounds:
                out["note"] = json.loads(note.content)["note"]
        return out

    def fix_pr_state(self, job_id: str, task_id: str) -> str | None:
        """Why the owner's fix can't start because of GitHub's live view of the task's
        PR (merged or closed since vigild's poll), if it can't. This is a network
        call: the caller makes it outside any DB transaction."""
        if self.pr_state is not None and (url := self._task_pr(task_id)):
            try:
                st = self.pr_state(self.store.get_job(job_id).repo, url)
            except Exception:
                st = "open"  # the poll will see it; the fix round's push will fail if not
            if st in ("merged", "closed"):
                return f"the PR of {task_id} is {st}"
        return None

    def fix_check(self, job_id: str, task_id: str) -> str | None:
        """Why the owner's "fix with findings" can't start for a task, from vigild's
        recorded state only (no network): it has used its review-round limit (the one
        a review's own fix rounds hit), or too little of its cap is left."""
        t = self.store.get_task(task_id)
        limit = self.cfg.routing.escalate_review_rounds
        used = int(t.contract.get("review_rounds") or 0)
        if used >= limit:
            return (
                f"{task_id} has used its {limit} review rounds (the limit "
                f"routing.escalate_review_rounds); replan, or merge as is"
            )
        cap, spent, left = self.remaining(task_id)
        if t.contract.get("shape") != "deps" and left < MIN_RETRY_USD:
            return (
                f"task {task_id} has ${left:.2f} of its ${cap:.2f} cap left "
                f"(${spent:.2f} spent); a fix round needs at least ${MIN_RETRY_USD:.2f}"
            )
        return None

    def fix_escalation(self, job_id: str, task_id: str) -> str | None:
        """The notice for the owner when the next fix round would trip routing rule 5
        and move the task to a pricier backend, else None."""
        t = self.store.get_task(task_id)
        rnd = int(t.contract.get("review_rounds") or 0) + 1
        r = route(Task.model_validate({**t.contract, "review_rounds": rnd}), self.cfg)
        if r.tier_reason.startswith("rule5") and not str(t.tier_reason or "").startswith("rule5"):
            return f"this round runs on {r.preset} (escalated)"
        return None

    def prepare_fix(self, job_id: str, task_id: str) -> int:
        """Count the owner's fix as a review round and route the task again, as a
        review that sent findings back does (rule 5 applies at the second round).
        Returns the round."""
        rnd = int(self.store.get_task(task_id).contract.get("review_rounds") or 0) + 1
        self._reroute_for_round(job_id, task_id, rnd)
        return rnd

    def review(self, job_id: str) -> None:
        """Review every published task at its head (a Sonnet reviewer, fresh
        context, read-only tools), audit where the ladder samples (Opus), and
        move the job: all clean is `ready`; a finding of medium or higher goes
        back to the same worker as a fix round; a third round with findings,
        after rule 5 escalated the second, pages the owner."""
        job = self.store.get_job(job_id)
        if job.state is not JobState.IN_REVIEW:
            return
        if self._refused_by_visibility(job):
            return
        b = self._paused(job)
        if b is not None:
            self._hold_for(job_id, b)
            return
        # passes of at most REVIEW_PASS_TASKS (owner, 2026-10-23): each reserves
        # for its own tasks, the next only after the last one's stage went back
        while True:
            tasks = self._reviewable(job_id)
            if not tasks and self._all_merged(job_id):
                # nothing to review: every PR has merged. No stage, no session; the PR
                # poll moves the job to merged
                self.store.add_event(
                    "review_skipped", {"why": "every task's PR has merged"},
                    Provenance.HUMAN, job_id=job_id,
                )  # fmt: skip
                return
            batch = tasks[:REVIEW_PASS_TASKS]
            result = self._review_stage(job_id, job, batch, more=len(tasks) > len(batch))
            if result == "dispatch":
                # the pass is over and its stage released (2a round 6): only now
                # does the fix round (or the deps job's regeneration) build
                self.dispatch(job_id)
            if result != "next":
                return
            job = self.store.get_job(job_id)

    def page_stranded_drafts_sweep(self) -> list[str]:
        """The periodic form: a draft younger than 10 minutes is left to its first coord."""
        return self.page_stranded_drafts(grace_seconds=600)

    def _pr_merged(self, t: Any) -> bool:
        """The task's PR has merged: its row says so, or the poll saw it merged."""
        if t.state == "merged":
            return True
        row = self.store.q(
            "SELECT 1 FROM events WHERE task_id=? AND type='pr_merged' LIMIT 1", (t.id,)
        )
        return bool(row)

    def _reviewable(self, job_id: str) -> list[Any]:
        """The tasks a review pass may take: open PRs only. A task whose PR has merged is
        never reviewed (owner's rule); one the poll saw merged but whose row lags is
        brought up to date here."""
        out: list[Any] = []
        for t in self.store.list_tasks(job_id):
            if t.state != "pr_open":
                continue
            if self._pr_merged(t):
                self.store.update_task(t.id, state="merged")
                continue
            out.append(t)
        return out

    def _all_merged(self, job_id: str) -> bool:
        live = [t for t in self.store.list_tasks(job_id) if t.state not in DEAD_TASK]
        return bool(live) and all(t.state == "merged" for t in live)

    def _review_stage(self, job_id: str, job: Job, tasks: list[Any], *, more: bool) -> str:
        """One pass's review stage: one reservation for its tasks (owner,
        2026-10-23); each review and audit session draws its cap from it,
        and what's left goes back when the pass ends, before anything else
        builds. Returns what `_review_pass` decided."""
        bud = self.cfg.budgets
        amount = len(tasks) * (bud.review_session_usd + bud.audit_session_usd)
        if not tasks:
            # nothing to review (a restart after the reviews, or only fix
            # rounds owed): no reserve; straight on to ready or the fix rounds
            return self._review_pass(job_id, job, tasks, more=False)
        cap = min(bud.daily_usd, bud.monthly_usd)
        if amount > cap + 1e-9:
            # a guard for changed caps: at $0.40 + $0.80 a pass of 4 is $4.80
            self._block(job_id, f"review stage needs ${amount:.2f}; the daily cap is ${cap:.2f}")
            self._page(
                job_id,
                f"{job_id}: the review stage needs ${amount:.2f}; the daily cap is ${cap:.2f}",
                {"kind": "budget", "cap": "daily", "needs_usd": round(amount, 2),
                 "tasks": [t.id for t in tasks], "actions": ["replan", "abandon"],
                 "next": "re-plan into fewer tasks, or abandon; a retry can't fit"},
            )  # fmt: skip
            return "done"
        try:
            # the review reserve waits its turn like a worker's: first in, first out
            stage = self._reserve_queued(
                job_id, "review_stage", session_id=None, task_id=None, amount=amount,
                purpose="review_stage", expect=REVIEW_WAIT,
            )  # fmt: skip
        except QueueCancelled:
            return "done"  # the job moved on (abandoned, held) while it waited
        except BudgetExceeded as e:
            held = self._refused_budget(job_id, e, None)
            if held is not None:
                self._hold_for(job_id, held)
            else:
                self._budget_page(job_id, e, [t.id for t in tasks])
            return "done"
        try:
            self.store.add_event(
                "review_stage_reserved", {"amount_usd": amount, "tasks": [t.id for t in tasks]},
                Provenance.HUMAN, job_id=job_id,
            )  # fmt: skip
            with self._stage_lock:
                self._review_stage_ids[job_id] = stage
            return self._review_pass(job_id, job, tasks, more=more)
        finally:
            with self._stage_lock:
                self._review_stage_ids.pop(job_id, None)
            self.budget.settle(stage, 0.0)  # what no session drew goes back to the day

    def _review_pass(self, job_id: str, job: Job, tasks: list[Any], *, more: bool) -> str:
        """One review pass: "dispatch" when the job goes back to building
        (the caller dispatches it once this pass's stage is released),
        "next" when the pass was clean and tasks are still waiting for the
        next pass, "done" otherwise."""
        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
            outcomes = dict(zip(
                [t.id for t in tasks],
                [f.result() for f in [pool.submit(self._review_task, job, t) for t in tasks]],
                strict=True,
            ))  # fmt: skip
        if self.store.get_job(job_id).state is not JobState.IN_REVIEW:
            # the owner merged (or vigild moved) the job while this ran: its
            # results are moot; nothing more is written (2a round 3)
            self._drop_review(job_id, {"tasks": sorted(outcomes)})
            return "done"
        failed = {tid: why for tid, (kind, why) in outcomes.items() if kind == "failed"}
        b = self._paused(job)
        if failed and b is not None:
            self._hold_for(job_id, b)  # a cap tripped mid-review: wait on its breaker
            return "done"
        if failed:
            self._block(job_id, f"review failed: {sorted(failed)}")
            self._page(
                job_id,
                f"{job_id}: the review stage failed for {', '.join(sorted(failed))}",
                {"kind": "review_failed", "tasks": failed, "actions": ["retry", "abandon"]},
                resumable=True,
            )
            return "done"
        flagged = {tid: f for tid, (kind, f) in outcomes.items() if kind == "deps_findings"}
        if flagged:
            return "dispatch" if self._deps_review_failed(job_id, flagged) else "done"
        fixes = sorted(tid for tid, (kind, _) in outcomes.items() if kind == "fix")
        # a task a previous pass sent to a fix round, while another's review
        # failed, is still owed its round: it goes with this one (review B2)
        pending = [
            t.id for t in self.store.list_tasks(job_id) if t.state == "fix" and t.id not in fixes
        ]
        for tid in pending:
            fix = self._fix_round(self.store.get_task(tid))
            outcomes[tid] = ("fix", len(fix["findings"]) if fix else 0)
        fixes = sorted({*fixes, *pending})
        if fixes:
            limit = self.cfg.routing.escalate_review_rounds
            over = [
                tid
                for tid in fixes
                if int(self.store.get_task(tid).contract.get("review_rounds") or 0) > limit
            ]
            if over:
                self._block(job_id, f"review findings after escalation: {over}")
                self._page(
                    job_id,
                    f"{job_id}: findings remain after {limit} review rounds and an escalation",
                    {
                        "kind": "review_rounds",
                        "tasks": {tid: outcomes[tid][1] for tid in over},
                        "actions": ["replan", "abandon"],
                    },
                )
                return "done"
            moved = self._move_on(
                job_id, JobState.BUILDING, sorted(outcomes),
                evidence={"findings": {tid: outcomes[tid][1] for tid in fixes}},
            )  # fmt: skip
            return "dispatch" if moved else "done"
        if more:
            return "next"  # clean so far; the next pass reviews the rest
        left, legacy = self._unstripped(self.store.get_job(job_id))
        if left:
            self._block(job_id, f".vigil/ on the PR head of {sorted(left)}")
            body: dict[str, Any] = {"kind": "vigil_in_pr", "tasks": left}
            if legacy:  # retry can't help: the branch predates stripping
                body["actions"] = ["replan", "abandon"]
                body["next"] = "the branch predates artifact stripping and needs the owner's hand"
            else:
                body["actions"] = ["retry", "abandon"]
            self._page(
                job_id,
                f"{job_id}: .vigil/ is on the PR head of {', '.join(sorted(left))}",
                body,
                resumable=not legacy,
            )
            return "done"
        if not self._move_on(job_id, JobState.READY, sorted(outcomes)):
            return "done"
        self.breakers.success(Kind.REPO_FAILURES, f"repo:{job.repo}")
        self._ready(job_id)
        return "done"

    def _move_on(
        self,
        job_id: str,
        to: JobState,
        tasks: list[str],
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """The review's move out of `in_review` (to building for a fix round
        or a regeneration, or to ready). If the PR poll merged the job since
        the pass's re-check, the move is dropped quietly (`review_dropped`,
        2a rounds 6-7); a refusal for any other reason still raises."""
        if to is JobState.READY:
            evidence = {**(evidence or {}), "pr_heads": self._pr_heads(job_id)}
        try:
            transition(self.store, job_id, to, Actor.VIGILD, evidence=evidence)
        except TransitionError as e:
            if self.store.get_job(job_id).state is JobState.IN_REVIEW:
                gaps = plan_gaps(self.store, job_id) if to is JobState.READY else {}
                if gaps:  # a PR closed during the pass: replan or abandon (finding #4)
                    self._hold_without_pr(job_id, gaps, str(e))
                    return False
                raise
            self._drop_review(job_id, {"to": to.value, "tasks": tasks})
            return False
        return True

    def _drop_review(self, job_id: str, data: dict[str, Any]) -> None:
        """A review's results on a job that moved on are dropped
        (`review_dropped`). On an abandoned job nothing the review set going
        is left (2a rounds 8-9): a task it sent to a fix round, or queued
        again, is abandoned."""
        now = self.store.get_job(job_id).state
        if now is JobState.ABANDONED:
            for t in self.store.list_tasks(job_id):
                if t.state in ("fix", "queued", "running", "waiting"):
                    self.store.update_task(t.id, state="abandoned")
        self.store.add_event(
            "review_dropped", {"state": now.value, **data}, Provenance.HUMAN, job_id=job_id
        )

    def _deps_review_failed(self, job_id: str, flagged: dict[str, Any]) -> bool:
        """Owner ruling 3: a deps PR has no worker to fix it. A failed review
        closes the PR and regenerates the deps job once, on a new branch; a
        second failed review closes that PR too and pages the owner. The
        parent job stays blocked on this one throughout."""
        job = self.store.get_job(job_id)
        if job.state is not JobState.IN_REVIEW:
            # the job moved on (abandoned, merged): no PR is closed and no
            # task queued again (2a round 8)
            self._drop_review(job_id, {"tasks": sorted(flagged)})
            return False
        again: list[str] = []
        for tid in sorted(flagged):
            t = self.store.get_task(tid)
            url = self._task_pr(tid)
            if url:
                try:
                    self.publisher.close(job.repo, url)
                except PublishError as e:
                    self._page(
                        job_id, f"{job_id}: deps PR left open after its review failed",
                        {"kind": "deps_pr_left_open", "pr_url": url, "error": str(e)[:300],
                         "actions": ["dismiss"],
                         "next": "close it by hand; vigild won't build from it"},
                    )  # fmt: skip
                # the runner's own close, apart from the PR poll's `pr_closed`
                # events: only these count as regenerations (2a round 3)
                self.store.add_event(
                    "deps_review_closed", {"pr_url": url},
                    Provenance.HUMAN, job_id=job_id, task_id=tid,
                )  # fmt: skip
            closed = self.store.q(
                "SELECT COUNT(*) n FROM events WHERE task_id=? AND type='deps_review_closed'",
                (tid,),
            )[0]["n"]
            if closed >= 2:  # this was the regenerated PR: the owner decides
                self.store.update_task(tid, state="closed")
                continue
            contract = {**t.contract, "branch": f"vigil/{tid}-r{closed + 1}"}
            self.store.update_task(tid, contract=contract, state="queued")
            again.append(tid)
        if again and len(again) == len(flagged):
            # the caller dispatches, once the pass's stage is released
            return self._move_on(
                job_id, JobState.BUILDING, sorted(flagged),
                evidence={"findings": flagged, "deps_regenerated": again},
            )  # fmt: skip
        self._block(job_id, f"deps review findings, twice: {sorted(flagged)}")
        self._page(
            job_id,
            f"{job_id}: the review flagged the deps PR twice; both PRs are closed",
            {"kind": "deps_review", "findings": flagged, "actions": ["abandon"],
             "next": "abandon it (the job waiting on it stays blocked) and add the "
             "dependency by hand, or re-plan the job that needs it"},
        )  # fmt: skip
        return False

    def _ready(self, job_id: str) -> None:
        """Stage 8's queue: the job's PRs, each with its review and audit."""
        job = self.store.get_job(job_id)
        prs = []
        for t in self.store.list_tasks(job_id):
            if t.state in DEAD_TASK:
                continue
            rv = self.store.get_artifact(job_id, "review.json", t.id)
            au = self.store.get_artifact(job_id, "audit.json", t.id)
            review = json.loads(rv.content) if rv else {}
            audit = Audit.model_validate_json(au.content) if au else None
            prs.append(
                {
                    "task_id": t.id,
                    "pr_url": self._task_pr(t.id),
                    "head": self._task_head(t.id),
                    "shape": review.get("shape"),
                    "rung": review.get("rung"),
                    "review_rounds": review.get("round"),
                    "findings": len(review.get("findings") or []),
                    # what the owner can send back with "fix with findings"
                    "selectable": selectable(self.store, job_id, t.id),
                    "audited": audit is not None,
                    "match_rate": round(audit.match_rate(), 3) if audit else None,
                }
            )
        self.store.create_inbox_item(
            "ready", "ready_to_merge", job_id, f"Ready to merge: {job.title}",
            {"prs": prs, "actions": ["fix"]},
        )  # fmt: skip

    def _review_task(self, job: Any, t: Any) -> tuple[str, Any]:
        """One task's review (and audit, when sampled): ("clean" | "fix" |
        "failed", detail). Never raises: a vigild error is a failed review."""
        try:
            if self._pr_merged(self.store.get_task(t.id)):
                return "clean", "merged"  # its PR merged since the pass was chosen
            return self._review_one(job, t)
        except Exception as e:
            log.exception("review of %s failed", t.id)
            return "failed", f"vigild error: {type(e).__name__}: {e}"[:1000]

    def _review_one(self, job: Any, t: Any) -> tuple[str, Any]:
        head = self._task_head(t.id)
        if head is None:
            return "failed", "no published head"
        task = Task.model_validate(t.contract)
        shape = classify_shape(task, self.cfg)
        level = task.spec.verify.level
        tests = self.store.get_artifact(job.id, "tests.json", t.id)
        # the verifier's own record says whether the task changes how tests run
        rf = bool(tests and json.loads(tests.content).get("runner_files"))
        rung = self.ladder.rung(shape, runner_file=rf)
        reviewer, auditor = self.ladder.presets(shape, runner_file=rf)
        rnd = task.review_rounds + 1
        audit_required = self.ladder.should_audit(shape, level, runner_file=rf)
        base_sha = self.workspace.base_sha(t.id)
        diff = self.workspace.full_diff(job.repo, base_sha, head)
        spec_md = self.store.get_artifact(job.id, "spec.md")
        report = self.store.get_artifact(job.id, "worker_report.json", t.id)
        request = {
            "job_id": job.id,
            "task_id": t.id,
            "round": rnd,
            "head": head,
            "base_sha": base_sha,
            "contract": t.contract,
            "verifier": json.loads(tests.content) if tests else None,  # vigild's own record
            "spec_md": spec_md.content if spec_md else "",
        }
        untrusted = {
            "the request": job.request,
            "the worker's report": report.content if report else "(none)",
        }
        rules = self.workspace.read_blob(job.repo, base_sha, "CLAUDE.md") or ""  # the base's
        packet = assemble_review(rules, request, diff=diff, untrusted=untrusted)
        out, why = self._read_only_session(
            job, t, Role.REVIEWER, reviewer, head, base_sha, rnd, packet.text, ReviewOutput,
            self.cfg.budgets.review_session_usd, shape.value,
        )  # fmt: skip
        if out is None:
            return "failed", f"no valid review.json: {why}"
        assert isinstance(out, ReviewOutput)
        rv = Review(
            **out.model_dump(),
            task_id=t.id,
            job_id=job.id,
            head=head,
            shape=shape,
            rung=rung,
            round=rnd,
            audit_required=audit_required,
            reviewer=reviewer,
            provenance=Provenance.CLAUDE,
        )
        self.store.add_artifact(
            job.id, "review.json", rv.model_dump_json(), Provenance.CLAUDE, task_id=t.id
        )
        findings = [f.model_dump(mode="json") for f in rv.blocking()]
        if audit_required:
            numbered = json.dumps(
                {i: f.model_dump(mode="json") for i, f in enumerate(rv.findings)}, indent=1
            )
            apacket = assemble_audit(
                rules, request, diff=diff, review=numbered, untrusted=untrusted
            )
            aout, why = self._read_only_session(
                job, t, Role.AUDITOR, auditor, head, base_sha, rnd, apacket.text, AuditOutput,
                self.cfg.budgets.audit_session_usd, shape.value,
            )  # fmt: skip
            if aout is None:
                return "failed", f"no valid audit.json: {why}"
            assert isinstance(aout, AuditOutput)
            audit = Audit(
                **aout.model_dump(),
                task_id=t.id,
                job_id=job.id,
                head=head,
                shape=shape,
                rung=rung,
                auditor=auditor,
                provenance=Provenance.CLAUDE,
            )
            self.store.add_artifact(
                job.id, "audit.json", audit.model_dump_json(), Provenance.CLAUDE, task_id=t.id
            )
            change = self.ladder.record(audit, verify_level=level, runner_file=rf)
            if change is not None:
                self._ladder_page(change)
            findings += [
                f.model_dump(mode="json", exclude={"matched"})
                for f in audit.missed()
                if f.severity in BLOCKING
            ]
        if self._moved_on(job.id, t.id):
            return "dropped", "the job moved on during its review"
        if not findings:
            self.store.update_task(t.id, state="reviewed")
            return "clean", 0
        self.store.add_artifact(
            job.id,
            "findings.json",
            json.dumps({"round": rnd, "head": head, "findings": findings}),
            Provenance.CLAUDE,
            task_id=t.id,
        )
        if task.shape is not None and task.shape.value == "deps":
            # no model wrote it, so no fix round: the owner decides (B39)
            return "deps_findings", findings
        self._reroute_for_round(job.id, t.id, rnd)
        self.store.update_task(t.id, state="fix")
        return "fix", len(findings)

    def _moved_on(self, job_id: str, task_id: str) -> bool:
        """The job left review, or the task's PR merged or closed, while its
        review ran (2a round 3): its result must not overwrite that."""
        return (
            self.store.get_job(job_id).state is not JobState.IN_REVIEW
            or self.store.get_task(task_id).state != "pr_open"
        )

    def _reroute_for_round(self, job_id: str, task_id: str, rounds: int) -> None:
        """A review round with findings is counted on the contract and the task
        is routed again: the second round trips routing rule 5 (escalation)."""
        self._reroute(job_id, task_id, review_rounds=rounds)
        self.store.update_task(task_id, state="fix")

    def _reroute(self, job_id: str, task_id: str, **changes: Any) -> None:
        """Route a task again after its contract's counters move (a review
        round, another attempt). Crossing into rule 5 is an escalation."""
        t = self.store.get_task(task_id)
        contract = {**t.contract, **changes}
        r = route(Task.model_validate(contract), self.cfg)
        contract.update(tier=r.tier, tier_reason=r.tier_reason, backend=r.preset)
        self.store.update_task(task_id, contract=contract)
        self.store.route_task(
            task_id, tier=r.tier, tier_reason=r.tier_reason, backend_preset=r.preset
        )
        if r.tier_reason.startswith("rule5") and not str(t.tier_reason or "").startswith("rule5"):
            self.store.add_event(
                "escalation",
                {"task_id": task_id, **changes, "from": t.backend_preset, "to": r.preset,
                 "tier_reason": r.tier_reason},
                Provenance.HUMAN, job_id=job_id, task_id=task_id,
            )  # fmt: skip
            most = self.cfg.vigild.breakers.max_escalations_per_day
            if escalations_today(self.store) > most:
                self.breakers.trip(
                    Kind.ESCALATIONS, GLOBAL, {"today": escalations_today(self.store), "max": most}
                )

    def _note_over_cap(
        self,
        shape: str,
        role: Role,
        job_id: str | None,
        task_id: str | None,
        *,
        cap: float,
        cost: float,
    ) -> None:
        """A review or audit went past its cap (owner, 2026-10-23): it
        completed, its overage is in the day's spend, and it is an event. The
        second overage for one shape, counted since the caps last changed,
        pages once."""
        b = self.cfg.budgets
        caps = [b.review_session_usd, b.audit_session_usd]
        # the event, the count and the page in one transaction: sessions
        # finishing at once can't page twice, or not at all (2a round 6)
        with self.store.tx():
            self.store.add_event(
                "review_over_cap",
                {"shape": shape, "role": role.value, "cap_usd": cap,
                 "cost_usd": round(cost, 6), "overage_usd": round(cost - cap, 6), "caps": caps},
                Provenance.HUMAN, job_id=job_id, task_id=task_id,
            )  # fmt: skip
            n = sum(
                1
                for r in self.store.q("SELECT data FROM events WHERE type='review_over_cap'")
                if (d := json.loads(r["data"])).get("shape") == shape and d.get("caps") == caps
            )
            if n == 2:
                self._page(
                    None,
                    f"Review caps too low for shape {shape}: 2 overages; consider re-sizing",
                    {"kind": "review_caps", "shape": shape, "caps": caps, "actions": ["dismiss"],
                     "next": "raise review_session_usd or audit_session_usd in "
                     "configs/budgets.yaml"},
                )  # fmt: skip

    def _ladder_page(self, change: LadderChange) -> None:
        """You see every promotion and demotion (design, trust ladder)."""
        verb = "promoted" if change.to > change.frm else "demoted"
        self._page(
            None,
            f"review ladder: {change.shape.value} {verb} to rung {change.to}",
            {"kind": "ladder", "shape": change.shape.value, "from": change.frm, "to": change.to,
             "reason": change.reason, "sample_size": change.sample_size,
             "match_rate": change.match_rate, "actions": ["dismiss"]},
        )  # fmt: skip

    def _read_only_session(
        self,
        job: Any,
        t: Any,
        role: Role,
        preset: str,
        head: str,
        base_sha: str,
        rnd: int,
        packet: str,
        model: type[ReviewOutput] | type[AuditOutput],
        amount: float,
        shape: str = "",
    ) -> tuple[ReviewOutput | AuditOutput | None, str]:
        """A reviewer or auditor session on a fresh clone of the task's head:
        read-only tools, its answer as structured output (validated here; an
        empty or invalid answer fails the stage gate)."""
        branch = str(t.contract["branch"])
        parent = [s for s in self.store.list_sessions(job.id) if s.task_id == t.id]
        s = self.store.create_session(
            job.id, role, task_id=t.id, parent_session_id=parent[-1].id if parent else None,
            backend_preset=preset, branch=branch,
        )  # fmt: skip
        try:
            check_backend(self.cfg, job.repo, preset, private=live_private(job.repo))
            with self._stage_lock:
                stage = self._review_stage_ids.get(job.id)
            if stage is not None:  # its cap, drawn from the job's review stage
                rid = self.budget.draw(stage, session_id=s.id, amount=amount, purpose=role.value)
            else:
                rid = self.budget.reserve(
                    session_id=s.id, task_id=None, amount=amount, purpose=role.value
                )
        except (RepoPolicyError, BudgetExceeded) as e:
            self.store.update_session(s.id, status="refused")
            if isinstance(e, BudgetExceeded):
                self._refused_budget(job.id, e, None)  # a cap metered spend reached trips
            return None, str(e)
        clone_id = f"{t.id}-{role.value}{rnd}"
        try:
            clone = self.workspace.clone(
                job.repo, clone_id, branch=branch, base=job.base, start=head, record_as=base_sha
            )
            self.workspace.write_task(
                clone, {**t.contract, "base_sha": head, "head": head, "role": role.value}
            )
            session_image = self._session_image(job.repo, base_sha)
        except Exception:
            self.budget.settle(rid)
            self.store.update_session(s.id, status="failed")
            raise
        try:
            spec, attrs = self._review_spec(
                job, t, s, role, preset, clone, rnd, packet, model, session_image
            )
        except Exception:
            self.budget.settle(rid)  # the draw goes back (2a round 8)
            self.store.update_session(s.id, status="failed")
            raise
        out = self._run_session(s, spec, rid, attrs, cap=amount)
        return self._review_answer(s, job, t, role, shape, amount, out, model)

    def _review_spec(
        self,
        job: Any,
        t: Any,
        s: SessionRow,
        role: Role,
        preset: str,
        clone: Path,
        rnd: int,
        packet: str,
        model: type[ReviewOutput] | type[AuditOutput],
        session_image: str | None,
    ) -> tuple[SessionSpec, dict[str, Any]]:
        spec = SessionSpec(
            session_id=s.id,
            role=role,
            job_id=job.id,
            task_id=t.id,
            repo=job.repo,
            preset=preset,
            model=self.cfg.preset(preset).model or "",
            clone=clone,
            session_dir=self._session_dir(s.id),
            max_turns=REVIEW_TURNS,
            max_minutes=REVIEW_MINUTES,
            # the cap is checked call by call at the gateway, and a review may
            # go past it (owner, 2026-10-23): only the day stops the CLI
            max_budget_usd=self.cfg.budgets.daily_usd,
            mounts=self.workspace.overlays(clone),
            packet=packet,
            image=session_image,
            output_schema=model.model_json_schema(),
            max_output_tokens=REVIEW_OUTPUT_TOKENS,
        )
        attrs = {
            "vigil.job_id": job.id,
            "vigil.task_id": t.id,
            "vigil.role": role.value,
            "vigil.repo": job.repo,
            "vigil.model": spec.model,
            "vigil.attempt": rnd,
        }
        return spec, attrs

    def _review_answer(
        self,
        s: SessionRow,
        job: Any,
        t: Any,
        role: Role,
        shape: str,
        amount: float,
        out: ExecOutcome,
        model: type[ReviewOutput] | type[AuditOutput],
    ) -> tuple[ReviewOutput | AuditOutput | None, str]:
        cost = session_cost(self.store, s.id)
        if cost > amount + 1e-9:
            self._note_over_cap(shape, role, job.id, t.id, cap=amount, cost=cost)
        if out.status != "done":
            self.store.update_session(s.id, status="failed")
            return None, f"{role.value} session {out.status}: {(out.error or '')[:500]}"
        try:
            parsed = model.model_validate(out.result.get("structured_output"))
        except ValidationError as e:
            self.store.update_session(s.id, status="failed")
            return None, str(e)[:1000]
        self.store.update_session(s.id, status="done")
        return parsed, ""


# --- wiring for the real daemon ------------------------------------------------------------


class GitHubPublisher:
    """Mints a one-hour, one-repo token for each publish and revokes it after."""

    def __init__(self, app: Any, http: Any, workspace: Workspace) -> None:
        self.app = app
        self.http = http
        self.workspace = workspace

    def publish(
        self,
        *,
        repo: str,
        head: str,
        base_sha: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        scope: list[str],
        existing: str | None = None,
    ) -> str:
        from vigild.github.app import WORKER_PERMISSIONS
        from vigild.github.publish import RestPulls, publish

        with self.app.lease(repo, WORKER_PERMISSIONS) as tok:
            return publish(
                self.workspace,
                repo=repo,
                head=head,
                base_sha=base_sha,
                branch=branch,
                base=base,
                title=title,
                body=body,
                pulls=RestPulls(self.http, tok.token),
                scope=scope,
                token=tok.token,
                existing=existing,
            )

    def close(self, repo: str, url: str) -> None:
        """Close one of vigild's PRs, with a one-repo `pull_requests: write`
        token, revoked after (a deps PR whose review failed, owner ruling 3)."""
        from vigild.github.publish import RestPulls

        prefix = f"https://github.com/{repo}/pull/"
        if not url.startswith(prefix) or not url[len(prefix) :].isdigit():
            raise PublishError(f"{url} isn't a PR of {repo}")
        with self.app.lease(repo, {"pull_requests": "write"}) as tok:
            RestPulls(self.http, tok.token).close(repo, int(url[len(prefix) :]))


class GitHubPRs:
    """vigild's PR poll (item 5): a PR's state, read with a one-repo,
    `pull_requests: read` token, revoked after."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def state(self, repo: str, url: str) -> str:
        from vigild.github.publish import RestPulls

        prefix = f"https://github.com/{repo}/pull/"
        if not url.startswith(prefix) or not url[len(prefix) :].isdigit():
            raise PublishError(f"{url} isn't a PR of {repo}")
        with self.app.lease(repo, {"pull_requests": "read"}) as tok:
            return RestPulls(self.app.http, tok.token).state(repo, int(url[len(prefix) :]))


class NoPublisher:
    def publish(
        self,
        *,
        repo: str,
        head: str,
        base_sha: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        scope: list[str],
        existing: str | None = None,
    ) -> str:
        raise PublishError("the GitHub App is not configured (GITHUB_APP_ID, GITHUB_APP_KEY_PATH)")

    def close(self, repo: str, url: str) -> None:
        raise PublishError("the GitHub App is not configured (GITHUB_APP_ID, GITHUB_APP_KEY_PATH)")
