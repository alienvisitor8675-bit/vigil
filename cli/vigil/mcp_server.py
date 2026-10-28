"""`vigil mcp`: a stdio MCP server over vigild's REST API (#55).

One thin client, like the CLI: every tool is one API call. The token is
`VIGIL_API_TOKEN` from this process's environment only (never `~/.config/vigil/`), it
goes in one header, and every string that leaves a tool is scrubbed of it. Each request
carries `X-Vigil-Channel: mcp`; vigild validates that and logs the call.

Not named `mcp.py`: `python cli/vigil` puts this directory first on sys.path, and a
module of that name would shadow the SDK.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_URL = "http://127.0.0.1:8787"
CHANNEL = ("X-Vigil-Channel", "mcp")
TOOLS = ("submit_job", "job_status", "list_inbox", "inbox_act", "kill",
         "submit_batch", "batch_status", "approve_all")  # fmt: skip
# vigild's job ids: a short repo prefix, a dash, a counter (`pa-0007`)
JOB_ID = re.compile(r"^[a-z0-9]{1,8}-[0-9]{1,9}$")
# a batch id (`b-0001`), as `vigil batch status` checks it
BATCH_ID = re.compile(r"^b-[0-9]+$")
Action = Literal["approve", "replan", "retry", "abandon", "reset"]


def build_server(client: httpx2.Client, token: str | None = None) -> MCPServer:
    """The server over `client` (base URL and bearer header set by the caller)."""
    mcp = MCPServer("vigil")

    def scrub(text: str) -> str:
        return text.replace(token, "[token]") if token else text

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = client.request(method, path, json=body, headers=dict([CHANNEL]))
        except httpx2.HTTPError as e:
            raise ToolError(scrub(f"could not reach vigild: {type(e).__name__}")) from None
        text = scrub(r.text)
        if r.status_code >= 400:
            raise ToolError(f"vigild answered {r.status_code}: {text[:500]}")
        try:
            out = r.json()
        except ValueError:
            return {"result": text[:2000]}
        return out if isinstance(out, dict) else {"result": out}

    def job_path(job_id: str) -> str:
        """The one place an id becomes a URL part: checked, then quoted."""
        if not JOB_ID.fullmatch(job_id):
            raise ToolError("job_id is not a job id (like pa-0007)")
        return quote(job_id, safe="")

    def batch_path(batch_id: str) -> str:
        if not BATCH_ID.fullmatch(batch_id):
            raise ToolError("batch_id is not a batch id (like b-0001)")
        return quote(batch_id, safe="")

    def item_path(item_id: int) -> str:
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
            raise ToolError("item_id must be a positive integer")
        return str(item_id)

    def clean(out: Any) -> Any:
        if isinstance(out, str):
            return scrub(out)
        if isinstance(out, dict):
            return {k: clean(v) for k, v in out.items()}
        if isinstance(out, list):
            return [clean(v) for v in out]
        return out

    @mcp.tool()
    def submit_job(
        repo: str,
        title: str,
        request: str,
        why: str,
        value: int,
        risk: int,
        base: str = "main",
        notes: list[str] | None = None,
        labels: list[str] | None = None,
        issue: int | None = None,
    ) -> dict[str, Any]:
        """Submit a job (POST /api/jobs). It lands in the inbox as a spec for the owner to
        approve. Returns the job (id, state)."""
        body = {"repo": repo, "base": base, "title": title, "request": request, "why": why,
                "value": value, "risk": risk, "notes": notes or [], "labels": labels or [],
                "issue": issue}  # fmt: skip
        return clean(call("POST", "/api/jobs", body))  # type: ignore[no-any-return]

    @mcp.tool()
    def job_status(job_id: str) -> dict[str, Any]:
        """A job's state and fields (GET /api/jobs/{id})."""
        return clean(call("GET", f"/api/jobs/{job_path(job_id)}"))  # type: ignore[no-any-return]

    @mcp.tool()
    def list_inbox() -> dict[str, Any]:
        """The open inbox: specs, ready PRs and questions (GET /api/inbox)."""
        return clean(call("GET", "/api/inbox"))  # type: ignore[no-any-return]

    @mcp.tool()
    def inbox_act(
        item_id: int,
        action: Action,
        note: str | None = None,
        edits: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Act on an inbox item: approve (optional `edits`), replan (`note` required), retry,
        abandon (optional `job_id` on a breaker's page) or reset."""
        path = f"/api/inbox/{item_path(item_id)}/{action}"
        body: dict[str, Any] | None = None
        if action == "approve" and edits is not None:
            body = {"edits": edits}
        elif action == "replan":
            if not note:
                raise ToolError("replan needs a note")
            body = {"note": note}
        elif action == "abandon" and job_id:
            job_path(job_id)
            body = {"job_id": job_id}
        return clean(call("POST", path, body))  # type: ignore[no-any-return]

    @mcp.tool()
    def kill(job_id: str) -> dict[str, Any]:
        """Stop a job cleanly and abandon it (POST /api/jobs/{id}/kill). Open PRs stay."""
        return clean(call("POST", f"/api/jobs/{job_path(job_id)}/kill", {"channel": "mcp"}))  # type: ignore[no-any-return]

    @mcp.tool()
    def submit_batch(name: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        """Submit 1 to 10 jobs as one batch (POST /api/batch). Each job has the fields
        submit_job takes. All are made or none. Returns the batch id and job ids."""
        return clean(call("POST", "/api/batch", {"name": name, "jobs": jobs}))  # type: ignore[no-any-return]

    @mcp.tool()
    def batch_status(batch_id: str) -> dict[str, Any]:
        """Per-job state and cost of a batch, and its total (GET /api/batch/{id})."""
        return clean(call("GET", f"/api/batch/{batch_path(batch_id)}"))  # type: ignore[no-any-return]

    @mcp.tool()
    def approve_all(batch_id: str) -> dict[str, Any]:
        """Approve every open spec of a batch (POST /api/batch/{id}/approve-all)."""
        return clean(call("POST", f"/api/batch/{batch_path(batch_id)}/approve-all"))  # type: ignore[no-any-return]

    return mcp


def serve() -> None:
    """Run on stdio. Environment only: `VIGIL_API_TOKEN`, `VIGIL_API_URL`."""
    token = os.environ.get("VIGIL_API_TOKEN")
    if not token:
        raise SystemExit("vigil mcp: VIGIL_API_TOKEN is not set in this process's environment")
    client = httpx2.Client(
        base_url=os.environ.get("VIGIL_API_URL") or DEFAULT_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,  # a kill waits for in-flight model calls to drain
    )
    try:
        build_server(client, token).run("stdio")
    finally:
        client.close()
