"""Turning submissions into jobs, filing specs for approval, and approving.

Every piece of work runs inside a job, and every job waits in
`spec_pending` until the owner approves its spec from the inbox:
- a complete task (test_cmd + done_when) becomes a one-worker job whose
  spec.md and plan.md vigild writes from the contract (provenance human);
- a task without them, or a free-form request, becomes a job whose coord
  drafts spec.md and plan.md (the spec-writing task of routing rule 1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from vigild.config import Config, ConfigError
from vigild.contract.models import BenchInfo, Score, Source, Task
from vigild.db import Store
from vigild.devimage import missing_tools
from vigild.gateway.firstcall import refusals as first_call_refusals
from vigild.hidden import ARTIFACT as HIDDEN_ARTIFACT
from vigild.hidden import public_view
from vigild.intake.paths import matches_any
from vigild.intake.scoring import IntakeOutcome, score
from vigild.intake.spec_gate import check_job_spec, check_task, runner_files_named
from vigild.jobs.artifacts import canonicalize_plan, parse_plan, parse_spec
from vigild.jobs.fixfindings import MAX_PICKED, pick, selectable
from vigild.jobs.fsm import KILL_CHANNELS, TERMINAL, Actor, TransitionError, transition
from vigild.jobs.model import InboxItem, Job, JobState
from vigild.jobs.ownernote import NOTE_MAX_BYTES
from vigild.metering.calls import session_cost
from vigild.provenance import Provenance
from vigild.redact import redact
from vigild.repos import is_bench_repo
from vigild.router.rules import RoutingError, route
from vigild.verifier import runner_overlap


class Runner(Protocol):
    def on_job_created(self, job_id: str, needs_spec: bool) -> None: ...
    def on_approved(self, job_id: str) -> None: ...
    def on_resume(self, job_id: str, *, replan: bool = False) -> str | None: ...
    def on_rebuild(self, repo: str, base_sha: str) -> None: ...
    def busy(self, job_id: str) -> bool: ...
    def fix_pr_state(self, job_id: str, task_id: str) -> str | None: ...
    def fix_check(self, job_id: str, task_id: str) -> str | None: ...
    def fix_escalation(self, job_id: str, task_id: str) -> str | None: ...
    def prepare_fix(self, job_id: str, task_id: str) -> int: ...
    def replay_review(
        self, job_id: str, task_id: str, prompt_version: str, head: str | None = None
    ) -> dict[str, Any]: ...
    def replay_set(self, prompt_version: str) -> dict[str, Any]: ...


class SubmitError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class DuplicateIssue(SubmitError):
    """The issue already has a job that isn't abandoned (the database's unique
    index said so); `job_id` is that job."""

    def __init__(self, repo: str, issue: int, job_id: str) -> None:
        super().__init__([f"{repo}#{issue} already has a job: {job_id}"])
        self.job_id = job_id


log = logging.getLogger(__name__)


class NotFound(KeyError):
    pass


class Conflict(ValueError):
    pass


class KillFailed(RuntimeError):
    """A kill's effects failed at `step`; the job is `abandoned` and a second kill
    finishes the work (issue #52)."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step


class BadNote(ValueError):
    """A replan note that is empty, not text, or missing where one is required."""


class NoteTooLarge(ValueError):
    """A replan note over NOTE_MAX_BYTES."""


@dataclass
class SubmitOutcome:
    state: str
    task_id: str | None
    job_id: str | None = None
    needs_spec: bool = False
    reasons: list[str] = field(default_factory=list)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    base: str = "main"
    title: str
    request: str
    notes: list[str] = Field(default_factory=list)
    why: str
    value: Score
    risk: Score
    source: Source = Source.HUMAN
    labels: list[str] = Field(default_factory=list)
    # The issue in `repo` this answers: label intake skips an issue a live job carries.
    issue: int | None = Field(default=None, gt=0, strict=True)

    @field_validator("source")
    @classmethod
    def _not_bench(cls, v: Source) -> Source:
        if v is Source.BENCH:
            raise ValueError("bench jobs are created only by JobService.submit_bench")
        return v


def render_spec(task: Task, money_globs: list[str]) -> str:
    s = task.spec
    scope = s.files_in_scope
    money = [f for f in scope if matches_any(f, money_globs)]
    lines = [
        f"# Spec: {s.goal}",
        "",
        "## Goal",
        s.goal,
        "",
        "## Non-goals",
        "- Anything outside the files in scope.",
        "",
        "## Acceptance criteria",
        f"- AC1: {s.done_when} test: `{s.test_cmd}`",
        "",
        "## Files in scope",
        *[f"- `{f}`" for f in scope],
        "",
        "## Files out of scope",
        *([f"- `{f}`" for f in s.out_of_scope] or ["- none listed"]),
        "",
        "## Money paths",
        *([f"- `{f}`" for f in money] or ["none"]),
        "",
        "## Risks",
        f"- Risk score {task.risk}/5. Why this is worth doing: {task.why}",
        "",
        "## Rollback",
        "Revert the PR.",
        "",
    ]
    return "\n".join(lines)


def render_plan(task: Task) -> str:
    worker = {
        "task_id": task.task_id,
        "goal": task.spec.goal,
        "why": task.why,
        "value": task.value,
        "risk": task.risk,
        "size": (task.size.value if task.size else "S"),
        "test_cmd": task.spec.test_cmd,
        "done_when": task.spec.done_when,
        "files_in_scope": task.spec.files_in_scope,
        "out_of_scope": task.spec.out_of_scope,
        "must_fail_first": task.spec.must_fail_first,
        "must_fail_first_reason": task.spec.must_fail_first_reason,
        "depends_on": [],
        "labels": task.labels,
        "max_cost_usd": task.limits.max_cost_usd,
        "verify_level": task.spec.verify.level,
    }
    return (
        "# Plan\n\nOne worker, from the submitted contract.\n\n## Workers\n```json\n"
        + json.dumps(
            {
                "workers": [worker],
                "verify": [
                    {
                        "criterion": "AC1",
                        "level": task.spec.verify.level,
                        "why_not_higher": "submitted as a single contract; phase 1 builds level 0"
                        if task.spec.verify.level == 0
                        else None,
                    }
                ],
            },
            indent=2,
        )
        + "\n```\n"
    )


class JobService:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        runner: Runner,
        files: Callable[[str, str], Callable[[str], str | None]] | None = None,
        dirs: Callable[[str, str], Callable[[str], bool]] | None = None,
    ) -> None:
        self.dirs = dirs  # (repo, base) -> is this path a directory at the base's tip
        self.files = files  # (repo, base) -> read a file at the base's tip
        self.cfg = cfg
        self.store = store
        self.runner = runner
        # how each job's last resume, retry or re-plan ran: "started", or
        # "queued" behind its current run (2a round 3), for the API to say
        self.last_run: dict[str, str | None] = {}
        # the owner's fix round that moves the task to a pricier backend says so
        self.last_notice: dict[str, str | None] = {}
        self._kill_locks: dict[str, threading.Lock] = {}  # one kill at a time per job
        self._kill_locks_guard = threading.Lock()

    # --- submissions ----------------------------------------------------------

    def _intake(self, req: dict[str, Any], files: list[str]) -> list[str]:
        r = score({**req, "files_in_scope": files}, self.cfg.routing.money_paths)
        if r.outcome is IntakeOutcome.REJECT:
            raise SubmitError(r.reasons)
        return r.reasons if r.outcome is IntakeOutcome.PROPOSE else []

    def _gate(
        self, repo: str, spec_text: str, plan_text: str, base: str, job_id: str | None = None
    ) -> list[str]:
        return check_job_spec(
            parse_spec(spec_text),
            parse_plan(plan_text),
            self.cfg.routing.money_paths,
            missing_tools=lambda cmd: missing_tools(self.cfg, repo, cmd),
            # the base's files, so the gate resolves package scripts as the
            # verifier will (round 4, item 6)
            read=self.files(repo, base) if self.files is not None else None,
            is_dir=self.dirs(repo, base) if self.dirs is not None else None,
            # a worker id is never reused in a job (phase 1 run finding #4)
            existing_task_ids=self._used_task_ids(job_id) if job_id else (),
        )

    def _first_call_fits(self, repo: str) -> None:
        """Preflight: refuse, with the numbers, a job whose roles' first calls
        could not fit their reservations. Nothing exists yet: no job, no
        session, no container, no attempt used."""
        errors = first_call_refusals(self.cfg, repo)
        if errors:
            raise SubmitError(errors)

    @staticmethod
    def _not_bench(repo: str, source: Source | str) -> None:
        """A bench repo or the bench source reaches a job only through `submit_bench`."""
        if is_bench_repo(repo) or str(getattr(source, "value", source)) == "bench":
            raise SubmitError(["bench jobs are created only by JobService.submit_bench"])

    def submit_bench(
        self, *, repo: str, base: str, title: str, request: str, bench: BenchInfo
    ) -> Job:
        """Make a bench job and start its coord (bench slice 1a). The only way in:
        there is no REST route. The repo is a virtual `bench/<name>` one that
        configs/repos.yaml lists; the run never touches GitHub, skips the owner's
        spec approval (`approve_bench`), and draws from the experiment line."""
        if not is_bench_repo(repo):
            raise SubmitError([f"a bench job lives on a bench/ repo, not {repo}"])
        try:
            self.cfg.repo(repo)
            self._first_call_fits(repo)
        except ConfigError as e:
            raise SubmitError([str(e)]) from e
        job = self.store.create_job(
            repo=repo, base=base, title=title, request=request, notes=[], source="bench"
        )
        meta = {
            "why": f"bench task {bench.task_id} ({bench.dataset})",
            "value": 3,
            "risk": 2,
            "labels": [],
        }
        self.store.add_artifact(job.id, "intake.json", json.dumps(meta), Provenance.HUMAN)
        self.store.add_artifact(
            job.id, "bench.json", json.dumps(bench.model_dump(mode="json"), sort_keys=True),
            Provenance.HUMAN,
        )  # fmt: skip
        self.runner.on_job_created(job.id, True)
        return self.store.get_job(job.id)

    def approve_bench(self, job_id: str) -> Job:
        """`approval: bench` (owner, 2026-10-27): a bench job's draft goes straight to
        `approved`, through the FSM's guarded bench edge. The spec gate's checks have
        run and are recorded; a failed gate is tagged by the caller and the run
        continues, so bench measures spec quality."""
        job = self.store.get_job(job_id)
        if job.source != "bench":
            raise Conflict(f"{job_id} is not a bench job")
        try:
            job = transition(
                self.store, job_id, JobState.APPROVED, Actor.VIGILD,
                evidence={"bench": True, "approval": "bench"},
            )  # fmt: skip
        except TransitionError as e:
            raise Conflict(str(e)) from e
        self.store.add_artifact(
            job_id, "approval.json",
            json.dumps({"approval": "bench", "actor": "vigild", "edits": None}),
            Provenance.HUMAN,
        )  # fmt: skip
        self.store.add_event("approval", {"approval": "bench"}, Provenance.HUMAN, job_id=job_id)
        return job

    def submit_task(self, raw: dict[str, Any]) -> SubmitOutcome:
        raw = dict(raw)
        # vigild assigns task ids (review r1, item 16); a caller's id is ignored.
        raw["task_id"] = "pending-w1"
        try:
            task = Task.model_validate(raw)
            self._not_bench(task.repo, task.source)
            self.cfg.repo(task.repo)
            route(task, self.cfg)  # refuse what can't be routed (rule 2 overrides, policy)
            self._first_call_fits(task.repo)
        except RoutingError as e:
            raise SubmitError([str(e)]) from e
        except ValidationError as e:
            raise SubmitError(
                [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            ) from e
        except ConfigError as e:
            raise SubmitError([str(e)]) from e

        # vigild computes the runner-file class; a caller's value is replaced
        task = task.model_copy(
            update={
                "runner_file_change": runner_overlap(
                    task.spec.files_in_scope,
                    self.cfg.repo(task.repo).runner_config,
                    task.spec.test_cmd or "",
                )
            }
        )
        intake = {"why": task.why, "value": task.value, "risk": task.risk, "source": task.source}
        proposed = self._intake(intake, task.spec.files_in_scope)
        if proposed:
            tid = f"proposal-{len(self.store.list_tasks()) + 1}"
            try:
                self.store.create_task(
                    None, {**task.model_dump(mode="json"), "task_id": tid}, Provenance.HUMAN
                )
            except sqlite3.IntegrityError as e:
                raise SubmitError([f"task {tid} already exists"]) from e
            self.store.update_task(tid, state="proposed")
            return SubmitOutcome("proposed", tid, reasons=proposed)

        gate = check_task(task)
        if not gate.needs_spec:
            # Run the job spec gate before any job exists, so a refusal leaves no orphan.
            errors = self._gate(
                task.repo,
                render_spec(task, self.cfg.routing.money_paths),
                render_plan(task),
                task.base,
            )
            if errors:
                raise SubmitError(errors)
        job = self.store.create_job(
            repo=task.repo,
            base=task.base,
            title=task.spec.goal,
            request=f"{task.spec.goal}\n\nWhy: {task.why}",
            notes=[],
            source=task.source.value,
        )
        intake_doc = {**intake, "labels": task.labels, "submitted": task.model_dump(mode="json")}
        self.store.add_artifact(
            job.id, "intake.json", json.dumps(intake_doc, sort_keys=True), Provenance.HUMAN
        )
        if gate.needs_spec:
            self.runner.on_job_created(job.id, True)
            return SubmitOutcome("spec_pending", None, job.id, needs_spec=True, reasons=gate.errors)

        task = task.model_copy(update={"task_id": f"{job.id}-w1", "job_id": job.id})
        self.store.add_artifact(
            job.id, "spec.md", render_spec(task, self.cfg.routing.money_paths), Provenance.HUMAN
        )
        self.store.add_artifact(job.id, "plan.md", render_plan(task), Provenance.HUMAN)
        self.store.create_task(job.id, task.model_dump(mode="json"), Provenance.HUMAN)
        self.store.update_task(task.task_id, state="awaiting_approval")
        errors = self.file_for_approval(job.id)
        if errors:
            raise SubmitError(errors)
        self.runner.on_job_created(job.id, False)
        return SubmitOutcome("spec_pending", task.task_id, job.id)

    def submit_request(self, req: JobRequest, *, issue: dict[str, Any] | None = None) -> Job:
        """A free-form request becomes a job its coord drafts. `issue` (label
        intake, plan 2a item 2) is the GitHub issue it came from: stored as
        `issue.json` before the coord starts, and fenced in its packet."""
        self._not_bench(req.repo, req.source)
        try:
            self.cfg.repo(req.repo)
            self._first_call_fits(req.repo)
        except ConfigError as e:
            raise SubmitError([str(e)]) from e
        self._intake(req.model_dump(mode="json"), [])
        try:
            job = self.store.create_job(
                repo=req.repo,
                base=req.base,
                title=req.title,
                request=req.request,
                notes=req.notes,
                source=req.source.value,
                issue=req.issue,
            )
        except sqlite3.IntegrityError:  # jobs_live_issue: the database is the check
            held = None if req.issue is None else self.store.live_job_for_issue(req.repo, req.issue)
            if req.issue is None or held is None:
                raise
            raise DuplicateIssue(req.repo, req.issue, held) from None
        self.store.add_artifact(job.id, "intake.json", req.model_dump_json(), Provenance.HUMAN)
        if issue is not None:
            self.store.add_artifact(job.id, "issue.json", json.dumps(issue), Provenance.HUMAN)
        self.runner.on_job_created(job.id, True)
        return job

    # --- spec gate and the inbox ---------------------------------------------

    def flagged_tasks(self, job_id: str) -> dict[str, list[str]]:
        """Tasks of the stored plan whose scope names a runner file, with those
        files (owner ruling, 2026-10-23): they need hidden tests and an owner look."""
        plan = self.store.get_artifact(job_id, "plan.md")
        if plan is None:
            return {}
        runner_config = self.cfg.repo(self.store.get_job(job_id).repo).runner_config
        out: dict[str, list[str]] = {}
        for w in parse_plan(plan.content).workers:
            if hit := runner_overlap(w.files_in_scope, runner_config, w.test_cmd):
                out[w.task_id] = hit
        return out

    def _hidden_frozen(self, job_id: str) -> dict[str, bool]:
        """The host switch as it is when the spec is gated: recorded in `spec_gate.json`,
        so what is built and verified follows the spec the owner approved."""
        return {"hidden_tests_enabled": self._hidden_live(job_id)}

    def _hidden_live(self, job_id: str) -> bool:
        return self.cfg.repo(self.store.get_job(job_id).repo).hidden_tests.enabled

    def hidden_enabled(self, job_id: str) -> bool:
        """Whether hidden tests apply to this job: the value frozen at its spec gate,
        or, for a job gated before the freeze existed, the live host policy."""
        art = self.store.get_artifact(job_id, "spec_gate.json")
        frozen = json.loads(art.content).get("hidden_tests_enabled") if art else None
        return frozen if isinstance(frozen, bool) else self._hidden_live(job_id)

    def needs_hidden(self, job_id: str) -> dict[str, list[str]]:
        """`flagged_tasks`, but only when the host policy turns hidden tests on for
        the job's repo: off, nothing waits for an author. A bench job is graded by its
        task's own tests, so none is ever written for it."""
        if self.store.get_job(job_id).source == "bench" or not self.hidden_enabled(job_id):
            return {}
        return self.flagged_tasks(job_id)

    def file_for_approval(self, job_id: str) -> list[str]:
        """Run the job spec gate on the stored spec and plan, and file the inbox
        item. A job with a runner-file task is not filed here: the runner's
        author writes its hidden tests first (it never blocks a submit), then
        calls `file_item`."""
        errors = self.gate_spec(job_id)
        if errors or self.needs_hidden(job_id):
            return errors
        if self.store.get_job(job_id).source == "bench":
            return []  # no owner step: the runner takes the bench edge
        self.file_item(job_id)
        return []

    def _used_task_ids(self, job_id: str) -> list[str]:
        """The job's tasks that a plan has already been built from. A task still
        `submitted` or `awaiting_approval` is the job's own, made at submit from the
        plan vigild wrote for it; that plan owns the id."""
        waiting = ("submitted", "awaiting_approval")
        return [t.id for t in self.store.list_tasks(job_id) if t.state not in waiting]

    def _next_task_number(self, job_id: str) -> int:
        """One past the job's highest task number: a re-plan's tasks are new ones."""
        used = [
            int(m.group(1))
            for tid in self._used_task_ids(job_id)
            if (m := re.fullmatch(re.escape(job_id) + r"-w(\d+)", tid))
        ]
        return max(used, default=0) + 1

    def gate_spec(self, job_id: str) -> list[str]:
        spec = self.store.get_artifact(job_id, "spec.md")
        plan = self.store.get_artifact(job_id, "plan.md")
        if spec is None or plan is None:
            errors = ["spec.md and plan.md are both required"]
        else:
            job0 = self.store.get_job(job_id)
            errors = self._gate(job0.repo, spec.content, plan.content, job0.base, job_id)
        self.store.add_artifact(
            job_id,
            "spec_gate.json",
            json.dumps({"ok": not errors, "errors": errors, **self._hidden_frozen(job_id)}),
            Provenance.HUMAN,
        )
        if errors:
            return errors
        # vigild assigns task ids: {job}-w1, {job}-w2, ... (review r1, item 16)
        canonical = canonicalize_plan(
            job_id,
            plan.content,  # type: ignore[union-attr]
            self._next_task_number(job_id),
        )
        if canonical != plan.content:  # type: ignore[union-attr]
            self.store.add_artifact(job_id, "plan.md", canonical, plan.provenance)  # type: ignore[union-attr]
        flagged = self.flagged_tasks(job_id)
        if flagged:
            self.store.add_artifact(
                job_id,
                "spec_gate.json",
                json.dumps(
                    {
                        "ok": True,
                        "errors": [],
                        "runner_file_change": flagged,
                        **self._hidden_frozen(job_id),
                    }
                ),
                Provenance.HUMAN,
            )
        return []

    def file_item(self, job_id: str) -> None:
        """File the spec inbox item for a spec that passed the gate. One open
        spec item per job: a second call (a retry, a restart) files nothing."""
        with self.store.tx():
            if self.open_spec_item(job_id) is not None:
                return
            self._file_item(job_id)

    def _stage_spend(self, job_id: str) -> dict[str, float]:
        """Coord and hidden-test author spend so far, shown side by side on the
        spec item. The author's is outside every task's own cap."""
        out = {"coord_usd": 0.0, "hidden_test_author_usd": 0.0}
        for s in self.store.list_sessions(job_id):
            key = {"coord": "coord_usd", "hidden_test_author": "hidden_test_author_usd"}.get(
                s.role.value
            )
            if key:
                out[key] += session_cost(self.store, s.id)
        return {k: round(v, 6) for k, v in out.items()}

    def open_spec_item(self, job_id: str) -> InboxItem | None:
        for it in self.store.list_inbox():
            if it.job_id == job_id and it.kind == "spec":
                return it
        return None

    def _hidden_view(self, job_id: str, entry: dict[str, Any] | None) -> dict[str, Any]:
        """What the spec item says of a flagged task's hidden tests."""
        if not self.hidden_enabled(job_id):
            return {
                "status": "disabled",
                "note": "hidden tests are disabled (host policy); the task is built on its "
                "visible tests and opens a draft PR for your review",
            }
        return public_view(entry or {})

    def _amendment(self, job_id: str) -> dict[str, Any]:
        """What the coord changed in the previously approved spec, for the spec item:
        the coord's changelog and vigild's diff, only while they belong to the spec
        on file (a later redraft has a newer spec than the diff)."""
        spec = self.store.get_artifact(job_id, "spec.md")
        diff = self.store.get_artifact(job_id, "spec_diff.md")
        log = self.store.get_artifact(job_id, "spec_changelog.md")
        note = self.store.get_artifact(job_id, "owner_note.md")
        if spec is None or diff is None or log is None or note is None or diff.id < spec.id:
            return {}
        return {
            "amendment": {
                "changelog": log.content,
                "diff": diff.content,
                "note_bytes": len(note.content.encode()),
            }
        }

    def _file_item(self, job_id: str) -> None:
        plan = self.store.get_artifact(job_id, "plan.md")
        spec = self.store.get_artifact(job_id, "spec.md")
        assert plan is not None
        pdoc = parse_plan(plan.content)
        job = self.store.get_job(job_id)
        flagged = self.flagged_tasks(job_id)
        stored = self.store.get_artifact(job_id, HIDDEN_ARTIFACT)
        hidden = json.loads(stored.content).get("tasks", {}) if stored else {}
        intake_art = self.store.get_artifact(job_id, "intake.json")
        intake = json.loads(intake_art.content) if intake_art else {}
        sdoc = parse_spec(spec.content) if spec else None
        self.store.create_inbox_item(
            "specs",
            "spec",
            job_id,
            f"Approve spec: {job.title}",
            {
                "repo": job.repo,
                # approve, or send it back with a note (never an approval)
                "actions": ["approve", "replan"],
                **self._amendment(job_id),
                "spend": self._stage_spend(job_id),
                "why": intake.get("why"),
                "value": intake.get("value"),
                "risk": intake.get("risk"),
                "decider": intake.get("decider"),
                "money_paths": sdoc.money_paths if sdoc else [],
                "workers": [
                    {
                        "task_id": w.task_id,
                        "goal": w.goal,
                        "test_cmd": w.test_cmd,
                        "must_fail_first": w.must_fail_first,
                        "must_fail_first_reason": w.must_fail_first_reason,
                        # round 3, F1: the spec lets this worker change how tests run
                        "changes_how_tests_run": runner_files_named(
                            w.files_in_scope, self.cfg.repo(job.repo).runner_config, w.test_cmd
                        ),
                        # owner ruling, 2026-10-23: hidden tests (names and
                        # categories only) and an owner look
                        "runner_file_change": flagged.get(w.task_id, []),
                        **(
                            {"hidden_tests": self._hidden_view(job_id, hidden.get(w.task_id))}
                            if w.task_id in flagged
                            else {}
                        ),
                        "size": w.size.value,
                        "files_in_scope": w.files_in_scope,
                        "depends_on": w.depends_on,
                    }
                    for w in pdoc.workers
                ],
            },
        )

    def file_deps(
        self, job_id: str, deps: list[Any], parent_id: str, parent_tasks: list[str]
    ) -> list[str]:
        """A deps job's spec (plan 2a, item 5; B39), rendered by vigild from
        the requests: the deps gate, then Specs to approve. Approving it is the
        owner's go-ahead for the package manager to run."""
        from vigild.jobs.deps import check_requests

        job = self.store.get_job(job_id)
        pol = self.cfg.repo(job.repo)
        reqs = [d.model_dump() for d in deps]
        names = ", ".join(f"{d['package']}=={d['version']} ({d['ecosystem']})" for d in reqs)
        errors = check_requests(list(pol.dev_lockfiles), list(deps))
        why = "; ".join(str(d.get("why") or "") for d in reqs if d.get("why"))[:500]
        self.store.add_artifact(
            job_id, "intake.json",
            json.dumps({"why": f"{parent_id} needs {names}", "value": 3, "risk": 2,
                        "source": "self", "labels": ["deps"]}),
            Provenance.HUMAN,
        )  # fmt: skip
        self.store.add_artifact(
            job_id, "deps.json",
            json.dumps({"deps": reqs, "parent_job": parent_id, "parent_tasks": parent_tasks}),
            Provenance.CLAUDE,  # the requests came from a worker
        )  # fmt: skip
        files = "\n".join(f"- `{f}`" for f in pol.dev_lockfiles)
        spec = (
            f"# Spec: deps: {names}\n\n## Goal\nAdd {names}, which {', '.join(parent_tasks)} "
            f"(job `{parent_id}`) needs.\n\n## Non-goals\n- Any change outside the manifests' "
            "dependency fields and the lockfiles.\n\n## Acceptance criteria\n- AC1: the lockfiles "
            "hold the requested versions, and every package they newly pull in is at least 14 days "
            "old by its registry. test: vigild's deps checks\n- AC2: the dev image rebuilds from "
            "them and every suite passes offline. test: the dev image validation\n\n"
            f"## Files in scope\n{files}\n\n## Files out of scope\n- everything else\n\n"
            "## Money paths\nnone\n\n## Risks\n- A supply-chain change: the owner reviews the PR.\n"
            f"- The worker's reason (untrusted): {why or '(none given)'}\n\n"
            "## Rollback\nRevert the PR.\n"
        )
        plan = (
            "# Plan\n\nOne deps task, no model (B39): the package manager's lockfile-only update "
            "in a build container with registry egress only, the checks, the dev image rebuilt "
            "and validated, and its own PR at rung 1.\n\n```json\n"
            + json.dumps({"deps": reqs, "lockfiles": pol.dev_lockfiles}, indent=1)
            + "\n```\n"
        )
        self.store.add_artifact(job_id, "spec.md", spec, Provenance.HUMAN)
        self.store.add_artifact(job_id, "plan.md", plan, Provenance.HUMAN)
        self.store.add_artifact(
            job_id, "spec_gate.json", json.dumps({"ok": not errors, "errors": errors}),
            Provenance.HUMAN,
        )  # fmt: skip
        if errors:
            self.page(job_id, f"{job_id}: the deps gate refused", {"errors": errors,
                      "actions": ["abandon"]})  # fmt: skip
            return errors
        self.store.create_inbox_item(
            "specs", "spec", job_id, f"Approve deps job: {names}"[:200],
            {
                "repo": job.repo, "deps": reqs, "parent_job": parent_id,
                "parent_tasks": parent_tasks, "shape": "deps", "money_paths": [],
                "workers": [{
                    "task_id": f"{job_id}-w1", "goal": f"deps: {names}",
                    "test_cmd": "(no model; the dev image's suites, offline)",
                    "must_fail_first": False, "changes_how_tests_run": list(pol.dev_lockfiles),
                    "size": "S", "files_in_scope": list(pol.dev_lockfiles), "depends_on": [],
                }],
            },
        )  # fmt: skip
        return []

    def page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> InboxItem:
        """A question or page for the owner: the third inbox queue. A resumable
        page carries the job back to work when the owner resumes it. A bench run pages
        nobody: the page is recorded
        as an event (and its item closed at once), for the run's report."""
        body = {**body, "resumable": resumable}
        if job_id is not None and self.store.is_bench(job_id=job_id):
            item = self.store.create_inbox_item("questions", "page", job_id, title, body)
            self.store.resolve_inbox_item(item.id, "bench")
            self.store.add_event(
                "bench_page", {"title": title, "tag": "harness", **body}, Provenance.HUMAN,
                job_id=job_id,
            )  # fmt: skip
            return self.store.get_inbox_item(item.id)
        item = self.store.create_inbox_item("questions", "page", job_id, title, body)
        self.store.add_event(
            "page", {"item": item.id, "title": title, **body}, Provenance.HUMAN, job_id=job_id
        )
        return item

    def approve(self, item_id: int, edits: str | None = None) -> Job:
        # One transaction: state, approval row, artifact, event and the inbox
        # item commit together or not at all; the build starts after the commit.
        with self.store.tx():
            try:
                item = self.store.get_inbox_item(item_id)
            except KeyError as e:
                raise NotFound(item_id) from e
            if item.queue != "specs" or item.status != "open" or item.job_id is None:
                raise Conflict(f"inbox item {item_id} is not an open spec to approve")
            try:
                job = transition(
                    self.store,
                    item.job_id,
                    JobState.APPROVED,
                    Actor.OWNER,
                    channel="inbox",
                    evidence={"inbox_item_id": item_id},
                )
            except TransitionError as e:
                raise Conflict(str(e)) from e
            self.store.add_approval(job.id, item_id, Actor.OWNER.value, "inbox", edits)
            self.store.add_artifact(
                job.id,
                "approval.json",
                json.dumps({"inbox_item_id": item_id, "actor": "owner", "edits": edits}),
                Provenance.HUMAN,
            )
            self.store.add_event(
                "approval",
                {"inbox_item_id": item_id, "edits": edits},
                Provenance.HUMAN,
                job_id=job.id,
            )
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        self.runner.on_approved(job.id)
        return job

    def _question(self, item_id: int) -> InboxItem:
        try:
            item = self.store.get_inbox_item(item_id)
        except KeyError as e:
            raise NotFound(item_id) from e
        if item.queue != "questions" or item.status != "open":
            raise Conflict(f"inbox item {item_id} is not an open question or page")
        return item

    def resume(self, item_id: int) -> Job:
        """The owner resumes the job a page is about: a held job returns to the
        state it left, and the runner picks up its remaining work."""
        with self.store.tx():
            item = self._question(item_id)
            if not item.body.get("resumable") or item.job_id is None:
                raise Conflict(f"inbox item {item_id} has nothing to resume")
            job = self.store.get_job(item.job_id)
            if job.state in (JobState.BLOCKED, JobState.ESCALATED):
                assert job.blocked_from is not None
                try:
                    job = transition(
                        self.store, job.id, job.blocked_from, Actor.OWNER, channel="inbox",
                        evidence={"inbox_item_id": item_id, "action": "resume"},
                    )  # fmt: skip
                except TransitionError as e:
                    raise Conflict(str(e)) from e
            elif job.state is not JobState.SPEC_PENDING:
                raise Conflict(f"{job.id} is {job.state.value}; nothing to resume")
            self.store.add_event(
                "resume", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=job.id
            )
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
            # round 3, F7: one resume per hold; the job's other resumable pages close
            for other in self.store.list_inbox():
                if other.job_id == job.id and other.body.get("resumable") and other.id != item_id:
                    self.store.resolve_inbox_item(other.id, Actor.OWNER.value)
        self.last_run[job.id] = self.runner.on_resume(job.id)
        return job

    def _offered(self, item_id: int, action: str) -> InboxItem:
        item = self._question(item_id)
        if item.job_id is None or action not in (item.body.get("actions") or []):
            raise Conflict(f"inbox item {item_id} doesn't offer {action}")
        return item

    def retry(self, item_id: int) -> Job:
        """Run the failed and remaining tasks again, each with what's left of
        its own cap (review round 2, N5)."""
        self._offered(item_id, "retry")
        return self.resume(item_id)

    @staticmethod
    def _check_note(note: object) -> None:
        """An owner note is non-empty text of at most NOTE_MAX_BYTES."""
        if not isinstance(note, str) or not note.strip():
            raise BadNote("the note must be non-empty text")
        try:
            size = len(note.encode())
        except UnicodeEncodeError as e:  # a lone surrogate is not text
            raise BadNote("the note is not valid text") from e
        if size > NOTE_MAX_BYTES:
            raise NoteTooLarge(f"the note is over {NOTE_MAX_BYTES} bytes")

    def fix(self, item_id: int, task_id: object, findings: object, note: object = None) -> Job:
        """ "Fix with findings" (owner, 2026-10-27): a reviewed PR goes back to its worker
        with findings the owner chose from its review and audit, and a note. It is the
        2a fix round: the same branch and PR, then the verifier, a re-review and a
        re-audit, on what is left of the task's cap. The job goes ready -> building ->
        in_review -> ready, each move an event, and the round counts in `review_rounds`
        (past the review-round limit the fix is refused, as a review's own is paged).
        Everything is validated before any side effect, and GitHub's live view of the
        PR is read before the transaction opens, never inside it. The findings are model
        text and are fenced for the worker; the note is the owner's,
        stored verbatim as a human artifact."""
        if not isinstance(task_id, str) or not task_id:
            raise BadNote("task_id must be a task's id")
        if not isinstance(findings, list) or not findings or len(findings) > MAX_PICKED:
            raise BadNote(f"findings must be a list of 1 to {MAX_PICKED} ids or indexes")
        if note is not None:
            self._check_note(note)
        live: str | None = None
        try:  # the live PR check is a network call: outside the transaction below
            peek = self.store.get_inbox_item(item_id)
            if peek.job_id is not None and task_id in {
                p.get("task_id") for p in peek.body.get("prs") or []
            }:
                live = self.runner.fix_pr_state(peek.job_id, task_id)
        except KeyError:
            pass  # the transaction refuses it
        with self.store.tx():
            try:
                item = self.store.get_inbox_item(item_id)
            except KeyError as e:
                raise NotFound(item_id) from e
            if item.queue != "ready" or item.kind != "ready_to_merge" or item.job_id is None:
                raise Conflict(f"inbox item {item_id} is not a ready-to-merge item")
            if item.status != "open":
                raise Conflict(f"inbox item {item_id} is {item.status}")
            job = self.store.get_job(item.job_id)
            if self.runner.busy(job.id):
                raise Conflict(f"{job.id} is being worked on; try again in a moment")
            if job.state is not JobState.READY:
                raise Conflict(f"{job.id} is {job.state.value}; only a ready job takes a fix")
            if task_id not in {p.get("task_id") for p in item.body.get("prs") or []}:
                raise Conflict(f"{task_id} is not a task of inbox item {item_id}")
            try:
                task = self.store.get_task(task_id)
            except KeyError as e:
                raise Conflict(f"no task {task_id}") from e
            if task.job_id != job.id or task.state != "reviewed":
                raise Conflict(
                    f"{task_id} is {task.state}, not reviewed with an open PR; "
                    "a merged or closed PR can't be fixed"
                )
            try:
                chosen = pick(selectable(self.store, job.id, task_id), findings)
            except ValueError as e:
                raise Conflict(str(e)) from e
            if live is not None:
                raise Conflict(live)
            if (why := self.runner.fix_check(job.id, task_id)) is not None:
                raise Conflict(why)
            notice = self.runner.fix_escalation(job.id, task_id)
            rnd = self.runner.prepare_fix(job.id, task_id)
            head = self._task_head(task_id)
            self.store.add_artifact(
                job.id,
                "findings.json",
                json.dumps({"round": rnd, "head": head, "source": "owner", "findings": chosen}),
                Provenance.CLAUDE,
                task_id=task_id,
            )  # fmt: skip  (the text is the reviewer's and auditor's; the pick is the owner's)
            data: dict[str, Any] = {
                "inbox_item_id": item_id, "task_id": task_id, "round": rnd,
                "findings": [f["id"] for f in chosen],
            }  # fmt: skip
            if notice is not None:
                data["notice"] = notice
            if note is not None:
                assert isinstance(note, str)
                self.store.add_artifact(
                    job.id, "fix_note.json", json.dumps({"round": rnd, "note": note}),
                    Provenance.HUMAN, task_id=task_id,
                )  # fmt: skip
                data |= {
                    "note_bytes": len(note.encode()),
                    "note_sha256": hashlib.sha256(note.encode()).hexdigest(),
                }
            try:
                job = transition(
                    self.store, job.id, JobState.BUILDING, Actor.OWNER, channel="inbox",
                    evidence={"inbox_item_id": item_id, "action": "fix",
                              "findings": {task_id: len(chosen)}},
                )  # fmt: skip
            except TransitionError as e:
                raise Conflict(str(e)) from e
            self.store.add_event("fix_with_findings", data, Provenance.HUMAN, job_id=job.id,
                                 task_id=task_id)  # fmt: skip
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        self.last_notice[job.id] = notice
        self.last_run[job.id] = self.runner.on_resume(job.id)
        return job

    def _task_head(self, task_id: str) -> str | None:
        rows = self.store.q(
            "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return str(json.loads(rows[0]["data"])["head"]) if rows else None

    def replan(self, item_id: int, note: str | None = None) -> Job:
        """Send a job back to the coord. A held job (a failed build) goes back with
        the failures in the packet; a job waiting on its spec item goes back only
        with a note (the spec item's "replan with note": never an approval, the job
        stays `spec_pending`). With a note the coord amends the previously approved
        spec instead of redrafting it: the note is the owner's, stored verbatim."""
        if note is not None:
            self._check_note(note)
        with self.store.tx():
            try:
                peek = self.store.get_inbox_item(item_id)
            except KeyError as e:
                raise NotFound(item_id) from e
            if peek.job_id is not None and self.runner.busy(peek.job_id):
                raise Conflict(f"{peek.job_id} is being worked on; try again in a moment")
            if peek.queue == "specs":
                job = self._replan_spec(peek, note)
            else:
                item = self._offered(item_id, "replan")
                assert item.job_id is not None
                job = self.store.get_job(item.job_id)
                if job.state is not JobState.SPEC_PENDING:  # a stranded draft already is
                    try:
                        job = transition(
                            self.store, item.job_id, JobState.SPEC_PENDING, Actor.OWNER,
                            channel="inbox",
                            evidence={"inbox_item_id": item_id, "action": "replan"},
                        )  # fmt: skip
                    except TransitionError as e:
                        raise Conflict(str(e)) from e
                self._record_replan(job.id, item_id, note, item.body.get("tasks") or {})
                self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        self.last_run[job.id] = self.runner.on_resume(job.id, replan=True)
        return job

    def _replan_spec(self, item: InboxItem, note: str | None) -> Job:
        """The spec item's "replan with note": the job is already `spec_pending`, where
        the coord drafts, so no state changes; it is not the owner's approval."""
        if item.status != "open" or item.kind != "spec" or item.job_id is None:
            raise Conflict(f"inbox item {item.id} is not an open spec to replan")
        if note is None:
            raise BadNote("replanning a spec needs a note")
        job = self.store.get_job(item.job_id)
        if job.state is not JobState.SPEC_PENDING:
            raise Conflict(f"{job.id} is {job.state.value}, not spec_pending")
        self._record_replan(job.id, item.id, note, {})
        self.store.resolve_inbox_item(item.id, Actor.OWNER.value)
        return job

    def _record_replan(
        self, job_id: str, item_id: int, note: str | None, tasks: dict[str, Any]
    ) -> None:
        """The replan event and, with a note, its artifacts: the note itself (human),
        and the spec it amends: the spec as of the last approval, or, if the job was
        never approved, the current draft (labelled so). Notes accumulate until an
        approval closes them."""
        data: dict[str, Any] = {"inbox_item_id": item_id, "tasks": tasks}
        if note is not None:
            self.store.add_artifact(job_id, "owner_note.md", note, Provenance.HUMAN)
            data |= {
                "note_bytes": len(note.encode()),
                "note_sha256": hashlib.sha256(note.encode()).hexdigest(),
            }
            approval = self.store.get_artifact(job_id, "approval.json")
            spec = (
                self.store.get_artifact(job_id, "spec.md")
                if approval is None
                else self.store.spec_before(job_id, approval.id)
            )
            data["baseline"] = "draft" if approval is None else "approved"
            if spec is None and approval is not None:
                # approved before specs were versioned: the one on file, said so
                spec = self.store.get_artifact(job_id, "spec.md")
                data["baseline"] = "approved-pre-versioning"
            if spec is not None:
                self.store.add_artifact(
                    job_id, "spec_previous.md", spec.content, Provenance(spec.provenance)
                )
        self.store.add_event("replan", data, Provenance.HUMAN, job_id=job_id)

    def abandon(self, item_id: int, job_id: str | None = None) -> Job:
        """Give the job up: nothing more runs; open PRs stay for you to close.
        A breaker's page (one page however many jobs it holds) abandons the
        held job its caller names (2a round 3): any job a breaker holds can
        be abandoned from the inbox."""
        from vigild.breakers import Breakers

        breakers = Breakers(self.store, self.cfg)
        with self.store.tx():
            item = self._question(item_id)
            if "abandon" not in (item.body.get("actions") or []):
                raise Conflict(f"inbox item {item_id} doesn't offer abandon")
            target = item.job_id
            if item.body.get("kind") == "breaker":
                held = breakers.held_by(int(item.body.get("breaker") or 0))
                target = job_id or (item.job_id if item.job_id in held else None)
                if target is None or target not in held:
                    raise Conflict(f"name a job breaker page {item_id} holds: {held}")
            elif job_id not in (None, item.job_id):
                raise Conflict(f"inbox item {item_id} is about {item.job_id}, not {job_id}")
            if target is None:
                raise Conflict(f"inbox item {item_id} names no job")
            try:
                job = transition(
                    self.store, target, JobState.ABANDONED, Actor.OWNER,
                    channel="inbox", evidence={"inbox_item_id": item_id, "action": "abandon"},
                )  # fmt: skip
            except TransitionError as e:
                raise Conflict(str(e)) from e
            self.store.add_event(
                "abandon", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=job.id
            )
            self._close_out(job.id, breakers)
        failed = getattr(self.runner, "job_failed", None)
        if failed is not None:
            failed(job)  # an abandoned job counts toward the repo's breaker (item 4)
        return job

    def _close_out(self, job_id: str, breakers: Any) -> None:
        """What giving a job up leaves behind, closed: its tasks that have no open PR are
        abandoned, its own pages are resolved, and no breaker holds it."""
        for t in self.store.list_tasks(job_id):
            if t.state not in ("pr_open", "reviewed"):
                self.store.update_task(t.id, state="abandoned")
        for other in self.store.list_inbox():
            # the job's own pages; a breaker's page stays for its reset
            # and for the other jobs it holds (2a round 6)
            own = other.job_id == job_id and other.body.get("kind") != "breaker"
            if own and other.queue in ("questions", "specs"):
                self.store.resolve_inbox_item(other.id, Actor.OWNER.value)
        # a breaker that then holds nothing resolves its page but stays
        # open: only its own condition reopens dispatch (owner, 2026-10-23);
        # one that still holds a job keeps its page for it (2a round 7)
        breakers.unhold(job_id, by=Actor.OWNER.value)

    def kill(
        self, job_id: str, actor: Actor, channel: str, *, wait: float = 30.0
    ) -> dict[str, Any]:
        """`vigil kill` (issue #52): stop a job cleanly and give it up. The job moves to
        `abandoned` through the FSM first (so nothing new starts, and the runner's own
        steps find it terminal), then the runner stops its sessions and sidecars, revokes
        its gateway tokens, waits (at most `wait` s) for in-flight calls to drain and
        settles or releases every reservation it holds. Open PRs stay, for you to close.
        A kill is your choice, so it doesn't count toward the repo's breaker.

        Idempotent: a job that is already `done`, or `abandoned` with its kill finished (a
        `job_killed` event), changes nothing and answers `already: <state>`. A kill that
        failed partway leaves the job `abandoned` with no such event; the next kill runs
        the effects again (all of them are safe to repeat) and answers `already:
        abandoned, completed: true`."""
        if channel not in KILL_CHANNELS:
            raise Conflict(f"a kill comes from {' or '.join(KILL_CHANNELS)}, not {channel!r}")
        try:
            self.store.get_job(job_id)
        except KeyError as e:
            raise NotFound(job_id) from e
        with self._kill_lock(job_id):
            return self._kill(job_id, actor, channel, wait)

    def _kill_lock(self, job_id: str) -> threading.Lock:
        with self._kill_locks_guard:
            return self._kill_locks.setdefault(job_id, threading.Lock())

    def _kill(self, job_id: str, actor: Actor, channel: str, wait: float) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        out: dict[str, Any] = {"job_id": job_id, "killed": False, "already": None}

        def already(state: JobState, *, effects: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                **out, "state": state.value, "already": state.value, "completed": True,
                "message": f"{job_id} is already {state.value}; nothing to kill",
                **(effects or {}),
            }  # fmt: skip

        finished = bool(
            self.store.q("SELECT 1 FROM events WHERE job_id=? AND type='job_killed'", (job_id,))
        )
        if job.state is JobState.DONE or (job.state is JobState.ABANDONED and finished):
            return already(job.state)
        was = job.state.value
        if job.state is not JobState.ABANDONED:
            try:
                transition(
                    self.store, job_id, JobState.ABANDONED, actor, channel=channel,
                    evidence={"action": "kill", "from": was},
                )  # fmt: skip
            except TransitionError as e:
                now = self.store.get_job(job_id).state
                if now in TERMINAL:  # a second kill got there first
                    return already(now)
                raise Conflict(str(e)) from e
        effects = self._kill_effects(job_id, wait)
        self.store.add_event(
            "job_killed",
            {"actor": actor.value, "channel": channel, "from": was, **effects},
            Provenance.HUMAN, job_id=job_id,
        )  # fmt: skip
        if was == JobState.ABANDONED.value:  # the finish of a kill that failed partway
            return already(JobState.ABANDONED, effects=effects)
        message = f"killed {job_id} (was {was}); open PRs stay for you to close"
        return {
            **out, "state": JobState.ABANDONED.value, "killed": True, "message": message, **effects,
        }  # fmt: skip

    def _kill_effects(self, job_id: str, wait: float) -> dict[str, Any]:
        """The runner's stop, revoke, drain and settle, then the close-out. A step that
        raises is logged (redacted) and surfaces as `KillFailed` naming it; the job stays
        `abandoned` without a `job_killed` event, so the next kill runs both again."""
        effects: dict[str, Any] = {}
        runner_kill = getattr(self.runner, "kill_job", None)
        if runner_kill is not None:
            try:
                effects = runner_kill(job_id, wait=wait)
            except Exception as e:
                raise self._kill_failed(
                    job_id, "runner.kill_job (stop, revoke, drain, settle)", e
                ) from e
        from vigild.breakers import Breakers

        try:
            with self.store.tx():
                self._close_out(job_id, Breakers(self.store, self.cfg))
        except Exception as e:
            raise self._kill_failed(job_id, "close-out (tasks, pages, breaker holds)", e) from e
        return effects

    @staticmethod
    def _kill_failed(job_id: str, step: str, e: Exception) -> KillFailed:
        what = redact(f"{type(e).__name__}: {e}")
        log.error("kill %s failed at %s: %s", job_id, step, what)
        return KillFailed(step, what[:500])

    def reset(self, item_id: int) -> dict[str, list[str]]:
        """Reset a tripped circuit breaker (plan 2a, item 4): it closes, and
        the jobs it paused resume: `resumed`, or `queued` behind their own
        run that is still finishing (review B4)."""
        with self.store.tx():
            item = self._question(item_id)
            if item.body.get("kind") != "breaker" or "reset" not in (
                item.body.get("actions") or []
            ):
                raise Conflict(f"inbox item {item_id} doesn't offer reset")
            self.store.add_event(
                "reset", {"inbox_item_id": item_id, "breaker": item.body.get("breaker")},
                Provenance.HUMAN, job_id=item.job_id,
            )  # fmt: skip
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        reset = getattr(self.runner, "on_breaker_reset", None)
        if reset is None:
            return {"resumed": [], "queued": []}
        out = reset(int(item.body["breaker"]), item_id)
        return out if isinstance(out, dict) else {"resumed": list(out), "queued": []}

    def rebuild(self, item_id: int) -> None:
        """Build and validate a repo's failed dev image again (round 2)."""
        with self.store.tx():
            item = self._question(item_id)
            if item.body.get("kind") != "dev_image" or "rebuild" not in (
                item.body.get("actions") or []
            ):
                raise Conflict(f"inbox item {item_id} doesn't offer rebuild")
            self.store.add_event("rebuild", {"inbox_item_id": item_id, **{
                k: item.body.get(k) for k in ("repo", "key", "base_sha")
            }}, Provenance.HUMAN)  # fmt: skip
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        self.runner.on_rebuild(str(item.body["repo"]), str(item.body["base_sha"]))

    def dismiss(self, item_id: int) -> None:
        """Resolve a page with nothing to act on. A breaker's page that still
        holds jobs can't be dismissed: they would wait with no page (2a
        round 9); reset the breaker or abandon them."""
        from vigild.breakers import Breakers

        with self.store.tx():
            item = self._question(item_id)
            if item.body.get("kind") == "breaker":
                held = Breakers(self.store, self.cfg).held_by(int(item.body.get("breaker") or 0))
                if held:
                    raise Conflict(f"breaker page {item_id} holds {held}: reset it or abandon them")
            self.store.add_event(
                "dismiss", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=item.job_id
            )
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
