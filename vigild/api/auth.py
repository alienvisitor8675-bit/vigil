"""Auth (decision `api-auth`): bearer tokens with three scopes, plus the web UI's session cookie.

admin can do everything; read can GET; heartbeat can only post heartbeats
(phase 3). A scope whose token is unset matches nothing.

The web UI never stores a token (P7: no). `POST /api/login` takes the admin
or read token once and sets `vigil_session`: a random id (stored hashed),
HttpOnly, SameSite=Strict, Path=/, 12 hours, Secure when the UI is served over
HTTPS. A write made with the cookie must also carry `X-Vigil-UI: 1`, which a
cross-site form can't send. The CLI and MCP keep bearer tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum

from fastapi import HTTPException, Request

from vigild.db import Store, iso, utcnow
from vigild.services import Services, Tokens

COOKIE = "vigil_session"
UI_SESSION_TTL = timedelta(hours=12)
UI_HEADER = "x-vigil-ui"
SAFE = {"GET", "HEAD", "OPTIONS"}


class Scope(StrEnum):
    ADMIN = "admin"
    READ = "read"
    HEARTBEAT = "heartbeat"


GRANTS: dict[Scope, set[Scope]] = {
    Scope.ADMIN: {Scope.ADMIN, Scope.READ, Scope.HEARTBEAT},
    Scope.READ: {Scope.READ},
    Scope.HEARTBEAT: {Scope.HEARTBEAT},
}


def identify(tokens: Tokens, header: str | None) -> Scope | None:
    if not header or not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer ") :].strip().encode()
    if not presented:
        return None
    found: Scope | None = None
    for scope, token in (
        (Scope.ADMIN, tokens.admin),
        (Scope.READ, tokens.read),
        (Scope.HEARTBEAT, tokens.heartbeat),
    ):
        if token and hmac.compare_digest(presented, token.encode()) and found is None:
            found = scope
    return found


def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_ui_session(store: Store, scope: Scope) -> str:
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    store.x(
        "INSERT INTO ui_sessions (id_hash, scope, created_at, expires_at) VALUES (?,?,?,?)",
        (_h(raw), scope.value, iso(now), iso(now + UI_SESSION_TTL)),
    )
    return raw


def ui_session_scope(store: Store, raw: str | None) -> Scope | None:
    if not raw:
        return None
    rows = store.q(
        "SELECT scope, expires_at, revoked_at FROM ui_sessions WHERE id_hash=?", (_h(raw),)
    )
    if not rows or rows[0]["revoked_at"]:
        return None
    if datetime.fromisoformat(rows[0]["expires_at"]) <= utcnow():
        return None
    return Scope(rows[0]["scope"])


def end_ui_session(store: Store, raw: str | None) -> None:
    if raw:
        store.x("UPDATE ui_sessions SET revoked_at=? WHERE id_hash=?", (iso(), _h(raw)))


def require(needed: Scope) -> Callable[[Request], Scope]:
    def dep(request: Request) -> Scope:
        services: Services = request.app.state.services
        scope = identify(services.tokens, request.headers.get("authorization"))
        ui = bool(getattr(request.app.state, "ui", False))
        if ui and scope is None and "authorization" not in request.headers:
            scope = ui_session_scope(services.store, request.cookies.get(COOKIE))
            unsafe = scope is not None and request.method not in SAFE
            if unsafe and request.headers.get(UI_HEADER) != "1":
                raise HTTPException(403, "a write from the UI needs the X-Vigil-UI header")
        if scope is None:
            raise HTTPException(401, "missing or invalid bearer token")
        if needed not in GRANTS[scope]:
            raise HTTPException(403, f"the {scope.value} token can't do this")
        request.state.authed = True  # the channel middleware logs only authenticated calls
        return scope

    return dep
