"""Jobs, roles and sessions: a job is a tree, not a session."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_FIX_DEPTH = 3  # circuit breaker: a fix job of a fix job of a fix job, no deeper


class JobState(StrEnum):
    SPEC_PENDING = "spec_pending"
    APPROVED = "approved"
    PLANNING = "planning"
    BUILDING = "building"
    IN_REVIEW = "in_review"
    READY = "ready"
    MERGED = "merged"
    VERIFYING = "verifying"
    DONE = "done"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"  # the owner gave up on it, from the inbox (round 2, N5)


class Role(StrEnum):
    COORD = "coord"
    WORKER = "worker"
    REVIEWER = "reviewer"
    AUDITOR = "auditor"
    SUBAGENT = "subagent"
    HIDDEN_TEST_AUTHOR = "hidden_test_author"  # a fresh session; never a worker or reviewer
    DEPS = "deps"  # a deps job's build container: no model, registry egress only (B39)


class FixDepthExceeded(ValueError):
    pass


@dataclass
class Job:
    id: str
    repo: str
    base: str
    title: str
    request: str
    notes: list[str]
    source: str
    state: JobState
    blocked_from: JobState | None
    parent_job_id: str | None
    fix_depth: int
    coord_session_id: str | None
    created_at: str
    updated_at: str
    provenance: str
    blocked_on: str | None = None  # a deps job this one waits for (round 3)
    issue: int | None = None  # the GitHub issue in this repo it answers, if any
    batch_id: str | None = None  # the batch it was submitted in, if any (#54)


@dataclass
class TaskRow:
    id: str
    job_id: str | None
    contract: dict[str, Any]
    state: str
    tier: str | None
    tier_reason: str | None
    backend_preset: str | None
    created_at: str
    provenance: str


@dataclass
class SessionRow:
    id: str
    job_id: str | None
    task_id: str | None
    role: Role
    parent_session_id: str | None
    backend_preset: str | None
    status: str
    container_id: str | None
    branch: str | None
    pr_url: str | None
    clone_path: str | None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    started_at: str | None
    ended_at: str | None
    created_at: str
    provenance: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventRow:
    id: int
    job_id: str | None
    session_id: str | None
    task_id: str | None
    ts: str
    type: str
    data: dict[str, Any]
    provenance: str


@dataclass
class ArtifactRow:
    id: int
    job_id: str
    task_id: str | None
    name: str
    content: str
    sha256: str
    created_at: str
    provenance: str


@dataclass
class InboxItem:
    id: int
    queue: str
    kind: str
    job_id: str | None
    title: str
    body: dict[str, Any]
    status: str
    created_at: str
    resolved_at: str | None
    resolved_by: str | None
    provenance: str
