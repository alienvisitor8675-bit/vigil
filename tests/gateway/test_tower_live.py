"""One tiny chat turn through vigild's real gateway to the tower.

Deselected by default (marks `live` and `tower`); coord or the owner runs it with
the tower on the tailnet and TOWER_API_KEY in the env file. Free: the tower costs
no per-token money. Skips cleanly without the key."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vigild.gateway.app import create_gateway_app
from vigild.jobs.model import Role
from vigild.server import build

pytestmark = [pytest.mark.live, pytest.mark.tower]

REPO = "thomasbardhi01/vigil"
PRESET = "tower-qwen"


def test_one_turn_through_the_gateway_to_tower_qwen(tmp_path: Path) -> None:
    v = build(data_dir=tmp_path / "vdata")  # vigild's own startup reads the env file
    if not v.secrets.get("TOWER_API_KEY"):
        pytest.skip("TOWER_API_KEY is not set in the env file")
    v.cfg.repos.repos[REPO].backends.append(PRESET)  # in memory only
    sess = v.store.create_session(None, Role.WORKER, backend_preset=PRESET)
    v.budget.reserve(session_id=sess.id, task_id=None, amount=0.10, purpose="tower-smoke")
    token = v.gateway.issue_token(sess.id, REPO, PRESET)
    try:
        with TestClient(create_gateway_app(v.gateway)) as client:
            r = client.post(
                "/v1/messages",
                json={
                    "model": v.cfg.preset(PRESET).model,
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                },
                headers={"x-api-key": token},
                timeout=120,
            )
    finally:
        v.gateway.revoke(sess.id)
        v.budget.settle_session(sess.id)
    assert r.status_code == 200, r.text[:300]
    answer = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    assert answer.strip()
    (row,) = v.store.q("SELECT * FROM model_calls WHERE session_id=?", (sess.id,))
    assert (row["provider"], row["backend_preset"], row["cost_usd"]) == ("tower", PRESET, 0)
