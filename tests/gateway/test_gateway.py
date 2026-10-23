"""The model gateway (P1): containers hold a session token, never a provider key."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from vigild.budget import Budget
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.gateway.app import create_gateway_app
from vigild.gateway.service import Gateway, GatewayError
from vigild.jobs.model import Role

FAKE_KEY = "fake-provider-key-for-tests"
PA = "thomasbardhi01/parkagent"


class Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.stream = False
        self.status = 200

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx2.Response(
                self.status, json={"type": "error", "error": {"type": "overloaded_error"}}
            )
        if req.url.path.endswith("/count_tokens"):
            return httpx2.Response(200, json={"input_tokens": 12})
        body = json.loads(req.content)
        if body.get("stream"):
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "model": body["model"],
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 5000,
                        },
                    },
                },
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 300},
                },
                {"type": "message_stop"},
            ]
            data = b"".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events
            )
            return httpx2.Response(200, content=data, headers={"content-type": "text/event-stream"})
        return httpx2.Response(
            200,
            json={
                "model": body["model"],
                "content": [{"type": "text", "text": '{"answer": "S", "probability": 0.9}'}],
                "usage": {"input_tokens": 200, "output_tokens": 50},
            },
        )


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def budget(cfg: Config, store: Store) -> Budget:
    return Budget(store, cfg.budgets)


@pytest.fixture
def gw(cfg: Config, store: Store, budget: Budget, upstream: Upstream) -> Gateway:
    transport = httpx2.MockTransport(upstream)
    return Gateway(
        cfg,
        store,
        budget,
        Secrets({"ANTHROPIC_API_KEY": FAKE_KEY}),
        transport=transport,
        async_transport=transport,
    )


@pytest.fixture
def client(gw: Gateway) -> Iterator[TestClient]:
    with TestClient(create_gateway_app(gw)) as c:
        yield c


def _session(
    store: Store,
    budget: Budget,
    gw: Gateway,
    amount: float = 1.0,
    repo: str = PA,
    preset: str = "claude-sonnet",
) -> tuple[str, str]:
    s = store.create_session(None, Role.WORKER, backend_preset=preset)
    budget.reserve(session_id=s.id, task_id=f"t-{s.id}", amount=amount, purpose="worker")
    return s.id, gw.issue_token(s.id, repo, preset)


def _msg(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "claude-sonnet-5-5",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(kw)
    return body


def test_forwards_with_the_real_key_and_meters(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:
    sid, tok = _session(store, budget, gw)
    r = client.post(
        "/v1/messages",
        json=_msg(),
        headers={"authorization": f"Bearer {tok}", "anthropic-version": "2023-06-01"},
    )
    assert r.status_code == 200
    sent = upstream.requests[0]
    assert sent.headers["x-api-key"] == FAKE_KEY
    assert "authorization" not in sent.headers
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    row = store.q("SELECT * FROM model_calls")[0]
    assert (row["session_id"], row["tokens_in"], row["tokens_out"]) == (sid, 200, 50)
    assert row["cost_usd"] == pytest.approx(200 * 2e-6 + 50 * 1e-5)
    assert store.get_session(sid).cost_usd == pytest.approx(row["cost_usd"])
    assert FAKE_KEY not in r.text


def test_x_api_key_header_also_authenticates(
    client: TestClient, store: Store, budget: Budget, gw: Gateway
) -> None:
    _, tok = _session(store, budget, gw)
    assert client.post("/v1/messages", json=_msg(), headers={"x-api-key": tok}).status_code == 200


def test_streaming_is_proxied_and_metered(
    client: TestClient, store: Store, budget: Budget, gw: Gateway
) -> None:
    sid, tok = _session(store, budget, gw)
    with client.stream(
        "POST", "/v1/messages", json=_msg(stream=True), headers={"x-api-key": tok}
    ) as r:
        body = b"".join(r.iter_bytes())
    assert b"message_stop" in body
    row = store.q("SELECT * FROM model_calls WHERE session_id=?", (sid,))[0]
    assert (row["tokens_in"], row["tokens_out"], row["tokens_cache_read"]) == (1000, 300, 5000)


def test_missing_bad_or_revoked_token_is_401(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:
    assert client.post("/v1/messages", json=_msg()).status_code == 401
    assert (
        client.post("/v1/messages", json=_msg(), headers={"x-api-key": "nope"}).status_code == 401
    )
    sid, tok = _session(store, budget, gw)
    gw.revoke(sid)
    assert client.post("/v1/messages", json=_msg(), headers={"x-api-key": tok}).status_code == 401
    assert upstream.requests == []


def test_api_tokens_do_not_work_on_the_gateway(client: TestClient) -> None:
    # The gateway knows only session tokens.
    assert (
        client.post("/v1/messages", json=_msg(), headers={"x-api-key": FAKE_KEY}).status_code == 401
    )


def test_call_that_would_cross_the_reservation_is_refused_before_it_is_sent(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:
    sid, tok = _session(store, budget, gw, amount=0.05)
    r = client.post("/v1/messages", json=_msg(max_tokens=64000), headers={"x-api-key": tok})
    assert r.status_code == 403 and "budget" in r.text
    assert upstream.requests == []
    assert any(e.type == "gateway_refused" for e in store.events(session_id=sid))


def test_model_the_repo_does_not_allow_is_refused(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:
    _, tok = _session(store, budget, gw)
    r = client.post("/v1/messages", json=_msg(model="claude-mystery-9"), headers={"x-api-key": tok})
    assert r.status_code == 403 and upstream.requests == []


def test_private_repo_rule_on_the_wire(
    client: TestClient, cfg: Config, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:
    _, tok = _session(store, budget, gw)
    cfg.repos.repos[PA].backends = ["claude-opus"]  # policy changed after the token was issued
    r = client.post("/v1/messages", json=_msg(), headers={"x-api-key": tok})
    assert r.status_code == 403 and "does not list" in r.text
    assert upstream.requests == []


def test_only_messages_endpoints_exist(
    client: TestClient, store: Store, budget: Budget, gw: Gateway
) -> None:
    _, tok = _session(store, budget, gw)
    assert client.post("/v1/complete", json={}, headers={"x-api-key": tok}).status_code == 404
    assert client.get("/v1/models", headers={"x-api-key": tok}).status_code in (404, 405)


def test_count_tokens_is_forwarded_and_free(
    client: TestClient, store: Store, budget: Budget, gw: Gateway
) -> None:
    _, tok = _session(store, budget, gw)
    r = client.post("/v1/messages/count_tokens", json=_msg(), headers={"x-api-key": tok})
    assert r.status_code == 200 and r.json() == {"input_tokens": 12}
    assert store.q("SELECT COUNT(*) n FROM model_calls")[0]["n"] == 0


def test_upstream_error_passes_through(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:
    _, tok = _session(store, budget, gw)
    upstream.status = 529
    assert client.post("/v1/messages", json=_msg(), headers={"x-api-key": tok}).status_code == 529


def test_missing_provider_key_is_503(
    cfg: Config, store: Store, budget: Budget, upstream: Upstream
) -> None:
    t = httpx2.MockTransport(upstream)
    gw = Gateway(cfg, store, budget, Secrets({}), transport=t, async_transport=t)
    with TestClient(create_gateway_app(gw)) as c:
        _, tok = _session(store, budget, gw)
        assert c.post("/v1/messages", json=_msg(), headers={"x-api-key": tok}).status_code == 503


def test_internal_call_reserves_meters_and_settles(
    gw: Gateway, store: Store, budget: Budget, upstream: Upstream
) -> None:
    reply = gw.call(
        repo=PA,
        preset="claude-haiku",
        body={"max_tokens": 100, "messages": [{"role": "user", "content": "q"}]},
        purpose="decider",
        job_id=None,
    )
    assert reply["content"][0]["type"] == "text"
    assert json.loads(upstream.requests[0].content)["model"] == "claude-haiku-5-5"
    row = store.q("SELECT purpose, model FROM model_calls")[0]
    assert (row["purpose"], row["model"]) == ("decider", "claude-haiku-5-5")
    assert budget.open_today() == 0 and budget.spent_today() > 0


def test_internal_call_refuses_a_repo_route_not_listed(gw: Gateway, cfg: Config) -> None:
    cfg.repos.repos[PA].backends = ["claude-sonnet"]
    with pytest.raises(GatewayError):
        gw.call(
            repo=PA,
            preset="claude-haiku",
            body={"max_tokens": 10, "messages": []},
            purpose="decider",
            job_id=None,
        )


def test_internal_call_is_refused_when_the_day_is_spent(gw: Gateway, budget: Budget) -> None:
    budget.reserve(session_id="big", task_id="t1", amount=2.0, purpose="worker")
    budget.reserve(session_id="big2", task_id="t2", amount=2.0, purpose="worker")
    budget.reserve(session_id="big3", task_id="t3", amount=1.0, purpose="worker")
    with pytest.raises(GatewayError, match="daily"):
        gw.call(
            repo=PA,
            preset="claude-haiku",
            body={"max_tokens": 10, "messages": []},
            purpose="decider",
            job_id=None,
        )


# --- review round 1 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"mcp_servers": [{"type": "url", "url": "https://evil.example/mcp", "name": "x"}]},
        {"tools": [{"type": "web_fetch_20260209", "name": "web_fetch"}]},
        {"tools": [{"type": "web_search_20260209", "name": "web_search"}]},
        {"tools": [{"type": "code_execution_20260521", "name": "code_execution"}]},
        {"tools": [{"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5"}]},
        {"container": {"skills": []}},
        {"surprise_key": 1},
    ],
)
def test_server_tools_and_unknown_body_keys_are_refused(  # item 5
    client: TestClient,
    store: Store,
    budget: Budget,
    gw: Gateway,
    upstream: Upstream,
    extra: dict[str, Any],
) -> None:
    _, tok = _session(store, budget, gw)
    r = client.post("/v1/messages", json=_msg(**extra), headers={"x-api-key": tok})
    assert r.status_code == 403, r.text
    assert upstream.requests == []


def test_client_tools_are_allowed(
    client: TestClient, store: Store, budget: Budget, gw: Gateway
) -> None:  # item 5
    _, tok = _session(store, budget, gw)
    tools = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]
    assert (
        client.post("/v1/messages", json=_msg(tools=tools), headers={"x-api-key": tok}).status_code
        == 200
    )


def test_unknown_betas_are_refused(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:  # item 5
    _, tok = _session(store, budget, gw)
    for beta in ("mcp-client-2025-11-20", "code-execution-2025-08-25", "made-up-beta-2030-01-01"):
        r = client.post(
            "/v1/messages",
            json=_msg(),
            headers={"x-api-key": tok, "anthropic-beta": f"claude-code-20250219,{beta}"},
        )
        assert r.status_code == 403 and beta in r.text
    assert upstream.requests == []
    ok = client.post(
        "/v1/messages",
        json=_msg(),
        headers={"x-api-key": tok, "anthropic-beta": "claude-code-20250219"},
    )
    assert ok.status_code == 200


def test_in_flight_calls_hold_their_worst_case(
    gw: Gateway, store: Store, budget: Budget
) -> None:  # item 6
    _sid, tok = _session(store, budget, gw, amount=0.03)
    gs = gw.authorize(tok)
    body = json.dumps(_msg(max_tokens=2000)).encode()  # worst case ~ $0.02 on Sonnet
    hold = gw.preflight(gs, "/v1/messages", body)
    with pytest.raises(GatewayError, match="budget"):
        gw.preflight(gs, "/v1/messages", body)  # the first is still in flight
    gw.release(hold)
    gw.release(gw.preflight(gs, "/v1/messages", body))


def test_worst_case_prices_input_at_the_cache_write_rate(gw: Gateway) -> None:  # item 6
    raw = b"x" * 300_000  # ~100k tokens estimated
    worst = gw.worst_case("claude-sonnet-5-5", {"max_tokens": 1}, len(raw))
    assert worst == pytest.approx(100_000 * 4.00 / 1e6 + 10 / 1e6)  # 1-hour cache write rate


def test_tokens_expire(store: Store, budget: Budget, gw: Gateway) -> None:  # item 7
    s = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    tok = gw.issue_token(s.id, PA, "claude-sonnet", ttl_minutes=0)
    with pytest.raises(GatewayError, match="expired"):
        gw.authorize(tok)


def test_revoke_all_on_restart(store: Store, budget: Budget, gw: Gateway) -> None:  # item 7
    _, tok = _session(store, budget, gw)
    assert gw.revoke_all() == 1
    with pytest.raises(GatewayError):
        gw.authorize(tok)


def test_token_is_bound_to_the_sessions_model(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream
) -> None:  # item 9
    _, tok = _session(store, budget, gw)  # a claude-sonnet session
    r = client.post("/v1/messages", json=_msg(model="claude-opus-5-5"), headers={"x-api-key": tok})
    assert r.status_code == 403 and upstream.requests == []
    # the CLI's own small-model side calls (titles, summaries) are the one exception
    r = client.post("/v1/messages", json=_msg(model="claude-haiku-5-5"), headers={"x-api-key": tok})
    assert r.status_code == 200


# --- review round 2, N4: content blocks are allowlisted at every level -----------------------

IMG_URL = {"type": "image", "source": {"type": "url", "url": "https://evil.example/x.png"}}
IMG_B64 = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBO"}}
DOC_URL = {"type": "document", "source": {"type": "url", "url": "https://evil.example/x.pdf"}}
DOC_B64 = {
    "type": "document",
    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"},
}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}


def _result(*content: Any) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": list(content)}


def _turns(user_blocks: list[Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}, TOOL_USE]},
        {"role": "user", "content": user_blocks},
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"messages": _turns([_result(IMG_URL)])},  # the owner's case: nested in a tool_result
        {"messages": _turns([_result({"type": "text", "text": "x"}, DOC_URL)])},
        {"messages": [{"role": "user", "content": [IMG_URL]}]},
        {"messages": [{"role": "user", "content": [DOC_URL]}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "file", "file_id": "f"}}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}
                    ],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "web_search_tool_result", "tool_use_id": "s", "content": []}
                    ],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "mcp_tool_use",
                            "id": "m",
                            "name": "x",
                            "server_name": "s",
                            "input": {},
                        }
                    ],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "mcp_tool_result", "tool_use_id": "m", "content": []}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "search_result",
                            "source": "https://x",
                            "title": "t",
                            "content": [],
                        }
                    ],
                }
            ]
        },
        {"messages": [{"role": "user", "content": [{"type": "container_upload", "file_id": "f"}]}]},
        {"messages": [{"role": "user", "content": [{"text": "no type"}]}]},
        {
            "messages": [
                {"role": "user", "content": [_result(_result({"type": "text", "text": "x"}))]}
            ]
        },
        {"system": [{"type": "text", "text": "s"}, IMG_URL]},
        {"system": [{"type": "image", "source": IMG_B64["source"]}]},
        {"messages": "not a list"},
    ],
)
def test_content_blocks_outside_the_allowlist_are_refused(
    client: TestClient,
    store: Store,
    budget: Budget,
    gw: Gateway,
    upstream: Upstream,
    body: dict[str, Any],
) -> None:
    _, tok = _session(store, budget, gw)
    r = client.post("/v1/messages", json=_msg(**body), headers={"x-api-key": tok})
    assert r.status_code in (400, 403), r.text
    assert upstream.requests == []


def test_allowed_content_blocks_pass(
    client: TestClient, store: Store, budget: Budget, gw: Gateway
) -> None:
    _, tok = _session(store, budget, gw)
    body = _msg(
        system=[{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "look"}, IMG_B64, DOC_B64]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hm", "signature": "sig"},
                    {"type": "redacted_thinking", "data": "abc"},
                    TOOL_USE,
                ],
            },
            {
                "role": "user",
                "content": [
                    _result({"type": "text", "text": "out"}, IMG_B64),
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "plain"},
                ],
            },
        ],
    )
    assert client.post("/v1/messages", json=body, headers={"x-api-key": tok}).status_code == 200


# --- review round 2, N7 -----------------------------------------------------------------------


@pytest.mark.parametrize("max_tokens", [0, -5, "many", 1.5, True])
def test_max_tokens_must_be_a_positive_integer(
    client: TestClient,
    store: Store,
    budget: Budget,
    gw: Gateway,
    upstream: Upstream,
    max_tokens: Any,
) -> None:
    _, tok = _session(store, budget, gw)
    r = client.post("/v1/messages", json=_msg(max_tokens=max_tokens), headers={"x-api-key": tok})
    assert r.status_code == 400, r.text
    assert upstream.requests == []


@pytest.mark.parametrize("stream", [False, True])
def test_the_call_is_booked_before_its_hold_is_released(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, stream: bool
) -> None:
    """Between release and record, a concurrent call would see the session's
    spend without this call and its hold both: record first."""
    sid, tok = _session(store, budget, gw)
    seen: list[int] = []
    real = gw.release

    def release(hold: Any) -> None:
        seen.append(len(store.q("SELECT 1 FROM model_calls WHERE session_id=?", (sid,))))
        real(hold)

    gw.release = release  # type: ignore[method-assign]
    if stream:
        with client.stream(
            "POST", "/v1/messages", json=_msg(stream=True), headers={"x-api-key": tok}
        ) as r:
            b"".join(r.iter_bytes())
    else:
        client.post("/v1/messages", json=_msg(), headers={"x-api-key": tok})
    assert seen and seen[0] == 1


# --- owner decision 2026-10-22 (round 3): thinking blocks, in assistant turns, exact fields ---


@pytest.mark.parametrize(
    "block",
    [
        {"type": "thinking", "thinking": "hm", "signature": "s", "source": {"type": "url"}},
        {
            "type": "thinking",
            "thinking": "hm",
            "signature": "s",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "redacted_thinking", "data": "abc", "extra": 1},
        {"type": "redacted_thinking", "data": "abc", "thinking": "x"},
    ],
)
def test_thinking_blocks_carry_only_their_own_fields(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream, block: Any
) -> None:
    _, tok = _session(store, budget, gw)
    body = _msg(
        messages=[{"role": "user", "content": "go"}, {"role": "assistant", "content": [block]}]
    )
    r = client.post("/v1/messages", json=body, headers={"x-api-key": tok})
    assert r.status_code == 403, r.text
    assert upstream.requests == []


@pytest.mark.parametrize(
    "block",
    [
        {"type": "thinking", "thinking": "hm", "signature": "s"},
        {"type": "redacted_thinking", "data": "abc"},
    ],
)
def test_thinking_blocks_in_a_user_turn_are_refused(
    client: TestClient, store: Store, budget: Budget, gw: Gateway, upstream: Upstream, block: Any
) -> None:
    _, tok = _session(store, budget, gw)
    r = client.post(
        "/v1/messages",
        json=_msg(messages=[{"role": "user", "content": [block]}]),
        headers={"x-api-key": tok},
    )
    assert r.status_code == 403 and upstream.requests == []


def test_settling_waits_for_the_sessions_calls_in_flight(
    gw: Gateway, store: Store, budget: Budget
) -> None:
    """2a round 2 (found by the done-when): a killed worker's last streamed
    call was booked after its reservation settled, so the ledger missed it
    and its resume was given a little more than its cap's remainder.
    `drain` waits for the session's holds before vigild settles."""
    import threading
    import time

    from vigild.gateway.service import Hold

    sid, _tok = _session(store, budget, gw)
    hold = Hold(sid, gw._hold(sid, 0.01), gw.cfg.preset("claude-sonnet"), {})
    assert gw.drain(sid, timeout=0.1) is False  # a call still in flight
    threading.Timer(0.2, gw.release, args=(hold,)).start()
    t = time.monotonic()
    assert gw.drain(sid, timeout=5) is True
    assert 0.1 < time.monotonic() - t < 5
    assert gw.drain(sid, timeout=0.01) is True  # nothing in flight


