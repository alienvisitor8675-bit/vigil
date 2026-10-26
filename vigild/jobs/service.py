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
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigild.config import Config, ConfigError
from vigild.contract.models import Score, Source, Task
from vigild.db import Store
from vigild.devimage import missing_tools
from vigild.gateway.firstcall import refusals as first_call_refusals
from vigild.hidden import ARTIFACT as HIDDEN_ARTIFACT
from vigild.hidden import public_view
from vigild.intake.paths import matches_any
from vigild.intake.scoring import IntakeOutcome, score
from vigild.intake.spec_gate import check_job_spec, check_task, runner_files_named
from vigild.jobs.artifacts import canonicalize_plan, parse_plan, parse_spec
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import InboxItem, Job, JobState
from vigild.jobs.ownernote import NOTE_MAX_BYTES
from vigild.metering.calls import session_cost
from vigild.provenance import Provenance
from vigild.router.rules import RoutingError, route
from vigild.verifier import runner_overlap


class Runner(Protocol):
    def on_job_created(self, job_id: str, needs_spec: bool) -> None: ...
    def on_approved(self, job_id: str) -> None: ...
    def on_resume(self, job_id: str, *, replan: bool = False) -> None: ...
    def on_rebuild(self, repo: str, base_sha: str) -> None: ...
    def busy(self, job_id: str) -> bool: ...


class SubmitError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class NotFound(KeyError):
    pass


class Conflict(ValueError):
    pass


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
    ) -> None:
        self.files = files  # (repo, base) -> read a file at the base's tip
        self.cfg = cfg
        self.store = store
        self.runner = runner

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

    def submit_task(self, raw: dict[str, Any]) -> SubmitOutcome:
        raw = dict(raw)
        # vigild assigns task ids (review r1, item 16); a caller's id is ignored.
        raw["task_id"] = "pending-w1"
        try:
            task = Task.model_validate(raw)
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

    def submit_request(self, req: JobRequest) -> Job:
        try:
            self.cfg.repo(req.repo)
            self._first_call_fits(req.repo)
        except ConfigError as e:
            raise SubmitError([str(e)]) from e
        self._intake(req.model_dump(mode="json"), [])
        job = self.store.create_job(
            repo=req.repo,
            base=req.base,
            title=req.title,
            request=req.request,
            notes=req.notes,
            source=req.source.value,
        )
        self.store.add_artifact(job.id, "intake.json", req.model_dump_json(), Provenance.HUMAN)
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
        the job's repo: off, nothing waits for an author."""
        if not self.hidden_enabled(job_id):
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

    def page(
        self, job_id: str | None, title: str, body: dict[str, Any], *, resumable: bool = False
    ) -> InboxItem:
        """A question or page for the owner: the third inbox queue. A resumable
        page carries the job back to work when the owner resumes it."""
        body = {**body, "resumable": resumable}
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
        self.runner.on_resume(job.id)
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

    def replan(self, item_id: int, note: str | None = None) -> Job:
        """Send a job back to the coord. A held job (a failed build) goes back with
        the failures in the packet; a job waiting on its spec item goes back only
        with a note (the spec item's "replan with note": never an approval, the job
        stays `spec_pending`). With a note the coord amends the previously approved
        spec instead of redrafting it: the note is the owner's, stored verbatim."""
        if note is not None:
            if not isinstance(note, str) or not note.strip():
                raise BadNote("the note must be non-empty text")
            try:
                size = len(note.encode())
            except UnicodeEncodeError as e:  # a lone surrogate is not text
                raise BadNote("the note is not valid text") from e
            if size > NOTE_MAX_BYTES:
                raise NoteTooLarge(f"the note is over {NOTE_MAX_BYTES} bytes")
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
                try:
                    job = transition(
                        self.store, item.job_id, JobState.SPEC_PENDING, Actor.OWNER,
                        channel="inbox", evidence={"inbox_item_id": item_id, "action": "replan"},
                    )  # fmt: skip
                except TransitionError as e:
                    raise Conflict(str(e)) from e
                self._record_replan(job.id, item_id, note, item.body.get("tasks") or {})
                self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        self.runner.on_resume(job.id, replan=True)
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

    def abandon(self, item_id: int) -> Job:
        """Give the job up: nothing more runs; open PRs stay for you to close."""
        with self.store.tx():
            item = self._offered(item_id, "abandon")
            assert item.job_id is not None
            try:
                job = transition(
                    self.store, item.job_id, JobState.ABANDONED, Actor.OWNER,
                    channel="inbox", evidence={"inbox_item_id": item_id, "action": "abandon"},
                )  # fmt: skip
            except TransitionError as e:
                raise Conflict(str(e)) from e
            for t in self.store.list_tasks(job.id):
                if t.state != "pr_open":
                    self.store.update_task(t.id, state="abandoned")
            self.store.add_event(
                "abandon", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=job.id
            )
            for other in self.store.list_inbox():
                if other.job_id == job.id and other.queue == "questions":
                    self.store.resolve_inbox_item(other.id, Actor.OWNER.value)
        return job

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
        with self.store.tx():
            item = self._question(item_id)
            self.store.add_event(
                "dismiss", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=item.job_id
            )
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
