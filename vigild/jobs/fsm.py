"""The job state machine: the only place a job's state changes.

A stage advances only when its acceptance check (code, not judgment) holds.
Approval from the inbox, by the owner, is the only way out of
`spec_pending`. The one manual override is the owner's, from the inbox, and
it is logged; it never skips approval and never invents an edge.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vigild.db import Store
from vigild.jobs.model import Job, JobState, Role
from vigild.provenance import Provenance

S = JobState


class Actor(StrEnum):
    OWNER = "owner"  # you, through the inbox (admin token)
    VIGILD = "vigild"
    COORD = "coord"


class TransitionError(Exception):
    pass


@dataclass(frozen=True)
class Ctx:
    store: Store
    job: Job
    actor: Actor
    channel: str | None
    evidence: Mapping[str, Any]


Guard = Callable[[Ctx], list[str]]


def _approval(c: Ctx) -> list[str]:
    errs: list[str] = []
    if c.actor is not Actor.OWNER:
        errs.append("only the owner approves a spec")
    if c.channel != "inbox":
        errs.append("approval happens only from the inbox")
    for name in ("spec.md", "plan.md"):
        if c.store.get_artifact(c.job.id, name) is None:
            errs.append(f"{name} is missing")
    gate = c.store.get_artifact(c.job.id, "spec_gate.json")
    if gate is None or not json.loads(gate.content).get("ok"):
        errs.append("the spec gate has not passed")
    item_id = c.evidence.get("inbox_item_id")
    try:
        item = c.store.get_inbox_item(int(item_id)) if item_id is not None else None
    except KeyError:
        item = None
    if item is None or item.job_id != c.job.id or item.queue != "specs" or item.status != "open":
        errs.append("no open 'Specs to approve' inbox item for this job")
    return errs


def _none(c: Ctx) -> list[str]:
    return []


def _workers_routed(c: Ctx) -> list[str]:
    tasks = c.store.list_tasks(c.job.id)
    if not tasks:
        return ["no worker tasks have been created from plan.md"]
    return [
        f"worker task {t.id} is not routed"
        for t in tasks
        if not (t.tier_reason and t.backend_preset)
    ]


def _workers_done(c: Ctx) -> list[str]:
    errs: list[str] = []
    workers = [s for s in c.store.list_sessions(c.job.id) if s.role is Role.WORKER]
    tasks = [t for t in c.store.list_tasks(c.job.id) if t.state not in ("superseded", "abandoned")]
    if not tasks:
        errs.append("no worker tasks")
    for t in tasks:
        latest = [s for s in workers if s.task_id == t.id]
        if not latest or not latest[-1].pr_url or latest[-1].status != "pr_open":
            errs.append(f"worker {t.id} has no open PR")
        if c.store.get_artifact(c.job.id, "worker_report.json", t.id) is None:
            errs.append(f"worker {t.id} has no worker_report.json")
    return errs


def _findings(c: Ctx) -> list[str]:
    return [] if c.evidence.get("findings") else ["going back to building needs review findings"]


def _reviews_clean(c: Ctx) -> list[str]:
    """Stage 5 and 6 acceptance: every live task has a valid, clean review.json
    (an empty or invalid one fails), and where the review was sampled for
    audit, an audit.json of the same head that missed nothing medium or higher."""
    from pydantic import ValidationError

    from vigild.contract.models import BLOCKING, Audit, Review

    errs: list[str] = []
    for t in c.store.list_tasks(c.job.id):
        if t.state in ("superseded", "abandoned"):
            continue
        art = c.store.get_artifact(c.job.id, "review.json", t.id)
        if art is None:
            errs.append(f"worker {t.id} has no review.json")
            continue
        try:
            review = Review.model_validate_json(art.content)
        except ValidationError:
            errs.append(f"worker {t.id} review.json is invalid")
            continue
        if review.verdict != "clean" or review.blocking():
            errs.append(f"worker {t.id} review is not clean")
        if review.audit_required:
            au = c.store.get_artifact(c.job.id, "audit.json", t.id)
            try:
                audit = Audit.model_validate_json(au.content) if au else None
            except ValidationError:
                audit = None
            if audit is None or audit.head != review.head:
                errs.append(f"worker {t.id} has no audit.json for {review.head[:12]}")
            elif any(f.severity in BLOCKING for f in audit.missed()):
                errs.append(f"worker {t.id} audit found what the review missed")
    return errs


def _merged(c: Ctx) -> list[str]:
    return [] if c.evidence.get("merge_commits") else ["no merge commits recorded"]


def _merged_by_hand(c: Ctx) -> list[str]:
    """Review B7: the owner merged every PR of the job before (or instead of)
    vigild's review finishing; vigild's PR poll saw each one merged."""
    errs = _merged(c)
    live = [t for t in c.store.list_tasks(c.job.id) if t.state not in ("superseded", "abandoned")]
    if not live or any(t.state != "merged" for t in live):
        errs.append("not every PR of the job has merged")
    return errs


def _abandon(c: Ctx) -> list[str]:
    if c.actor is not Actor.OWNER or c.channel != "inbox":
        return ["only the owner, from the inbox, abandons a job"]
    return [] if c.evidence.get("action") == "abandon" else ["abandoning needs action=abandon"]


def _verified(c: Ctx) -> list[str]:
    art = c.store.get_artifact(c.job.id, "verify.json")
    if art is None or not json.loads(art.content).get("clean"):
        return ["no clean verify record"]
    return []


EDGES: dict[tuple[JobState, JobState], Guard] = {
    (S.SPEC_PENDING, S.APPROVED): _approval,
    (S.APPROVED, S.PLANNING): _none,
    (S.PLANNING, S.BUILDING): _workers_routed,
    (S.BUILDING, S.IN_REVIEW): _workers_done,
    (S.IN_REVIEW, S.BUILDING): _findings,
    (S.IN_REVIEW, S.READY): _reviews_clean,
    (S.READY, S.MERGED): _merged,
    (S.IN_REVIEW, S.MERGED): _merged_by_hand,
    (S.MERGED, S.VERIFYING): _none,
    (S.VERIFYING, S.DONE): _verified,
    (S.SPEC_PENDING, S.ABANDONED): _abandon,
}
HOLDS = (S.BLOCKED, S.ESCALATED)
TERMINAL = (S.DONE, S.ABANDONED)
# Out of a hold, besides resuming where it left (review round 2, N5): the
# owner sends a failed build back to the coord, or abandons the job.
FROM_HOLD = {S.SPEC_PENDING: "replan", S.ABANDONED: "abandon", S.MERGED: "prs_merged"}
# What vigild itself may do to a held job, each with its evidence: resume it
# once the deps job it waits on merged (round 3) or once a backend breaker's
# probe closed it (review B6), and record it merged once the owner merged
# every PR of it by hand (review B7). Everything else is the owner's.
VIGILD_HOLD_ACTIONS = ("deps_merged", "probe_closed", "prs_merged")
NEVER_FORCED = {(S.SPEC_PENDING, S.APPROVED)}


def transition(
    store: Store,
    job_id: str,
    to: JobState,
    actor: Actor,
    *,
    channel: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    force: bool = False,
) -> Job:
    evidence = dict(evidence or {})
    with store.lock:
        job = store.get_job(job_id)
        frm = job.state
        ctx = Ctx(store, job, actor, channel, evidence)
        owner_inbox = actor is Actor.OWNER and channel == "inbox"
        blocked_from: JobState | None = None

        if frm in TERMINAL:
            raise TransitionError(f"{job_id} is {frm.value}; no transition out of it")
        if to in HOLDS:
            if frm in HOLDS:
                raise TransitionError(f"no transition {frm.value} -> {to.value}")
            if not str(evidence.get("reason", "")).strip():
                raise TransitionError(f"{to.value} needs a reason")
            blocker = evidence.get("blocked_on")
            if blocker is not None:
                try:
                    store.get_job(str(blocker))
                except KeyError as e:
                    raise TransitionError(f"blocked_on names no job: {blocker}") from e
            blocked_from = frm
        elif frm in HOLDS:
            action = evidence.get("action")
            if to != job.blocked_from and (action is None or FROM_HOLD.get(to) != action):
                raise TransitionError(
                    f"no transition {frm.value} -> {to.value}; it can only resume "
                    f"{job.blocked_from.value if job.blocked_from else '?'}, re-plan or abandon"
                )
            vigild_may = actor is Actor.VIGILD and action in VIGILD_HOLD_ACTIONS
            if action == "deps_merged":
                vigild_may = vigild_may and job.blocked_on is not None and to == job.blocked_from
            elif action == "probe_closed":
                vigild_may = vigild_may and to == job.blocked_from
            elif action == "prs_merged":
                errors = _merged_by_hand(ctx)
                if errors:
                    raise TransitionError("; ".join(errors))
            if not owner_inbox and not vigild_may:
                raise TransitionError("only the owner, from the inbox, moves a held job")
            if to == job.blocked_from and job.blocked_on:
                # round 3: back to work only once the deps job it waits for has merged
                dep = store.get_job(job.blocked_on)
                if dep.state not in (S.MERGED, S.VERIFYING, S.DONE):
                    raise TransitionError(
                        f"waiting on {dep.id} (a deps job, {dep.state.value}); it must merge first"
                    )
        else:
            guard = EDGES.get((frm, to))
            if guard is None:
                raise TransitionError(f"no transition {frm.value} -> {to.value}")
            if force:
                if not owner_inbox:
                    raise TransitionError("override is the owner's, from the inbox, only")
                if (frm, to) in NEVER_FORCED:
                    raise TransitionError("approval is never an override; use the inbox approve")
            else:
                errors = guard(ctx)
                if errors:
                    raise TransitionError(
                        f"{frm.value} -> {to.value} refused: " + "; ".join(errors)
                    )

        store._write_state(job_id, to, blocked_from)
        if to in HOLDS:
            store._set_blocked_on(job_id, evidence.get("blocked_on"))
        elif frm in HOLDS:
            store._set_blocked_on(job_id, None)
        store.add_event(
            "state",
            {
                "from": frm.value,
                "to": to.value,
                "actor": actor.value,
                "channel": channel,
                "override": force,
                "evidence": evidence,
            },
            # "human" = not model output: you, or vigild's own code.
            Provenance.CLAUDE if actor is Actor.COORD else Provenance.HUMAN,
            job_id=job_id,
        )
        return store.get_job(job_id)
