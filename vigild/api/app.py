"""The REST API: the one API behind the CLI, MCP and the web UI.

Every route is declared with its full path here, so
scripts/check-feature-map.py can find it.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from vigild.api.auth import (
    COOKIE,
    UI_SESSION_TTL,
    Scope,
    create_ui_session,
    end_ui_session,
    identify,
    require,
)
from vigild.db import Store
from vigild.jobs.service import Conflict, JobRequest, NotFound, SubmitError
from vigild.jobs.tree import job_tree
from vigild.services import Services

router = APIRouter()
READ = Depends(require(Scope.READ))
ADMIN = Depends(require(Scope.ADMIN))


def _svc(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return getattr(obj, "value", obj)


# --- health ---------------------------------------------------------------------


@router.get("/api/health", dependencies=[READ])
def health(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    try:
        svc.store.q("SELECT 1")
        db = "ok"
    except Exception as e:  # pragma: no cover - a broken DB is the point of the check
        db = f"error: {e}"
    return {"ok": db == "ok", "db": db, "degraded": sorted(svc.degraded), "version": "0.1.0"}


# --- the web UI's session (P7: no token in the browser) -----------------------------


def _ui_only(request: Request) -> None:
    """P7: the cookie login exists only on the Tailscale listener (item 20)."""
    if not getattr(request.app.state, "ui", False):
        raise HTTPException(404, "not found")


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


@router.post("/api/login", status_code=204)
def login(request: Request, body: Annotated[dict[str, Any], Body()]) -> Response:
    _ui_only(request)
    svc = _svc(request)
    token = body.get("token") if isinstance(body.get("token"), str) else None
    scope = identify(svc.tokens, f"Bearer {token}") if token else None
    if scope not in (Scope.ADMIN, Scope.READ):
        raise HTTPException(401, "invalid token")
    assert scope is not None
    resp = Response(status_code=204)
    resp.set_cookie(
        COOKIE,
        create_ui_session(svc.store, scope),
        max_age=int(UI_SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,
        samesite="strict",
        secure=svc.cfg.vigild.api.cookie_secure or _https(request),
    )
    return resp


@router.post("/api/logout", status_code=204, dependencies=[READ])
def logout(request: Request) -> Response:
    _ui_only(request)
    end_ui_session(_svc(request).store, request.cookies.get(COOKIE))
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# --- tasks and jobs ---------------------------------------------------------------


@router.post("/api/tasks", dependencies=[ADMIN], status_code=202)
def submit_task(request: Request, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    try:
        out = _svc(request).jobs.submit_task(body)
    except SubmitError as e:
        raise HTTPException(422, {"errors": e.reasons}) from e
    return _plain(out)  # type: ignore[no-any-return]


@router.get("/api/tasks", dependencies=[READ])
def list_tasks(request: Request) -> list[dict[str, Any]]:
    return [_plain(t) for t in _svc(request).store.list_tasks()]


@router.get("/api/tasks/{task_id}", dependencies=[READ])
def get_task(request: Request, task_id: str) -> dict[str, Any]:
    try:
        return _plain(_svc(request).store.get_task(task_id))  # type: ignore[no-any-return]
    except KeyError as e:
        raise HTTPException(404, "no such task") from e


@router.post("/api/jobs", dependencies=[ADMIN], status_code=202)
def submit_job(request: Request, body: JobRequest) -> dict[str, Any]:
    try:
        job = _svc(request).jobs.submit_request(body)
    except SubmitError as e:
        raise HTTPException(422, {"errors": e.reasons}) from e
    return _plain(job)  # type: ignore[no-any-return]


@router.get("/api/jobs", dependencies=[READ])
def list_jobs(request: Request) -> list[dict[str, Any]]:
    return [_plain(j) for j in _svc(request).store.list_jobs()]


def _job_or_404(store: Store, job_id: str) -> None:
    try:
        store.get_job(job_id)
    except KeyError as e:
        raise HTTPException(404, "no such job") from e


@router.get("/api/jobs/{job_id}", dependencies=[READ])
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    store = _svc(request).store
    _job_or_404(store, job_id)
    return _plain(store.get_job(job_id))  # type: ignore[no-any-return]


@router.get("/api/jobs/{job_id}/tree", dependencies=[READ])
def get_job_tree(request: Request, job_id: str) -> dict[str, Any]:
    store = _svc(request).store
    _job_or_404(store, job_id)
    return job_tree(store, job_id)


# --- the inbox --------------------------------------------------------------------


@router.get("/api/inbox", dependencies=[READ])
def inbox(request: Request) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"specs": [], "ready": [], "questions": []}
    for item in _svc(request).store.list_inbox("open"):
        out[item.queue].append(_plain(item))
    return out


@router.post("/api/inbox/{item_id}/approve", dependencies=[ADMIN])
def approve(
    request: Request, item_id: int, body: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    edits = (body or {}).get("edits")
    try:
        job = _svc(request).jobs.approve(item_id, edits if isinstance(edits, str) else None)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "state": job.state.value}


@router.post("/api/inbox/{item_id}/resume", dependencies=[ADMIN])
def resume(request: Request, item_id: int) -> dict[str, Any]:
    try:
        job = _svc(request).jobs.resume(item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "state": job.state.value}


def _act(request: Request, item_id: int, action: str) -> dict[str, Any]:
    jobs = _svc(request).jobs
    try:
        job = {"retry": jobs.retry, "replan": jobs.replan, "abandon": jobs.abandon}[action](item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "state": job.state.value}


@router.post("/api/inbox/{item_id}/retry", dependencies=[ADMIN])
def retry(request: Request, item_id: int) -> dict[str, Any]:
    """A blocked job runs its failed tasks again, on what's left of their caps."""
    return _act(request, item_id, "retry")


