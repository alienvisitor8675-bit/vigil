"""The `X-Vigil-Channel` header (#55): vigild records where a call came from, from a fixed
allowlist; any other value is refused, and nothing else is trusted."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import RecordingRunner, auth
from vigild.db import Store
from vigild.jobs.model import JobState
from vigild.services import Tokens

H = "X-Vigil-Channel"
REQ: dict[str, Any] = {
    "repo": "thomasbardhi01/parkagent", "title": "T", "request": "do it", "why": "w",
    "value": 3, "risk": 1,
}  # fmt: skip


def _calls(store: Store, job_id: str | None = None) -> list[dict[str, Any]]:
    return [e.data for e in store.events(job_id=job_id) if e.type == "mcp_call"]


def test_an_unknown_channel_is_refused(client: TestClient, tokens: Tokens, store: Store) -> None:
    for bad in ("web", "MCP ", "mcp, api", "inbox", ""):
        r = client.get("/api/inbox", headers={**auth(tokens.read), H: bad})
        assert r.status_code == 422, bad
    assert _calls(store) == []


def test_no_header_is_the_api_and_records_nothing(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    assert client.get("/api/inbox", headers=auth(tokens.read)).status_code == 200
    for ch in ("api", "cli"):
        h = {**auth(tokens.read), H: ch}
        assert client.get("/api/inbox", headers=h).status_code == 200
    assert _calls(store) == []


def test_an_mcp_read_logs_one_small_event(client: TestClient, tokens: Tokens, store: Store) -> None:
    r = client.get("/api/inbox", headers={**auth(tokens.read), H: "mcp"})
    assert r.status_code == 200 and r.json() == {"specs": [], "ready": [], "questions": []}
    [call] = _calls(store)
    assert call == {"channel": "mcp", "method": "GET", "route": "/api/inbox", "status": 200}


def test_an_mcp_submit_logs_the_event_on_the_new_job(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    r = client.post("/api/jobs", json=REQ, headers={**auth(tokens.admin), H: "mcp"})
    assert r.status_code == 202
    job_id = r.json()["id"]
    [call] = _calls(store, job_id)
    assert call["channel"] == "mcp" and call["route"] == "/api/jobs" and call["status"] == 202
    assert "request" not in call and "title" not in call  # no body text in the event


def test_a_refused_call_is_logged_with_its_status_but_a_bad_token_is_not(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    r = client.post("/api/jobs", json={"repo": "x"}, headers={**auth(tokens.admin), H: "mcp"})
    assert r.status_code == 422
    assert [c["status"] for c in _calls(store)] == [422]
    r = client.get("/api/inbox", headers={**auth("wrong"), H: "mcp"})
    assert r.status_code == 401
    assert len(_calls(store)) == 1


def test_kill_records_mcp_as_the_channel_in_the_transition(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:
    job_id = client.post("/api/jobs", json=REQ, headers=auth(tokens.admin)).json()["id"]
    r = client.post(
        f"/api/jobs/{job_id}/kill",
        json={"channel": "mcp"},
        headers={**auth(tokens.admin), H: "mcp"},
    )
    assert r.status_code == 200, r.text
    assert store.get_job(job_id).state is JobState.ABANDONED
    states = [e.data for e in store.events(job_id=job_id) if e.type == "state"]
    assert states[-1]["channel"] == "mcp" and states[-1]["to"] == "abandoned"


def test_a_kill_body_channel_outside_the_allowlist_is_still_refused(
    client: TestClient, tokens: Tokens
) -> None:
    job_id = client.post("/api/jobs", json=REQ, headers=auth(tokens.admin)).json()["id"]
    r = client.post(f"/api/jobs/{job_id}/kill", json={"channel": "web"}, headers=auth(tokens.admin))
    assert r.status_code == 422


def test_inbox_routes_log_the_item_by_its_route_template(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    job_id = client.post("/api/jobs", json=REQ, headers=auth(tokens.admin)).json()["id"]
    item = store.create_inbox_item("specs", "spec", job_id, "spec", {})
    r = client.post(
        f"/api/inbox/{item.id}/abandon", json={}, headers={**auth(tokens.admin), H: "mcp"}
    )
    assert r.status_code == 409  # a spec item doesn't offer abandon; the refusal is logged too
    [call] = _calls(store)
    assert call["route"] == "/api/inbox/{item_id}/abandon" and call["item_id"] == item.id
    assert call["status"] == 409


def test_a_tokenless_mcp_request_writes_no_event(client: TestClient, store: Store) -> None:
    junk = "/api/" + "x" * 5000
    for method, path in (("GET", junk), ("GET", "/api/health"), ("POST", "/api/login"),
                         ("GET", "/api/nope")):  # fmt: skip
        r = client.request(method, path, headers={H: "mcp"}, json={} if method == "POST" else None)
        assert r.status_code in (401, 404), (path, r.status_code)
    assert _calls(store) == []


def test_an_authed_mcp_request_to_an_unmatched_path_writes_no_event_and_no_raw_path(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    r = client.get("/api/" + "y" * 5000, headers={**auth(tokens.admin), H: "mcp"})
    assert r.status_code == 404
    assert _calls(store) == []
    assert all("yyyy" not in str(e.data) for e in store.events())


def test_repeated_response_headers_survive_the_mcp_logging(
    client: TestClient, tokens: Tokens
) -> None:
    from fastapi.responses import JSONResponse

    from vigild.api.app import READ

    @client.app.get("/api/_two_cookies", dependencies=[READ])  # type: ignore[attr-defined]
    def two() -> JSONResponse:
        r = JSONResponse({"ok": True})
        r.headers.append("set-cookie", "a=1")
        r.headers.append("set-cookie", "b=2")
        return r

    r = client.get("/api/_two_cookies", headers={**auth(tokens.read), H: "mcp"})
    assert r.status_code == 200, r.text
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]
