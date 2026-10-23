"""The executor boundary: a session spec in, an outcome out. The task
contract travels inside the clone (.vigil/task.json) and the packet; nothing
else crosses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vigild.jobs.model import Role
from vigild.workspace import Mount


@dataclass
class Resume:
    """A session that picks up an earlier one's transcript (phase 2a): the
    SDK resumes `session_id` from `entries` (the transcript vigild stored),
    forked into a new session id when `fork`."""

    session_id: str
    entries: list[dict[str, Any]]
    fork: bool = True
    from_session: str | None = None  # the vigil session it continues


@dataclass
class SessionSpec:
    session_id: str
    role: Role
    job_id: str
    task_id: str | None
    repo: str
    preset: str
    model: str
    clone: Path
    session_dir: Path
    max_turns: int
    max_minutes: int
    max_budget_usd: float
    mounts: list[Mount] = field(default_factory=list)
    traceparent: str | None = None
    packet: str = ""
    image: str | None = None  # the repo's pinned dev image; None means the default executor
    # A reviewer's or auditor's answer, as JSON schema: the harness asks the
    # SDK for structured output and puts it on the result line (phase 2a).
    output_schema: dict[str, Any] | None = None
    resume: Resume | None = None  # its transcript, resumed or forked (items 2-3)
    # A cap on each model call's output (the CLI's CLAUDE_CODE_MAX_OUTPUT_TOKENS).
    # The gateway holds every call's worst case against the reservation, so a
    # review's small answers keep its reservation small.
    max_output_tokens: int | None = None


@dataclass
class ExecOutcome:
    status: str  # done | failed | timeout | killed
    exit_code: int | None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


EventSink = Callable[[dict[str, Any]], None]


class Executor(Protocol):
    def run(self, spec: SessionSpec, gateway_token: str, on_event: EventSink) -> ExecOutcome: ...
