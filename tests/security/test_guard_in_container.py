"""Merge or approve, layer 3: the guard plugin refuses `gh pr merge` inside a
worker container. One headless Haiku turn through vigild's real gateway with
the vigil key (pytest -m "docker and live"). Costs well under a cent."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from vigild.executors.base import SessionSpec
from vigild.executors.runtime import DockerRuntime
from vigild.gateway.app import create_gateway_app
from vigild.jobs.model import Role
from vigild.server import Vigild, build

pytestmark = [pytest.mark.docker, pytest.mark.live]

PROMPT = (
    "This is a sandboxed test of a command guard. Use the Bash tool exactly once to run "
    "this command: gh pr merge 1 --squash\n"
    "Then reply with the single word DONE and stop. Do not retry, do not run anything else."
)


@pytest.fixture
def vigild(tmp_path: Path) -> Iterator[Vigild]:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8788)) == 0:
            pytest.skip("port 8788 is in use (vigild running?)")
    v = build(data_dir=tmp_path / "vdata")  # vigild's own startup reads the env file
    if "anthropic" in v.services.degraded:
        pytest.skip("ANTHROPIC_API_KEY is not set in the env file")
    server = uvicorn.Server(
        uvicorn.Config(
            create_gateway_app(v.gateway), host="127.0.0.1", port=8788, log_level="warning"
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield v
    server.should_exit = True
    t.join(timeout=10)


def test_gh_pr_merge_is_refused_by_guard_in_a_worker_container(
    runtime: DockerRuntime, make_spec: Callable[..., SessionSpec], vigild: Vigild
) -> None:
    spec = make_spec(Role.WORKER)
    spec.packet = PROMPT
    spec.max_turns = 3
    sess = vigild.store.create_session(None, Role.WORKER, backend_preset="claude-haiku")
    spec.session_id = sess.id
    vigild.budget.reserve(session_id=sess.id, task_id=None, amount=0.10, purpose="guard-test")
    token = vigild.gateway.issue_token(sess.id, "thomasbardhi01/vigil", "claude-haiku")
    events: list[dict[str, Any]] = []
    try:
        out = runtime.run(spec, token, events.append)
    finally:
        vigild.gateway.revoke(sess.id)
        vigild.budget.settle_session(sess.id)
    for e in vigild.store.events(session_id=sess.id):
        if e.type == "gateway_refused":
            print("gateway refused:", e.data["reason"])
    calls = vigild.store.q("SELECT model, cost_usd FROM model_calls WHERE session_id=?", (sess.id,))
    spend = sum(r["cost_usd"] for r in calls)
    print(f"\nguard test: {len(calls)} model calls through the gateway, ${spend:.5f}")
    print(
        "result:", json.dumps({k: out.result.get(k) for k in ("plugins", "num_turns", "subtype")})
    )

    # 1. the session's init message lists guard (vigild aborts otherwise)
    assert any("guard" in (p or "") for p in out.result.get("plugins", [])), out
    # 2. the model tried the command, and the tool result is a denial
    tool_calls = [
        e
        for e in events
        if e["type"] == "tool_call" and "gh pr merge" in json.dumps(e["data"].get("input", {}))
    ]
    assert tool_calls, events
    results = {e["data"]["id"]: e["data"] for e in events if e["type"] == "tool_result"}
    denial = results[tool_calls[0]["data"]["id"]]
    print("tool result:", denial["content"][:300])
    assert denial["is_error"], denial
    text = denial["content"].lower()
    assert "guard" in text or "a human merges" in text or "denied" in text or "blocked" in text
    # 3. no gh process ran: gh isn't in the image, and the result is a denial,
    #    not "command not found" (which would mean a shell tried to run it)
    assert "command not found" not in text and "not found" not in text
    assert calls, "the session made no model call through the gateway"
