"""Turning submissions into jobs, filing specs for approval, and approving.

Every piece of work runs inside a job, and every job waits in
`spec_pending` until the owner approves its spec from the inbox:
- a complete task (test_cmd + done_when) becomes a one-worker job whose
  spec.md and plan.md vigild writes from the contract (provenance human);
- a task without them, or a free-form request, becomes a job whose coord
  drafts spec.md and plan.md (the spec-writing task of routing rule 1).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigild.config import Config, ConfigError
from vigild.contract.models import Score, Source, Task
from vigild.db import Store
from vigild.devimage import missing_tools
from vigild.intake.paths import matches_any
from vigild.intake.scoring import IntakeOutcome, score
from vigild.intake.spec_gate import check_job_spec, check_task, runner_files_named
from vigild.jobs.artifacts import canonicalize_plan, parse_plan, parse_spec
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import InboxItem, Job, JobState
from vigild.provenance import Provenance
from vigild.router.rules import RoutingError, route


class Runner(Protocol):
    def on_job_created(self, job_id: str, needs_spec: bool) -> None: ...
    def on_approved(self, job_id: str) -> None: ...
    def on_resume(self, job_id: str) -> str | None: ...
    def on_rebuild(self, repo: str, base_sha: str) -> None: ...


class SubmitError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class NotFound(KeyError):
    pass


class Conflict(ValueError):
    pass


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
        # how each job's last resume, retry or re-plan ran: "started", or
        # "queued" behind its current run (2a round 3), for the API to say
        self.last_run: dict[str, str | None] = {}

    # --- submissions ----------------------------------------------------------

    def _intake(self, req: dict[str, Any], files: list[str]) -> list[str]:
        r = score({**req, "files_in_scope": files}, self.cfg.routing.money_paths)
        if r.outcome is IntakeOutcome.REJECT:
            raise SubmitError(r.reasons)
        return r.reasons if r.outcome is IntakeOutcome.PROPOSE else []

    def _gate(self, repo: str, spec_text: str, plan_text: str, base: str) -> list[str]:
        return check_job_spec(
            parse_spec(spec_text),
            parse_plan(plan_text),
            self.cfg.routing.money_paths,
            missing_tools=lambda cmd: missing_tools(self.cfg, repo, cmd),
            # the base's files, so the gate resolves package scripts as the
            # verifier will (round 4, item 6)
            read=self.files(repo, base) if self.files is not None else None,
        )

    def submit_task(self, raw: dict[str, Any]) -> SubmitOutcome:
        raw = dict(raw)
        # vigild assigns task ids (review r1, item 16); a caller's id is ignored.
        raw["task_id"] = "pending-w1"
        try:
            task = Task.model_validate(raw)
            self.cfg.repo(task.repo)
            route(task, self.cfg)  # refuse what can't be routed (rule 2 overrides, policy)
        except RoutingError as e:
            raise SubmitError([str(e)]) from e
        except ValidationError as e:
            raise SubmitError(
                [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            ) from e
        except ConfigError as e:
            raise SubmitError([str(e)]) from e

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
        try:
            self.cfg.repo(req.repo)
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
        if issue is not None:
            self.store.add_artifact(job.id, "issue.json", json.dumps(issue), Provenance.HUMAN)
        self.runner.on_job_created(job.id, True)
        return job

    # --- spec gate and the inbox ---------------------------------------------

    def file_for_approval(self, job_id: str) -> list[str]:
        """Run the job spec gate on the stored spec and plan; file the inbox item."""
        spec = self.store.get_artifact(job_id, "spec.md")
        plan = self.store.get_artifact(job_id, "plan.md")
        if spec is None or plan is None:
            errors = ["spec.md and plan.md are both required"]
        else:
            pdoc = parse_plan(plan.content)
            job0 = self.store.get_job(job_id)
            errors = self._gate(job0.repo, spec.content, plan.content, job0.base)
        self.store.add_artifact(
            job_id,
            "spec_gate.json",
            json.dumps({"ok": not errors, "errors": errors}),
            Provenance.HUMAN,
        )
        if errors:
            return errors
        # vigild assigns task ids: {job}-w1, {job}-w2, ... (review r1, item 16)
        canonical = canonicalize_plan(job_id, plan.content)  # type: ignore[union-attr]
        if canonical != plan.content:  # type: ignore[union-attr]
            self.store.add_artifact(job_id, "plan.md", canonical, plan.provenance)  # type: ignore[union-attr]
            pdoc = parse_plan(canonical)
        job = self.store.get_job(job_id)
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
                        "size": w.size.value,
                        "files_in_scope": w.files_in_scope,
                        "depends_on": w.depends_on,
                    }
                    for w in pdoc.workers
                ],
            },
        )
        return []

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

    def replan(self, item_id: int) -> Job:
        """Send a held job back to the coord, with the failures in its packet."""
        with self.store.tx():
            item = self._offered(item_id, "replan")
            assert item.job_id is not None
            try:
                job = transition(
                    self.store, item.job_id, JobState.SPEC_PENDING, Actor.OWNER,
                    channel="inbox", evidence={"inbox_item_id": item_id, "action": "replan"},
                )  # fmt: skip
            except TransitionError as e:
                raise Conflict(str(e)) from e
            self.store.add_event(
                "replan",
                {"inbox_item_id": item_id, "tasks": item.body.get("tasks") or {}},
                Provenance.HUMAN,
                job_id=job.id,
            )
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
        self.last_run[job.id] = self.runner.on_resume(job.id)
        return job

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
            for t in self.store.list_tasks(job.id):
                if t.state not in ("pr_open", "reviewed"):
                    self.store.update_task(t.id, state="abandoned")
            self.store.add_event(
                "abandon", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=job.id
            )
            for other in self.store.list_inbox():
                # the job's own pages; a breaker's page stays for its reset
                # and for the other jobs it holds (2a round 6)
                if (other.job_id == job.id and other.queue == "questions"
                        and other.body.get("kind") != "breaker"):  # fmt: skip
                    self.store.resolve_inbox_item(other.id, Actor.OWNER.value)
            # a breaker that then holds nothing closes and resolves its page;
            # one that still holds a job keeps its page for it (2a round 7)
            for b in breakers.unhold(job.id):
                page = breakers.close_empty(b, by="abandon")
                if page is not None:
                    self.store.resolve_inbox_item(page, Actor.OWNER.value)
        failed = getattr(self.runner, "job_failed", None)
        if failed is not None:
            failed(job)  # an abandoned job counts toward the repo's breaker (item 4)
        return job

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
        with self.store.tx():
            item = self._question(item_id)
            self.store.add_event(
                "dismiss", {"inbox_item_id": item_id}, Provenance.HUMAN, job_id=item.job_id
            )
            self.store.resolve_inbox_item(item_id, Actor.OWNER.value)
