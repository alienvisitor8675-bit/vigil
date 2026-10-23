"""The SDK features phase 2a builds on, in the real executor image through
vigild's real gateway (pytest -m "docker and live"; Haiku, a few cents):

- a reviewer's answer as structured output, with read-only tools (item 1);
- the transcript mirrored out as it's written, and a later session that
  forks it and remembers what the first one said (items 2-3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.security.test_guard_in_container import vigild as vigild
from vigild.contract.models import ReviewOutput
from vigild.executors.base import ExecOutcome, Resume, SessionSpec
from vigild.executors.runtime import DockerRuntime
from vigild.jobs.model import Role
from vigild.server import Vigild

pytestmark = [
    pytest.mark.docker,
    pytest.mark.live,
    pytest.mark.parametrize("executor_image", ["generic"], indirect=True),
]


def _run(v: Vigild, runtime: DockerRuntime, spec: SessionSpec) -> tuple[ExecOutcome, list[Any]]:
    sess = v.store.create_session(None, spec.role, backend_preset="claude-haiku")
    spec.session_id = sess.id
    v.budget.reserve(session_id=sess.id, task_id=None, amount=0.10, purpose="sdk-probe")
    token = v.gateway.issue_token(sess.id, "thomasbardhi01/vigil", "claude-haiku")
    events: list[dict[str, Any]] = []
    try:
        out = runtime.run(spec, token, events.append)
    finally:
        v.gateway.revoke(sess.id)
        v.budget.settle_session(sess.id)
    for e in v.store.events(session_id=sess.id):
        if e.type == "gateway_refused":
            print("gateway refused:", e.data)
    spend = sum(
        r["cost_usd"]
        for r in v.store.q("SELECT cost_usd FROM model_calls WHERE session_id=?", (sess.id,))
    )
    print(f"{spec.role.value}: ${spend:.5f}; {json.dumps(out.result)[:600]}; err={out.error}")
    for e in events:
        if e["type"] == "stop_hook":
            print("stop_hook:", json.dumps(e["data"])[:600])
    return out, events


def test_a_reviewer_answers_as_structured_output(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec], vigild: Vigild
) -> None:
    spec = make_spec(Role.REVIEWER)
    spec.max_turns = 4
    spec.output_schema = ReviewOutput.model_json_schema()
    spec.packet = (
        "This is a test of the review output format. Do not use any tool. Answer with "
        "verdict `clean`, no findings, and every checklist item true."
    )
    out, _ = _run(vigild, runtime, spec)
    assert out.status == "done", out
    review = ReviewOutput.model_validate(out.result.get("structured_output"))
    assert review.verdict == "clean"


def test_a_session_forks_the_transcript_vigild_kept(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec], vigild: Vigild
) -> None:
    first = make_spec(Role.WORKER)
    first.max_turns = 2
    first.packet = "Reply with exactly the word PERIWINKLE and stop. Use no tool."
    out, events = _run(vigild, runtime, first)
    assert out.status == "done", out
    lines = [e["data"] for e in events if e["type"] == "transcript"]
    assert lines, "no transcript was mirrored"
    sdk = lines[-1]["session_id"]
    entries = [x for d in lines if d["session_id"] == sdk and not d.get("subpath")
               for x in d["entries"]]  # fmt: skip
    assert "PERIWINKLE" in json.dumps(entries)

    second = make_spec(Role.WORKER)
    second.max_turns = 2
    second.resume = Resume(session_id=sdk, entries=entries, fork=True)
    second.packet = "What single word did you reply with earlier? Reply with only that word."
    out2, events2 = _run(vigild, runtime, second)
    assert out2.status == "done", out2
    assert "PERIWINKLE" in str(out2.result.get("result_text", "")).upper()
    forked = {e["data"]["session_id"] for e in events2 if e["type"] == "transcript"}
    assert forked and sdk not in forked  # a fork writes under its own session id


def test_a_stop_hook_block_shows_up_in_the_transcript(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec], vigild: Vigild
) -> None:
    """Plan 2a, item 6: a worker that tries to stop without its report is sent
    back, with the hook's message in its transcript; the cap lets it go."""
    spec = make_spec(Role.WORKER)
    spec.max_turns = 8
    spec.packet = (
        "This is a test of a stop check. Do not use any tool. Reply with the single word DONE "
        "and stop. If you are told you can't stop yet, reply DONE again and stop."
    )
    out, events = _run(vigild, runtime, spec)
    assert out.status == "done", out
    hooks = [e["data"] for e in events if e["type"] == "stop_hook"]
    assert any(h["blocked"] for h in hooks), events
    entries = [x for e in events if e["type"] == "transcript" for x in e["data"]["entries"]]
    assert "vigil stop hook" in json.dumps(entries)
    assert out.result.get("stop_hook_blocks", 0) >= 1
