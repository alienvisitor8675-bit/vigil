"""The REST API: the one API behind the CLI, MCP and the web UI.

Every route is declared with its full path here, so
scripts/check-feature-map.py can find it.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

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
from vigild.jobs.service import (
    BadNote,
    Conflict,
    DuplicateIssue,
    JobRequest,
    NoteTooLarge,
    NotFound,
    SubmitError,
)
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
    except DuplicateIssue as e:
        raise HTTPException(409, {"errors": e.reasons, "job_id": e.job_id}) from e
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
    jobs = _svc(request).jobs
    try:
        job = jobs.resume(item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    # `queued`: the job's current run finishes first, then this one (2a round 3)
    return {"job_id": job.id, "state": job.state.value, "run": jobs.last_run.get(job.id)}


def _act(request: Request, item_id: int, action: str) -> dict[str, Any]:
    jobs = _svc(request).jobs
    try:
        job = {"retry": jobs.retry}[action](item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "state": job.state.value, "run": jobs.last_run.get(job.id)}


@router.post("/api/inbox/{item_id}/retry", dependencies=[ADMIN])
def retry(request: Request, item_id: int) -> dict[str, Any]:
    """A blocked job runs its failed tasks again, on what's left of their caps."""
    return _act(request, item_id, "retry")


@router.post("/api/inbox/{item_id}/replan", dependencies=[ADMIN])
def replan(
    request: Request, item_id: int, body: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """A blocked job goes back to its coord, with the failures in the packet. With
    {"note": "..."} (8 KB at most, kept verbatim as the owner's text) the coord
    amends the previously approved spec instead of redrafting it. A spec item's
    "replan with note" is the same call, and requires the note."""
    note = (body or {}).get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(422, "note must be text")
    jobs = _svc(request).jobs
    try:
        job = jobs.replan(item_id, note)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except NoteTooLarge as e:
        raise HTTPException(413, str(e)) from e
    except BadNote as e:
        raise HTTPException(422, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "state": job.state.value, "run": jobs.last_run.get(job.id)}


@router.post("/api/inbox/{item_id}/fix", dependencies=[ADMIN])
def fix(
    request: Request, item_id: int, body: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """ "Fix with findings" on a ready item: {"task_id": ..., "findings": [ids or
    indexes from the item's `selectable`], "note": "..."} (the note optional, 8 KB at
    most). The task's worker fixes them in the standard fix round, on the same branch and
    PR and on what is left of its cap; 409 if the job isn't ready, the PR is merged or
    closed, the findings aren't the task's, the task has used its review-round limit,
    or too little of the cap is left. The answer carries a `notice` when the round
    runs on an escalated (pricier) backend."""
    body = body or {}
    jobs = _svc(request).jobs
    try:
        job = jobs.fix(item_id, body.get("task_id"), body.get("findings"), body.get("note"))
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except NoteTooLarge as e:
        raise HTTPException(413, str(e)) from e
    except BadNote as e:
        raise HTTPException(422, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    out = {"job_id": job.id, "state": job.state.value, "run": jobs.last_run.get(job.id)}
    if notice := jobs.last_notice.get(job.id):
        out["notice"] = notice
    return out


@router.post("/api/inbox/{item_id}/abandon", dependencies=[ADMIN])
def abandon(
    request: Request, item_id: int, payload: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """Give a job up. On a breaker's page, `{"job_id": ...}` names which of
    the jobs it holds (2a round 3)."""
    job_id = (payload or {}).get("job_id")
    try:
        job = _svc(request).jobs.abandon(item_id, str(job_id) if job_id else None)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "state": job.state.value}


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


@router.post("/api/inbox/{item_id}/reset", dependencies=[ADMIN])
def reset(request: Request, item_id: int) -> dict[str, Any]:
    """Reset a tripped circuit breaker; the jobs it paused resume."""
    try:
        out = _svc(request).jobs.reset(item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    # `queued`: the job's last run is still finishing; it picks up after (B4)
    return {"item_id": item_id, "status": "reset", **out}


@router.post("/api/inbox/{item_id}/dismiss", dependencies=[ADMIN])
def dismiss(request: Request, item_id: int) -> dict[str, Any]:
    try:
        _svc(request).jobs.dismiss(item_id)
    except NotFound as e:
        raise HTTPException(404, "no such inbox item") from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return {"item_id": item_id, "status": "resolved"}


# --- reports ------------------------------------------------------------------------


@router.get("/api/decider/report", dependencies=[READ])
def decider_report(request: Request) -> dict[str, Any]:
    """Confidence per question type, and escalations to Haiku (owner ruling 6)."""
    from vigild.kpis import decider_report as report

    return report(_svc(request).store)


@router.get("/api/review/ladder", dependencies=[READ])
def review_ladder(request: Request) -> dict[str, Any]:
    """Each shape's live rung, its limit and its last change (owner ruling 1)."""
    from vigild.review.ladder import Ladder

    svc = _svc(request)
    return Ladder(svc.store, svc.cfg).state()


class ReplayBody(BaseModel):
    """`POST /api/review/replay`. No visibility field (and no other): a repo's privacy and
    backends come from configs/repos.yaml and vigild/repos.py, as for a real review."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=200)
    # one of the task's published heads (default: the latest); it picks the case's round
    head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class ReplaySetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_version: str = Field(min_length=1, max_length=200)


def _replayed(call: Any) -> dict[str, Any]:
    from vigild.review.replay import ReplayError

    try:
        out: dict[str, Any] = call()
    except ReplayError as e:
        raise HTTPException(e.status, str(e)) from e
    except RuntimeError as e:  # no runner attached
        raise HTTPException(503, str(e)) from e
    return out


@router.post("/api/review/replay", dependencies=[ADMIN])
def review_replay(request: Request, body: ReplayBody) -> dict[str, Any]:
    """Run one reviewer session on a past task head with a chosen reviewer prompt, on the
    experiment line and `eval-only`; it changes no job, task, ladder or inbox (#47)."""
    runner = _svc(request).jobs.runner
    return _replayed(
        lambda: runner.replay_review(body.job_id, body.task_id, body.prompt_version, body.head)
    )


@router.post("/api/review/replay-set", dependencies=[ADMIN])
def review_replay_set(request: Request, body: ReplaySetBody) -> dict[str, Any]:
    """Replay every case of the review regression set with a prompt version; report recall
    and the total cost (#47)."""
    runner = _svc(request).jobs.runner
    return _replayed(lambda: runner.replay_set(body.prompt_version))


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
