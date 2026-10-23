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

import functools
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

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
from vigild.db import Store, iso
from vigild.devimage import DevImages, SessionImages, StaleDevImage, missing_tools
from vigild.executors.base import ExecOutcome, Executor, Resume, SessionSpec
from vigild.gateway.service import TOKEN_TTL_MARGIN_MIN, Gateway
from vigild.github.publish import PublishError
from vigild.intake.github import issue_text
from vigild.jobs.acceptance import check_worker, runner_config_changes
from vigild.jobs.artifacts import parse_plan, render
from vigild.jobs.deps import infer_deps, is_lockfile
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import JobState, Role, SessionRow
from vigild.metering.calls import session_cost
from vigild.packets.assemble import (
    assemble_audit,
    assemble_coord,
    assemble_review,
    assemble_worker,
)
from vigild.packets.fence import fence
from vigild.provenance import Provenance
from vigild.redact import redact
from vigild.repos import RepoPolicyError, check_backend
from vigild.review.ladder import Ladder, LadderChange, classify_shape
from vigild.router.rules import RoutingError, route
from vigild.scheduler import wip
from vigild.scheduler.graph import Slots, run_graph
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier
from vigild.workspace import Workspace, WorkspaceError

log = logging.getLogger("vigild.runner")
COORD_DRAFTS = 3
COORD_MINUTES = 30
WORKER_MINUTES = 40
REVIEW_MINUTES = 20
REVIEW_TURNS = 30
LIVE_SESSION = ("created", "running")
DEAD_TASK = ("superseded", "abandoned")  # out of the job: re-planned away or given up
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