@router.post("/api/inbox/{item_id}/replan", dependencies=[ADMIN])
def replan(request: Request, item_id: int) -> dict[str, Any]:
    """A blocked job goes back to its coord, with the failures in the packet."""
    return _act(request, item_id, "replan")


@router.post("/api/inbox/{item_id}/abandon", dependencies=[ADMIN])
def abandon(request: Request, item_id: int) -> dict[str, Any]:
    return _act(request, item_id, "abandon")


@router.post("/api/inbox/{item_id}/rebuild", dependencies=[ADMIN])
def rebuild(request: Request, item_id: int) -> dict[str, Any]:
    """Build and validate a repo's failed dev image again."""
    try:
        _svc(request).jobs.rebuild(item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"item_id": item_id, "status": "rebuilding"}


@router.post("/api/inbox/{item_id}/dismiss", dependencies=[ADMIN])
def dismiss(request: Request, item_id: int) -> dict[str, Any]:
    try:
        _svc(request).jobs.dismiss(item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"item_id": item_id, "status": "resolved"}


# --- sessions -----------------------------------------------------------------------


@router.get("/api/sessions", dependencies=[READ])
def list_sessions(request: Request) -> list[dict[str, Any]]:
    return [_plain(s) for s in _svc(request).store.list_sessions()]


@router.get("/api/sessions/{session_id}", dependencies=[READ])
def get_session(request: Request, session_id: str) -> dict[str, Any]:
    try:
        return _plain(_svc(request).store.get_session(session_id))  # type: ignore[no-any-return]
    except KeyError as e:
        raise HTTPException(404, "no such session") from e


def create_app(services: Services, *, ui: bool = False) -> FastAPI:
    """`ui=True` only for the listener on the Mac's Tailscale address: it adds
    the cookie login (P7). The loopback listener is bearer-only (CLI, MCP)."""
    app = FastAPI(title="vigild", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    app.state.ui = ui
    app.include_router(router)

    @app.exception_handler(KeyError)
    async def _missing(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    return app
