"""P7 is NO: the web UI never stores a token. A one-time token login sets an
HttpOnly, SameSite=Strict session cookie; the CLI and MCP keep bearer tokens."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from tests.conftest import auth
from vigild.api import auth as auth_mod
from vigild.services import Tokens


def _login(client: TestClient, token: str | None) -> object:
    return client.post("/api/login", json={"token": token})


def test_login_sets_an_httponly_strict_cookie(ui_client: TestClient, tokens: Tokens) -> None:
    r = _login(ui_client, tokens.admin)
    assert r.status_code == 204  # type: ignore[attr-defined]
    cookie = r.headers["set-cookie"]  # type: ignore[attr-defined]
    assert "vigil_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert "Max-Age=43200" in cookie and "Path=/" in cookie
    assert tokens.admin not in cookie  # type: ignore[operator]


def test_cookie_authenticates_reads(ui_client: TestClient, tokens: Tokens) -> None:
    _login(ui_client, tokens.admin)
    assert ui_client.get("/api/inbox").status_code == 200


def test_cookie_writes_need_the_ui_header(ui_client: TestClient, tokens: Tokens) -> None:
    _login(ui_client, tokens.admin)
    body = {
        "repo": "thomasbardhi01/parkagent",
        "title": "t",
        "request": "r",
        "why": "w",
        "value": 3,
        "risk": 1,
    }
    assert ui_client.post("/api/jobs", json=body).status_code == 403
    assert ui_client.post("/api/jobs", json=body, headers={"X-Vigil-UI": "1"}).status_code == 202


def test_wrong_token_gets_no_cookie(ui_client: TestClient) -> None:
    r = _login(ui_client, "nope")
    assert r.status_code == 401  # type: ignore[attr-defined]
    assert "set-cookie" not in r.headers  # type: ignore[attr-defined]


def test_heartbeat_token_cannot_log_in(ui_client: TestClient, tokens: Tokens) -> None:
    assert _login(ui_client, tokens.heartbeat).status_code == 401  # type: ignore[attr-defined]


def test_read_token_login_is_read_only(ui_client: TestClient, tokens: Tokens) -> None:
    _login(ui_client, tokens.read)
    assert ui_client.get("/api/inbox").status_code == 200
    r = ui_client.post("/api/inbox/1/approve", headers={"X-Vigil-UI": "1"})
    assert r.status_code == 403


def test_logout_ends_the_session(ui_client: TestClient, tokens: Tokens) -> None:
    _login(ui_client, tokens.admin)
    assert ui_client.post("/api/logout", headers={"X-Vigil-UI": "1"}).status_code == 204
    assert ui_client.get("/api/inbox").status_code == 401


def test_session_expires(ui_client: TestClient, tokens: Tokens, monkeypatch: object) -> None:
    _login(ui_client, tokens.admin)
    real = auth_mod.utcnow
    monkeypatch.setattr(auth_mod, "utcnow", lambda: real() + timedelta(hours=13))  # type: ignore[attr-defined]
    assert ui_client.get("/api/inbox").status_code == 401


def test_a_forged_cookie_is_401(ui_client: TestClient) -> None:
    ui_client.cookies.set("vigil_session", "x" * 43)
    assert ui_client.get("/api/inbox").status_code == 401


def test_bearer_still_works_for_the_cli(ui_client: TestClient, tokens: Tokens) -> None:
    assert ui_client.get("/api/inbox", headers=auth(tokens.admin)).status_code == 200


def test_the_loopback_listener_has_no_ui_login(
    client: TestClient, tokens: Tokens
) -> None:  # item 20
    """P7: the UI is served on the Tailscale address only; loopback is bearer-only."""
    assert client.post("/api/login", json={"token": tokens.admin}).status_code == 404
    client.cookies.set("vigil_session", "anything")
    assert client.get("/api/inbox").status_code == 401


def test_secure_cookie_behind_tailscale_serve(
    ui_client: TestClient, tokens: Tokens
) -> None:  # item 20
    r = ui_client.post(
        "/api/login", json={"token": tokens.admin}, headers={"X-Forwarded-Proto": "https"}
    )
    assert "Secure" in r.headers["set-cookie"]
