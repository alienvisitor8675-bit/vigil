"""The job runner: drives a job through stages 1-3 of the protocol.

- on_job_created (a request with no spec): the coord drafts spec.md and
  plan.md; vigild runs the spec gate; up to three drafts, each its own session,
  then it pages you (resumable: resuming drafts again).
- on_approved (only from the inbox): planning turns plan.md's contracts into
  routed tasks; building runs them in dependency waves, each wave's workers in
  parallel, each in its own container, clone and branch.
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

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config
from vigild.contract.models import (
    Limits,
    Spec,
    Task,
    TestOutcome,
    Verify,
    WorkerContract,
    WorkerReport,
)
from vigild.db import SessionRoleError, Store, iso
from vigild.devimage import DevImages, SessionImages, StaleDevImage, missing_tools
from vigild.executors.base import ExecOutcome, Executor, SessionSpec
from vigild.gateway.service import TOKEN_TTL_MARGIN_MIN, Gateway
from vigild.github.publish import PublishError, artifact_paths, vigild_paths
from vigild.hidden import ARTIFACT as HIDDEN_ARTIFACT
from vigild.hidden import AUTHOR_ATTEMPTS, HiddenError, Place, entry_suite, load_suite, place_for
from vigild.intake.spec_gate import test_cmd_problem
from vigild.jobs.acceptance import NO_HIDDEN, check_worker, escalates
from vigild.jobs.artifacts import parse_plan, render
from vigild.jobs.deps import infer_deps, is_lockfile
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import JobState, Role, SessionRow
from vigild.metering.calls import last_call_truncated, session_cost
from vigild.packets.assemble import assemble_coord, assemble_hidden_test_author, assemble_worker
from vigild.packets.fence import fence
from vigild.provenance import Provenance
from vigild.redact import redact
from vigild.repos import RepoPolicyError, check_backend, check_hidden_author
from vigild.router.rules import RoutingError, route
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier, runner_like, runner_overlap
from vigild.workspace import Workspace, WorkspaceError

log = logging.getLogger("vigild.runner")
COORD_DRAFTS = 3
COORD_MINUTES = 30
WORKER_MINUTES = 40
LIVE_SESSION = ("created", "running")
DEAD_TASK = ("superseded", "abandoned")  # out of the job: re-planned away or given up
QUEUE_POLL_SECONDS = 15.0  # how often a queued session looks for room in the day
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
    ) -> str: ...


class Pager(Protocol):
    def page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> Any: ...
    def file_for_approval(self, job_id: str) -> list[str]: ...
    def flagged_tasks(self, job_id: str) -> dict[str, list[str]]: ...
    def file_item(self, job_id: str) -> None: ...
    def open_spec_item(self, job_id: str) -> Any: ...


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

    # --- queueing for the day's headroom ---------------------------------------------------

    def _queueable(self, e: BudgetExceeded, amount: float) -> bool:
        """Only a full day waits: its money frees up (a reservation settles, or
        the UTC day rolls over). A task's cap, the month, or an amount larger
        than the whole day never will."""
        return e.cap == "daily" and amount <= self.cfg.budgets.daily_usd

    def _queue_event(
        self, kind: str, job_id: str, what: str, task_id: str | None, **data: Any
    ) -> None:
        self.store.add_event(
            kind, {"for": what, **data}, Provenance.HUMAN, job_id=job_id, task_id=task_id
        )

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
    ) -> int:
        """Reserve, waiting while the day is full: the one waiting mechanism.
        Emits `queued` once and `dequeued` when it gets the money; not a
        failure, no page, no attempt used. Any other refusal raises."""
        while True:
            try:
                rid = self.budget.reserve(
                    session_id=session_id, task_id=task_id, amount=amount, purpose=purpose
                )
            except BudgetExceeded as e:
                if not self._queueable(e, amount):
                    raise
                if not queued:
                    queued = True
                    self._queue_event(
                        "queued", job_id, what, task_id, amount_usd=amount, detail=e.detail
                    )
                self.sleep(QUEUE_POLL_SECONDS)
                continue
            if queued:
                self._queue_event("dequeued", job_id, what, task_id, amount_usd=amount)
            return rid

    # --- triggers ---------------------------------------------------------------------

    def running(self) -> list[str]:
        with self._running_lock:
            return sorted(self._running)

    def _spawn(self, fn: Callable[[str], None], job_id: str) -> None:
        # round 3, F7: one run per job at a time, whatever starts it
        with self._running_lock:
            if job_id in self._running:
                log.info("%s is already running; not starting it twice", job_id)
                return
            self._running.add(job_id)

        def safe(j: str) -> None:
            try:
                fn(j)
            except Exception as e:  # never die silently: page, and let the owner resume
                log.exception("runner failed on %s", j)
                self._block(j, f"vigild error: {type(e).__name__}")
                self._page(
                    j, f"vigild error on {j}", {"error": f"{type(e).__name__}: {e}"}, resumable=True
                )
            finally:
                with self._running_lock:
                    self._running.discard(j)

        if self.background:
            threading.Thread(target=safe, args=(job_id,), daemon=True).start()
        else:
            safe(job_id)

    def on_job_created(self, job_id: str, needs_spec: bool) -> None:
        if needs_spec:
            self._spawn(self.run_coord, job_id)
        elif self.jobs is not None and self.jobs.flagged_tasks(job_id):
            # a submitted task that names a runner file: its hidden tests are
            # written in the background, and then the spec is filed
            self._spawn(self._finish_spec, job_id)

    def on_approved(self, job_id: str) -> None:
        self._spawn(self.build, job_id)

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

    def on_resume(self, job_id: str, *, replan: bool = False) -> None:
        state = self.store.get_job(job_id).state
        if state is JobState.SPEC_PENDING:
            # a retry never sends a finished draft back to the coord (hotfix 5);
            # only a replan asks it for a new one
            if not replan and self._author_only(job_id):
                self._spawn(self._finish_spec, job_id)
            else:
                self._spawn(self.run_coord, job_id)
        elif state is JobState.APPROVED:
            self._spawn(self.build, job_id)
        elif state is JobState.PLANNING:
            self._spawn(self._plan_and_dispatch, job_id)
        elif state is JobState.BUILDING:
            self._spawn(self.dispatch, job_id)

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
        for s in self.store.list_sessions():
            if s.status in LIVE_SESSION:
                self.store.update_session(s.id, status="failed", ended_at=iso())
                out["sessions"] += 1
                if s.role is Role.COORD and s.job_id:
                    failed_coord.add(s.job_id)
                elif s.role is Role.HIDDEN_TEST_AUTHOR and s.job_id:
                    failed_author.add(s.job_id)
        for rid in self.budget.open_reservation_ids():
            self.budget.settle(rid)
            out["reservations"] += 1
        for job in self.store.list_jobs():
            if job.state in (JobState.PLANNING, JobState.BUILDING):
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
            elif job.state is JobState.APPROVED:
                self.deferred.append(job.id)
                out["rebuilt"] += 1
        self.store.add_event("reconcile", out, Provenance.HUMAN)
        return out

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
            self.on_approved(job_id)
        return len(started)

    # --- helpers ------------------------------------------------------------------------

    def _page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> None:
        assert self.jobs is not None
        self.jobs.page(job_id, title, body, resumable=resumable)

    def _block(self, job_id: str, reason: str, *, state: JobState = JobState.BLOCKED) -> None:
        try:
            job = self.store.get_job(job_id)
            if job.state in (JobState.BLOCKED, JobState.ESCALATED, JobState.DONE):
                return
            transition(self.store, job_id, state, Actor.VIGILD, evidence={"reason": reason})
        except TransitionError, KeyError:
            log.warning("could not block %s", job_id)

    def _event_sink(self, s: SessionRow) -> Callable[[dict[str, Any]], None]:
        def sink(e: dict[str, Any]) -> None:
            self.store.add_event(
                str(e.get("type", "state")),
                redact(e.get("data") or {}),
                Provenance.CLAUDE,
                job_id=s.job_id,
                session_id=s.id,
                task_id=s.task_id,
            )

        return sink

    def _run_session(
        self, s: SessionRow, spec: SessionSpec, reservation: int, attrs: dict[str, Any]
    ) -> ExecOutcome:
        """Token, telemetry, run, revoke, settle. The reservation is already made."""
        if s.role is not spec.role:
            # role separation, enforced at dispatch: a session row never runs as
            # another role, so an author's session is never a worker or a reviewer
            raise SessionRoleError(f"session {s.id} is a {s.role.value}, not a {spec.role.value}")
        token = self.gateway.issue_token(
            s.id, spec.repo, spec.preset, ttl_minutes=spec.max_minutes + TOKEN_TTL_MARGIN_MIN
        )
        self.store.update_session(
            s.id, status="running", started_at=iso(), clone_path=str(spec.clone)
        )
        out: ExecOutcome | None = None
        try:
            with self.telemetry.session(s.id, attrs) as tr:
                spec.traceparent = tr.traceparent
                self.store.update_session(s.id, detail={"trace_id": tr.trace_id})
                with self.telemetry.span(tr, "vigil.spawn", {"vigil.image": spec.image or ""}):
                    out = self.executor.run(spec, token, self._event_sink(s))
                tr.set("vigil.status", out.status)
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
                    "SELECT 1 FROM events WHERE session_id=? AND type='metering_failed'", (s.id,)
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
            self._settle_fail_closed(s, spec, reservation, out)
            self.store.update_session(s.id, ended_at=iso())

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
        rows = self.store.q("SELECT amount_usd FROM reservations WHERE id=?", (reservation,))
        reserved = float(rows[0]["amount_usd"]) if rows else 0.0
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

    def _session_dir(self, session_id: str) -> Path:
        return self.cfg.vigild.data_path / "sessions" / session_id

    # --- stage 1: the coord drafts spec.md and plan.md ------------------------------------

    def run_coord(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        preset = self.cfg.routing.coord
        try:
            # the repo's dev image must be fresh before any work starts on it;
            # a new lockfile builds one here (owner direction, round 2)
            session_image = self._session_image(
                job.repo, self.workspace.head_sha(job.repo, job.base)
            )
            check_backend(self.cfg, job.repo, preset)
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
            except BudgetExceeded as e:
                self.store.update_session(coord.id, status="refused")
                self._page(
                    job_id,
                    f"{job_id}: {e.cap} cap reached; coord not started",
                    {"error": str(e)},
                    resumable=True,
                )
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
            **{f"previous_{k}": v for k, v in previous.items()},
        }
        untrusted: dict[str, str] = {}
        if replan:
            # vigild's summary goes in the contract; the failure details carry
            # worker and test output, so they go in the untrusted section
            request["replan"] = {
                tid: {"title": t.get("title"), "state": t.get("state")} for tid, t in replan.items()
            }
            untrusted["previous build failures"] = json.dumps(replan, indent=1)[:12000]
        self.workspace.write_task(clone, request)
        packet = assemble_coord(clone, request, untrusted=untrusted, gate_errors=gate_errors)
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
            if name == "spec.md" and human:
                # a coord never rewrites a human-written spec: a replan re-plans only
                assert kept is not None
                drafts["spec"] = kept.content
                continue
            text = self.workspace.read_blob(job.repo, head, f".vigil/{job_id}/{name}") or ""
            drafts[name.removesuffix(".md")] = text
            self.store.add_artifact(
                job_id, name, render(name, text, Provenance.CLAUDE), Provenance.CLAUDE
            )
        assert self.jobs is not None
        return self.jobs.file_for_approval(job_id), drafts

    # --- stage 1b: hidden tests for a task whose scope names a runner file --------------------

    def _finish_spec(self, job_id: str) -> None:
        """The spec passed the gate. For each runner-file task a fresh author
        session writes its hidden tests (the job stays `spec_pending` and no
        inbox item exists meanwhile); then the spec is filed. An author that
        fails twice leaves the task marked "hidden tests missing"."""
        assert self.jobs is not None
        flagged = self.jobs.flagged_tasks(job_id)
        if not flagged or self.jobs.open_spec_item(job_id) is not None:
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
            check_backend(self.cfg, job.repo, preset)
            check_hidden_author(self.cfg, job.repo, preset)
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
        art = self.store.get_artifact(job_id, "plan.md")
        assert art is not None
        workers = parse_plan(art.content).workers
        planned = {w.task_id for w in workers}
        for old in self.store.list_tasks(job_id):
            if old.id not in planned and old.state not in ("pr_open", *DEAD_TASK):
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
                self._page(
                    job_id, f"{job_id}: {w.task_id} can't run", {"error": str(e)}, resumable=True
                )
                self._block(job_id, f"routing: {e}")
                return
            contract = task.model_copy(
                update={"tier": r.tier, "tier_reason": r.tier_reason, "backend": r.preset}
            ).model_dump(mode="json")
            try:
                existing = self.store.get_task(w.task_id)
                if existing.job_id != job_id:
                    raise RuntimeError(f"task {w.task_id} belongs to {existing.job_id}")
                if existing.state == "pr_open":
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
        """Run every live task not yet published, in dependency waves. After a
        wave with a failure, the job blocks and the owner chooses (N5)."""
        tasks = {t.id: t for t in self.store.list_tasks(job_id) if t.state not in DEAD_TASK}
        done = {t for t, row in tasks.items() if row.state == "pr_open"}
        # an escalated task is held out of a retry: it would build the same thing
        # (a new plan and new hidden tests are what help), but the job's other
        # failed tasks still retry (hotfix 5)
        escalated = [t for t, row in tasks.items() if row.state == "escalated"]
        pending = [t for t in tasks if t not in done and t not in escalated]
        while pending:
            wave = [t for t in pending if set(tasks[t].contract.get("depends_on", [])) <= done]
            if not wave and escalated:
                self._hold_failed(job_id, escalated)  # what's left waits behind them
                return
            if not wave:
                # Nothing can start: a dependency is missing from the plan (item 17).
                stuck = {t: self.store.get_task(t).state for t in pending}
                self._page(
                    job_id,
                    f"{job_id}: work is stuck behind unfinished dependencies",
                    {"pending": stuck, "actions": ["replan", "abandon"]},
                )
                self._block(job_id, f"stuck: {sorted(stuck)}")
                return
            pending = [t for t in pending if t not in wave]
            try:
                reservations = self._reserve_wave(job_id, wave)
            except Refused:
                return
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures = [
                    pool.submit(self._run_worker, job_id, t, *reservations[t])
                    if t in reservations
                    else pool.submit(self._run_queued_worker, job_id, t)
                    for t in wave
                ]
                results = [f.result() for f in futures]
            done |= {t for t, ok in zip(wave, results, strict=True) if ok}
            failed = [t for t, ok in zip(wave, results, strict=True) if not ok]
            if failed:
                self._hold_failed(job_id, failed + [t for t in escalated if t not in failed])
                return
        if escalated:
            self._hold_failed(job_id, escalated)
            return
        live = [t for t in self.store.list_tasks(job_id) if t.state not in DEAD_TASK]
        if all(t.state == "pr_open" for t in live):
            transition(self.store, job_id, JobState.IN_REVIEW, Actor.VIGILD)

    def remaining(self, task_id: str) -> tuple[float, float, float]:
        """(cap, spent, left) for a task: a retry gets what's left, never a fresh cap."""
        cap = float(self.store.get_task(task_id).contract["limits"]["max_cost_usd"])
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
        left), re-plan, or abandon."""
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
            # retry once the deps PR has merged: the base moves, so the dev image
            # key changes and vigild builds the new image first
            actions = (["retry"] if can_retry else []) + ["abandon"]
            self._block(job_id, "needs_deps")
            self._page(
                job_id,
                f"{job_id}: needs dependencies: "
                + ", ".join(
                    f"{d['package']}@{d['version']}"
                    for t in tasks.values()
                    for d in t.get("needs_deps", [])
                )[:250],
                {
                    "kind": "needs_deps",
                    "tasks": tasks,
                    "actions": actions,
                    "next": "a deps job changes only the manifest and lockfile, rebuilds the dev "
                    "image and lands as its own PR (phase 2 builds it; until then, by hand). "
                    "Retry this job after that PR merges.",
                },
                resumable=can_retry,
            )
            return
        escalated = [t for t in task_ids if self.store.get_task(t).state == "escalated"]
        actions = (["retry"] if can_retry else []) + ["replan", "abandon"]
        self._block(
            job_id,
            f"workers failed: {sorted(task_ids)}",
            state=JobState.ESCALATED if escalated else JobState.BLOCKED,
        )
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
            )
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
            _, _, amount = self.remaining(tid)
            try:
                if amount < MIN_RETRY_USD:
                    raise BudgetExceeded(
                        "per_task", f"task {tid} has ${amount:.2f} of its cap left"
                    )
                held[tid] = (
                    self.budget.reserve(
                        session_id=None, task_id=tid, amount=amount, purpose="worker"
                    ),
                    amount,
                )
            except BudgetExceeded as e:
                if self._queueable(e, amount):
                    self._queue_event(
                        "queued", job_id, "worker", tid, amount_usd=amount, detail=e.detail
                    )
                    continue
                for rid, _amt in held.values():
                    self.budget.settle(rid, 0.0)
                for t2 in task_ids:
                    self.store.update_task(t2, state="waiting")
                per_task = e.cap == "per_task"
                self._page(
                    job_id,
                    f"{job_id}: {e.cap} cap reached; dispatch halted",
                    {
                        "cap": e.cap,
                        "detail": e.detail,
                        "tasks": task_ids,
                        "actions": ([] if per_task else ["retry"]) + ["replan", "abandon"],
                    },
                    resumable=not per_task,
                )
                self._block(job_id, f"budget: {e}")
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
        self, repo: str, base_sha: str, head: str, overlap: list[str], tests: dict[str, Any] | None
    ) -> str:
        """The PR body's plain statement for a runner-file task: the class is
        durable (`runner_file_change`), the owner looks, hidden tests decided."""
        if not overlap:
            return ""
        changed = {path for _, path in self.workspace.changed(repo, base_sha, head)}
        moved = [p for p in overlap if p in changed]
        files = ", ".join(f"`{p}`" for p in moved or overlap)
        what = "runner file changed" if moved else "runner file in scope, unchanged"
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
        preset = t.backend_preset or self.cfg.routing.fallback
        branch = contract["branch"]
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
        if paths:
            session_base = self.workspace.commit_before_session(
                clone, paths, f"vigil: {job_id} spec and plan"
            )
        self.workspace.write_task(clone, {**contract, "base_sha": base_sha})
        approval = self.store.get_artifact(job_id, "approval.json")
        edits = json.loads(approval.content).get("edits") if approval else None
        packet = assemble_worker(clone, contract, untrusted={}, approval_edits=edits)
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
            max_turns=60,
            max_minutes=WORKER_MINUTES,
            max_budget_usd=amount,  # what's left of the task's cap (N5)
            mounts=self.workspace.overlays(clone),
            packet=packet.text,
            image=session_image,  # the verifier runs the plain dev image, `image`
            max_output_tokens=self.cfg.budgets.session_max_output_tokens,
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
        out = self._run_session(s, spec, reservation, attrs)
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
                self.store.update_session(s.id, status="waiting")
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
        hidden_entry = self._hidden_entry(job_id, task_id) if overlap else None
        if overlap and self.verifier.host_key is None:
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
            if overlap and "hidden" not in tests:
                reason = (hidden_entry or {}).get("reason") or "none were written"
                tests["hidden"] = {"present": False, "reason": reason}
            self.store.add_artifact(
                job_id, "tests.json", json.dumps(redact(tests)), Provenance.HUMAN, task_id=task_id
            )
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
        runner_note = self._runner_note(job.repo, base_sha, head, overlap, tests)
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
                title=f"[vigil {job_id}] {contract['spec']['goal']}",
                body=body,
                scope=[
                    *contract["spec"]["files_in_scope"],
                    *artifact_paths(job_id, task_id),
                    *vigild_paths(job_id),
                ],
            )
        except PublishError as e:
            return self._fail(job_id, s, task_id, f"{task_id}: publish refused", {"error": str(e)})
        self.store.update_session(s.id, status="pr_open", pr_url=url)
        self.store.update_task(task_id, state="pr_open")
        self.store.add_event(
            "pr_open",
            {"pr_url": url, "branch": branch, "head": head, "runner_file_change": overlap},
            Provenance.HUMAN,
            job_id=job_id,
            session_id=s.id,
            task_id=task_id,
        )
        return True


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
            )


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
    ) -> str:
        raise PublishError("the GitHub App is not configured (GITHUB_APP_ID, GITHUB_APP_KEY_PATH)")
