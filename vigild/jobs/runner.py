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
import math
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
from vigild.jobs.model import FixDepthExceeded, Job, JobState, Role, SessionRow
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
from vigild.repos import RepoPolicyError, check_backend, live_private, repo_rule_errors
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
# A review or an audit answers in a few thousand tokens. Capping each call's
# output keeps the gateway's worst-case hold inside the session's reservation.
REVIEW_OUTPUT_TOKENS = 8192
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

    def close(self, repo: str, url: str) -> None: ...


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
        breakers: Breakers | None = None,
        deps: Any = None,
        pr_state: Callable[[str, str], str] | None = None,
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
        # a resume that arrives while the job's run is going: run it after (B4)
        self._queued: dict[str, Callable[[str], None]] = {}
        # the WIP room check and the move to running, for every job at once (B10)
        self._wip_lock = threading.Lock()
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

    # --- triggers ---------------------------------------------------------------------

    def running(self) -> list[str]:
        with self._running_lock:
            return sorted(self._running)

    def _spawn(self, fn: Callable[[str], None], job_id: str) -> str:
        """One run per job at a time, whatever starts it (round 3, F7). A run
        asked for while one is going is queued, not dropped (review B4): it
        starts when the current one ends. Returns "started" or "queued"."""
        with self._running_lock:
            if job_id in self._running:
                self._queued[job_id] = fn  # the latest ask wins; it reads the job's state then
                log.info("%s is running; its next run is queued", job_id)
                return "queued"
            self._running.add(job_id)

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
                        run = self._queued.pop(j, None)
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
            self._spawn(self.run_coord, job_id)

    def on_approved(self, job_id: str) -> None:
        self._spawn(self.build, job_id)

    def on_resume(self, job_id: str) -> str | None:
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
        return self._spawn(fn, job_id) if fn is not None else None

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
            elif job.state is JobState.IN_REVIEW:
                # review B3: reviews a restart cut off run again; a review
                # session is read-only, so nothing of it needs keeping
                self.deferred.append(job.id)
                out["requeued"] += 1
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
            except TransitionError, KeyError:
                log.warning("could not resume %s (%s)", job_id, evidence)
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

    def _block(self, job_id: str, reason: str, blocked_on: str | None = None) -> None:
        try:
            job = self.store.get_job(job_id)
            if job.state in (JobState.BLOCKED, JobState.ESCALATED, JobState.DONE):
                return
            evidence: dict[str, Any] = {"reason": reason}
            if blocked_on is not None:
                evidence["blocked_on"] = blocked_on
            transition(self.store, job_id, JobState.BLOCKED, Actor.VIGILD, evidence=evidence)
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
                if not self.gateway.drain(s.id):  # its last calls booked first
                    log.warning("%s: a call was still in flight at settling", s.id)
                self.budget.settle(reservation)
                self.store.update_session(s.id, ended_at=iso())
                self._spend_check()

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
                rid = self.budget.reserve(
                    session_id=coord.id,
                    task_id=None,
                    amount=self.cfg.budgets.coord_session_usd,
                    purpose="coord",
                )
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
        deps = {t: list(row.contract.get("depends_on", [])) for t, row in tasks.items()}
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
            for tid in retrying:
                # another attempt; the third is routed by rule 5 (escalated)
                attempt = int(self.store.get_task(tid).contract.get("attempt") or 1)
                self._reroute(job_id, tid, attempt=attempt + 1)
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
        left), re-plan, or abandon. It counts toward the repo's failures. A task
        that needs a dependency instead gets a child deps job (item 5)."""
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
        self.job_failed(self.store.get_job(job_id))
        actions = (["retry"] if can_retry else []) + ["replan", "abandon"]
        self._block(job_id, f"workers failed: {sorted(task_ids)}")
        titles = "; ".join(str(t.get("title") or tid) for tid, t in tasks.items())
        self._page(
            job_id,
            f"{job_id}: {len(task_ids)} worker(s) need you: {titles}"[:300],
            {"kind": "worker_failed", "tasks": tasks, "actions": actions},
            resumable=can_retry,
        )

    def _wave_share(self, task_ids: list[str]) -> float:
        """What the day (and the month) has left, divided across a wave's
        workers (review B1), floored to a hundredth of a cent so the shares
        never sum past it."""
        b = self.cfg.budgets
        n = sum(1 for t in task_ids if self.store.get_task(t).contract.get("shape") != "deps")
        left = min(
            b.daily_usd - self.budget.spent_today() - self.budget.open_today(),
            b.monthly_usd - self.budget.spent_month() - self.budget.open_month(),
        )
        return math.floor(max(0.0, left) / max(1, n) * 1e4) / 1e4

    def _reserve_wave(self, job_id: str, task_ids: list[str]) -> dict[str, tuple[int, float]]:
        """Reserve every worker of a wave before any starts, each for what's
        left of its own cap or its share of what the day has left, whichever
        is less (review B1: a plan of default caps fits the day instead of
        tripping a cap). On a refusal, release them, block the job and page:
        dispatch halts. A retry gets the same treatment, on what's left of
        each task's cap."""
        held: dict[str, tuple[int, float]] = {}
        share = self._wave_share(task_ids)
        for tid in task_ids:
            if self.store.get_task(tid).contract.get("shape") == "deps":
                held[tid] = (0, 0.0)  # a deps job runs no model: nothing to reserve
                continue
            _, spent, left = self.remaining(tid)
            amount = min(left, share)
            try:
                if amount < MIN_RETRY_USD:
                    raise BudgetExceeded(
                        # its own cap used up, or too little of the day left for it
                        "per_task" if left < MIN_RETRY_USD and spent > 0 else "daily",
                        f"task {tid}: ${left:.2f} of its cap left, ${share:.2f} of the day "
                        "for each worker of its wave",
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
        for name in ("spec.md", "plan.md", "approval.json"):
            art = self.store.get_artifact(job_id, name)
            if art is not None:
                path = clone / ".vigil" / job_id / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(art.content)
                paths.append(f".vigil/{job_id}/{name}")
        if paths and not start:
            self.workspace.commit_before_session(clone, paths, f"vigil: {job_id} spec and plan")
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
            "pr_open", {"pr_url": url, "branch": branch, "head": head},
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
        if self._refused_by_visibility(job):
            return
        b = self._paused(job)
        if b is not None:
            self._hold_for(job_id, b)
            return
        tasks = [t for t in self.store.list_tasks(job_id) if t.state == "pr_open"]
        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
            outcomes = dict(zip(
                [t.id for t in tasks],
                [f.result() for f in [pool.submit(self._review_task, job, t) for t in tasks]],
                strict=True,
            ))  # fmt: skip
        failed = {tid: why for tid, (kind, why) in outcomes.items() if kind == "failed"}
        b = self._paused(job)
        if failed and b is not None:
            self._hold_for(job_id, b)  # a cap tripped mid-review: wait on its breaker
            return
        if failed:
            self._block(job_id, f"review failed: {sorted(failed)}")
            self._page(
                job_id,
                f"{job_id}: the review stage failed for {', '.join(sorted(failed))}",
                {"kind": "review_failed", "tasks": failed, "actions": ["retry", "abandon"]},
                resumable=True,
            )
            return
        flagged = {tid: f for tid, (kind, f) in outcomes.items() if kind == "deps_findings"}
        if flagged:
            self._deps_review_failed(job_id, flagged)
            return
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
        self.breakers.success(Kind.REPO_FAILURES, f"repo:{job.repo}")
        self._ready(job_id)

    def _deps_review_failed(self, job_id: str, flagged: dict[str, Any]) -> None:
        """Owner ruling 3: a deps PR has no worker to fix it. A failed review
        closes the PR and regenerates the deps job once, on a new branch; a
        second failed review closes that PR too and pages the owner. The
        parent job stays blocked on this one throughout."""
        job = self.store.get_job(job_id)
        again: list[str] = []
        for tid in sorted(flagged):
            t = self.store.get_task(tid)
            url = self._task_pr(tid)
            if url:
                try:
                    self.publisher.close(job.repo, url)
                except PublishError as e:
                    log.warning("could not close %s: %s", url, e)
                self.store.add_event(
                    "pr_closed", {"pr_url": url, "why": "deps review failed"},
                    Provenance.HUMAN, job_id=job_id, task_id=tid,
                )  # fmt: skip
            closed = self.store.q(
                "SELECT COUNT(*) n FROM events WHERE task_id=? AND type='pr_closed'", (tid,)
            )[0]["n"]
            if closed >= 2:  # this was the regenerated PR: the owner decides
                self.store.update_task(tid, state="closed")
                continue
            contract = {**t.contract, "branch": f"vigil/{tid}-r{closed + 1}"}
            self.store.update_task(tid, contract=contract, state="queued")
            again.append(tid)
        if again and len(again) == len(flagged):
            transition(
                self.store, job_id, JobState.BUILDING, Actor.VIGILD,
                evidence={"findings": flagged, "deps_regenerated": again},
            )  # fmt: skip
            self.dispatch(job_id)
            return
        self._block(job_id, f"deps review findings, twice: {sorted(flagged)}")
        self._page(
            job_id,
            f"{job_id}: the review flagged the deps PR twice; both PRs are closed",
            {"kind": "deps_review", "findings": flagged, "actions": ["abandon"],
             "next": "abandon it (the job waiting on it stays blocked) and add the "
             "dependency by hand, or re-plan the job that needs it"},
        )  # fmt: skip

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
        if task.shape is not None and task.shape.value == "deps":
            # no model wrote it, so no fix round: the owner decides (B39)
            return "deps_findings", findings
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
            most = self.cfg.vigild.breakers.max_escalations_per_day
            if escalations_today(self.store) > most:
                self.breakers.trip(
                    Kind.ESCALATIONS, GLOBAL, {"today": escalations_today(self.store), "max": most}
                )

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
