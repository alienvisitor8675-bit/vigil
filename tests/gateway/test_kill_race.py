"""#60: a call that passed `authorize` before a kill's revoke may reach its hold after the
kill's drain and settle. It must be refused there, and open no reservation on the
settled session (the over-cap path used to, for a reviewer or auditor)."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json

import pytest

from tests.gateway.test_gateway import PA, Upstream, budget, gw, upstream  # noqa: F401
from vigild.budget import Budget
from vigild.db import Store
from vigild.gateway.service import Gateway, GatewayError
from vigild.jobs.model import Role

MODEL = "claude-sonnet-5-5"


def _big_call(gw: Gateway, usd: float) -> bytes:
    n = 1000
    while True:
        raw = json.dumps(
            {
                "model": MODEL,
                "max_tokens": 16384,
                "messages": [{"role": "user", "content": "x" * n}],
            }
        ).encode()
        if gw.worst_case(MODEL, {"max_tokens": 16384}, len(raw)) >= usd:
            return raw
        n += 1000


def _open_for(store: Store, sid: str) -> list[int]:
    rows = store.q("SELECT id FROM reservations WHERE session_id=? AND status='open'", (sid,))
    return [int(r["id"]) for r in rows]


def test_a_call_authorized_before_the_revoke_cannot_hold_after_the_drain(
    gw: Gateway, store: Store, budget: Budget
) -> None:
    s = store.create_session(None, Role.REVIEWER, backend_preset="claude-sonnet")
    budget.reserve(session_id=s.id, task_id=None, amount=0.40, purpose="reviewer")
    token = gw.issue_token(s.id, PA, "claude-sonnet")
    gs = gw.authorize(token)  # the call is past authorization ...
    gw.revoke(s.id)  # ... the kill revokes, drains and settles ...
    assert gw.drain(s.id, 0.1)
    budget.settle_session(s.id)
    assert _open_for(store, s.id) == []
    with pytest.raises(GatewayError) as e:  # ... and the call reaches its hold only now
        gw.preflight(gs, "/v1/messages", _big_call(gw, 0.60))  # over the $0.40 cap
    assert e.value.status == 401
    assert _open_for(store, s.id) == [], "no new reservation on a settled session"
    assert budget.open_today() == 0


def test_a_hold_of_a_session_whose_token_is_revoked_is_refused(
    gw: Gateway, store: Store, budget: Budget
) -> None:
    s = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    budget.reserve(session_id=s.id, task_id="t1", amount=1.0, purpose="worker")
    token = gw.issue_token(s.id, PA, "claude-sonnet")
    gs = gw.authorize(token)
    gw.revoke(s.id)
    raw = json.dumps({"model": MODEL, "max_tokens": 100, "messages": []}).encode()
    with pytest.raises(GatewayError) as e:
        gw.preflight(gs, "/v1/messages", raw)
    assert e.value.status == 401
    assert not gw._inflight, "nothing is held in flight for a revoked session"