# --- 2a round 8, R4(b): a metered call reserves its worst case and never settles past it -------


def test_a_metered_call_reserves_its_worst_case(gw: Gateway, store: Store, cfg: Config) -> None:
    amounts: list[float] = []
    real = gw.budget.reserve

    def spy(**kw: Any) -> int:
        amounts.append(float(kw["amount"]))
        return real(**kw)

    gw.budget.reserve = spy  # type: ignore[method-assign]
    payload = {"result": {"usage": {"input_tokens": 400, "output_tokens": 10}}}
    gw.metered(repo=PA, route="jev-openrouter", provider="openrouter", model="typesafe/jev-1.13",
               purpose="decider", job_id=None, run=lambda: payload, private=False,
               body_chars=3_000_000)  # fmt: skip
    assert amounts and amounts[0] > cfg.budgets.decider_call_usd  # sized from the body


def test_a_metered_call_costing_more_than_reserved_trips_the_daily_breaker(
    gw: Gateway, store: Store, cfg: Config
) -> None:
    from vigild.breakers import GLOBAL, Breakers, Kind

    gw.breakers = Breakers(store, cfg)
    payload = {"result": {"usage": {"input_tokens": 10_000_000, "output_tokens": 0}}}  # $0.42
    gw.metered(repo=PA, route="jev-openrouter", provider="openrouter", model="typesafe/jev-1.13",
               purpose="decider", job_id=None, run=lambda: payload, private=False,
               body_chars=30)  # fmt: skip
    assert gw.budget.spent_today() <= cfg.budgets.decider_call_usd + 1e-9  # booked: the reservation
    assert gw.breakers.is_open(Kind.DAILY, GLOBAL)
    assert [e for e in store.events() if e.type == "settle_excess"]