class Pager(Protocol):
    def page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> Any: ...
    def file_for_approval(self, job_id: str) -> list[str]: ...


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
        self.closers: list[Callable[[], None]] = []  # e.g. the docker client (item 0)
        self.ladder = Ladder(store, cfg)
        self._sdk_seen: set[tuple[str, str]] = set()
        self.slots = Slots(cfg.vigild.scheduler.sessions)  # across every job (item 2)

    def close(self) -> None:
        for close in self.closers:
            close()
        self.closers = []

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

    def on_approved(self, job_id: str) -> None:
        self._spawn(self.build, job_id)

    def on_resume(self, job_id: str) -> None:
        state = self.store.get_job(job_id).state
        if state is JobState.SPEC_PENDING:
            self._spawn(self.run_coord, job_id)
        elif state is JobState.APPROVED:
            self._spawn(self.build, job_id)
        elif state is JobState.PLANNING:
            self._spawn(self._plan_and_dispatch, job_id)
        elif state is JobState.BUILDING:
            self._spawn(self.dispatch, job_id)
        elif state is JobState.IN_REVIEW:
            self._spawn(self.review, job_id)

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
        live = [s for s in self.store.list_sessions() if s.status in LIVE_SESSION]
        for rid in self.budget.open_reservation_ids():
            self.budget.settle(rid)  # first: what's left of each task's cap is known
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
            elif job.state is JobState.SPEC_PENDING and job.id in failed_coord:
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
            if self.store.get_job(job_id).state is JobState.BUILDING:
                self.on_resume(job_id)  # workers a restart cut off (item 3)
            else:
                self.on_approved(job_id)
        return len(started)

    # --- helpers ------------------------------------------------------------------------

    def _page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> None:
        assert self.jobs is not None
        self.jobs.page(job_id, title, body, resumable=resumable)

    def _block(self, job_id: str, reason: str) -> None:
        try:
            job = self.store.get_job(job_id)
            if job.state in (JobState.BLOCKED, JobState.ESCALATED, JobState.DONE):
                return
            transition(
                self.store, job_id, JobState.BLOCKED, Actor.VIGILD, evidence={"reason": reason}
            )
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
        self, s: SessionRow, spec: SessionSpec, reservation: int, attrs: dict[str, Any]
    ) -> ExecOutcome:
        """A session slot, then token, telemetry, run, revoke, settle. The
        reservation is already made; the token is issued once the slot is
        taken, so waiting for one doesn't eat its life."""
        with self.slots.session():
            token = self.gateway.issue_token(
                s.id, spec.repo, spec.preset, ttl_minutes=spec.max_minutes + TOKEN_TTL_MARGIN_MIN
            )
            self.store.update_session(
                s.id, status="running", started_at=iso(), clone_path=str(spec.clone)
            )
            try:
                with self.telemetry.session(s.id, attrs) as tr:
                    spec.traceparent = tr.traceparent
                    self.store.update_session(s.id, detail={"trace_id": tr.trace_id})
                    with self.telemetry.span(tr, "vigil.spawn", {"vigil.image": spec.image or ""}):
                        out = self.executor.run(spec, token, self._event_sink(s))
                    tr.set("vigil.status", out.status)
                return out
            finally:
                self.gateway.revoke(s.id)
                self.budget.settle(reservation)
                self.store.update_session(s.id, ended_at=iso())

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
                rid = self.budget.reserve(
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
        packet = assemble_coord(clone, request, untrusted=untrusted, gate_errors=gate_errors)
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
        for name in ("spec.md", "plan.md"):
            text = self.workspace.read_blob(job.repo, head, f".vigil/{job_id}/{name}") or ""
            drafts[name.removesuffix(".md")] = text
            self.store.add_artifact(
                job_id, name, render(name, text, Provenance.CLAUDE), Provenance.CLAUDE
            )
        assert self.jobs is not None
        return self.jobs.file_for_approval(job_id), drafts

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
        deps = {t: list(row.contract.get("depends_on", [])) for t, row in tasks.items()}
        limit = self.cfg.budgets.wip_per_repo
        wip_held: set[str] = set()

        def launch(batch: list[str]) -> dict[str, Callable[[], bool]] | None:
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
            try:
                held = self._reserve_wave(job_id, admitted)
            except Refused:
                return None
            for tid in admitted:
                if self.store.get_task(tid).state == "failed":
                    # another attempt; the third is routed by rule 5 (escalated)
                    attempt = int(self.store.get_task(tid).contract.get("attempt") or 1)
                    self._reroute(job_id, tid, attempt=attempt + 1)
                self.store.update_task(tid, state="running")  # takes its place in the stack
            return {
                tid: functools.partial(self._run_worker, job_id, tid, *held[tid])
                for tid in admitted
            }

        result = run_graph(deps, done, launch)
        if result.failed:
            self._hold_failed(job_id, result.failed)
            return
        if result.halted:
            return  # a budget refusal blocked the job and paged
        waiting = [t for t in result.waiting if t in wip_held]
        if waiting:
            for tid in waiting:
                self.store.update_task(tid, state="waiting")
            self._wip_hold(job_id, job.repo, waiting)
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
        if all(t.state in PUBLISHED for t in live):
            transition(self.store, job_id, JobState.IN_REVIEW, Actor.VIGILD)
            self.review(job_id)

    def _wip_hold(self, job_id: str, repo: str, waiting: list[str]) -> None:
        """The repo is at its WIP limit: the job waits, and the inbox shows the
        stack (design "Intake", rule 2). Merge or close one, then retry."""
        limit = self.cfg.budgets.wip_per_repo
        self._block(job_id, f"WIP limit: {limit} open fleet PRs in {repo}")
        self._page(
            job_id,
            f"{repo}: {limit} open fleet PRs (the WIP limit); {len(waiting)} task(s) wait",
            {
                "kind": "wip",
                "repo": repo,
                "stack": wip.stack(self.store, repo),
                "waiting": waiting,
                "actions": ["retry", "abandon"],
            },
            resumable=True,
        )

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
        can_retry = True
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
            can_retry = can_retry and left >= MIN_RETRY_USD
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
        actions = (["retry"] if can_retry else []) + ["replan", "abandon"]
        self._block(job_id, f"workers failed: {sorted(task_ids)}")
        titles = "; ".join(str(t.get("title") or tid) for tid, t in tasks.items())
        self._page(
            job_id,
            f"{job_id}: {len(task_ids)} worker(s) need you: {titles}"[:300],
            {"kind": "worker_failed", "tasks": tasks, "actions": actions},
            resumable=can_retry,
        )

    def _reserve_wave(self, job_id: str, task_ids: list[str]) -> dict[str, tuple[int, float]]:
        """Reserve every worker of a wave before any starts, each for what's
        left of its own cap; on a refusal, release them, block the job and
        page: dispatch halts."""
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
        for name in ("spec.md", "plan.md", "approval.json"):
            art = self.store.get_artifact(job_id, name)
            if art is not None:
                path = clone / ".vigil" / job_id / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(art.content)
                paths.append(f".vigil/{job_id}/{name}")
        if paths and not start:
            self.workspace.commit_before_session(clone, paths, f"vigil: {job_id} spec and plan")
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
            self._resume_from(prev.id) if prev and (published or escalated or crashed) else None
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
        self.store.update_session(s.id, detail={"image": session_image})
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
                )
            except Exception as e:  # docker or git trouble: no verdict, nothing accepted
                log.exception("verifier failed on %s", task_id)
                return self._fail(
                    job_id, s, task_id, f"{task_id}: verifier error", {"error": str(e)[:1000]}
                )
            self.store.add_artifact(
                job_id, "tests.json", json.dumps(tests), Provenance.HUMAN, task_id=task_id
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
        )
        if errors:
            return self._fail(
                job_id, s, task_id, f"{task_id}: stage 3 acceptance failed", {"errors": errors}
            )
        _, changed_cfg = runner_config_changes(
            self.workspace,
            job.repo,
            base_sha,
            head,
            contract,
            self.cfg.repo(job.repo).runner_config,
        )
        runner_note = (
            "**This task changes how tests run:** "
            + ", ".join(f"`{p}`" for p in changed_cfg)
            + ". The spec names each path; the verifier ran the base's copies.\n\n"
            if changed_cfg
            else ""
        )
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
                scope=list(contract["spec"]["files_in_scope"]),
                existing=pr_url,
            )
        except PublishError as e:
            return self._fail(job_id, s, task_id, f"{task_id}: publish refused", {"error": str(e)})
        self.store.update_session(s.id, status="pr_open", pr_url=url, detail={"head": head})
        self.store.update_task(task_id, state="pr_open")
        self.store.add_event(
            "pr_open",
            {"pr_url": url, "branch": branch, "head": head},
            Provenance.HUMAN,
            job_id=job_id,
            session_id=s.id,
            task_id=task_id,
        )
        return True

    # --- stages 5-6: review and audit (phase 2a, item 1) ---------------------------------

    def _task_head(self, task_id: str) -> str | None:
        """The head a task's draft PR last published, from vigild's own record."""
        rows = self.store.q(
            "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return str(json.loads(rows[0]["data"])["head"]) if rows else None

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
        return {"round": rounds, "findings": doc.get("findings") or []}

    def review(self, job_id: str) -> None:
        """Review every published task at its head (a Sonnet reviewer, fresh
        context, read-only tools), audit where the ladder samples (Opus), and
        move the job: all clean is `ready`; a finding of medium or higher goes
        back to the same worker as a fix round; a third round with findings,
        after rule 5 escalated the second, pages the owner."""
        job = self.store.get_job(job_id)
        if job.state is not JobState.IN_REVIEW:
            return
        tasks = [t for t in self.store.list_tasks(job_id) if t.state == "pr_open"]
        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
            outcomes = dict(zip(
                [t.id for t in tasks],
                [f.result() for f in [pool.submit(self._review_task, job, t) for t in tasks]],
                strict=True,
            ))  # fmt: skip
        failed = {tid: why for tid, (kind, why) in outcomes.items() if kind == "failed"}
        if failed:
            self._block(job_id, f"review failed: {sorted(failed)}")
            self._page(
                job_id,
                f"{job_id}: the review stage failed for {', '.join(sorted(failed))}",
                {"kind": "review_failed", "tasks": failed, "actions": ["retry", "abandon"]},
                resumable=True,
            )
            return
        fixes = sorted(tid for tid, (kind, _) in outcomes.items() if kind == "fix")
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
                return
            transition(
                self.store,
                job_id,
                JobState.BUILDING,
                Actor.VIGILD,
                evidence={"findings": {tid: outcomes[tid][1] for tid in fixes}},
            )
            self.dispatch(job_id)
            return
        transition(self.store, job_id, JobState.READY, Actor.VIGILD)
        self._ready(job_id)

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
                    "audited": audit is not None,
                    "match_rate": round(audit.match_rate(), 3) if audit else None,
                }
            )
        self.store.create_inbox_item(
            "ready", "ready_to_merge", job_id, f"Ready to merge: {job.title}", {"prs": prs}
        )

    def _review_task(self, job: Any, t: Any) -> tuple[str, Any]:
        """One task's review (and audit, when sampled): ("clean" | "fix" |
        "failed", detail). Never raises: a vigild error is a failed review."""
        try:
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
        rung = self.ladder.rung(shape)
        reviewer, auditor = self.ladder.presets(shape)
        rnd = task.review_rounds + 1
        audit_required = self.ladder.should_audit(shape, level)
        base_sha = self.workspace.base_sha(t.id)
        diff = self.workspace.full_diff(job.repo, base_sha, head)
        spec_md = self.store.get_artifact(job.id, "spec.md")
        report = self.store.get_artifact(job.id, "worker_report.json", t.id)
        tests = self.store.get_artifact(job.id, "tests.json", t.id)
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
            self.cfg.budgets.review_session_usd,
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
                self.cfg.budgets.audit_session_usd,
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
            change = self.ladder.record(audit, verify_level=level)
            if change is not None:
                self._ladder_page(change)
            findings += [
                f.model_dump(mode="json", exclude={"matched"})
                for f in audit.missed()
                if f.severity in BLOCKING
            ]
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
        self._reroute_for_round(job.id, t.id, rnd)
        self.store.update_task(t.id, state="fix")
        return "fix", len(findings)

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

    def _ladder_page(self, change: LadderChange) -> None:
        """You see every promotion and demotion (design, trust ladder)."""
        verb = "promoted" if change.to > change.frm else "demoted"
        self._page(
            None,
            f"review ladder: {change.shape.value} {verb} to rung {change.to}",
            {"kind": "ladder", "shape": change.shape.value, "from": change.frm, "to": change.to,
             "reason": change.reason, "actions": ["dismiss"]},
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
            check_backend(self.cfg, job.repo, preset)
            rid = self.budget.reserve(
                session_id=s.id, task_id=None, amount=amount, purpose=role.value
            )
        except (RepoPolicyError, BudgetExceeded) as e:
            self.store.update_session(s.id, status="refused")
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
            max_budget_usd=amount,
            mounts=self.workspace.overlays(clone),
            packet=packet,
            image=session_image,
            output_schema=model.model_json_schema(),
        )
        attrs = {
            "vigil.job_id": job.id,
            "vigil.task_id": t.id,
            "vigil.role": role.value,
            "vigil.repo": job.repo,
            "vigil.model": spec.model,
            "vigil.attempt": rnd,
        }
        out = self._run_session(s, spec, rid, attrs)
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
