"""`vigil mcp` (#55): a stdio MCP server over the REST API. The tools map one to one onto
routes, send the channel header, and never show the token."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client  # noqa: F401  (the SDK's own stdio transport)

from tests.conftest import auth
from vigild.api.app import create_app
from vigild.config import Config
from vigild.db import Store
from vigild.jobs.service import JobService
from vigild.services import Services, Tokens

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))

from vigil.mcp_server import DEFAULT_URL, TOOLS, build_server  # noqa: E402

TOKEN = "s3cret-admin-token-0123456789abcdef"
REQ: dict[str, Any] = {
    "repo": "thomasbardhi01/parkagent", "title": "T", "request": "do it", "why": "w",
    "value": 3, "risk": 1,
}  # fmt: skip


class Seen:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.status, self.body = status, {"ok": True} if body is None else body

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body)


def _server(seen: Seen) -> Any:
    client = httpx2.Client(
        base_url="http://vigild.test",
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=httpx2.MockTransport(seen),
    )
    return build_server(client, TOKEN)


async def _call(seen: Seen, tool: str, args: dict[str, Any]) -> Any:
    async with Client(_server(seen)) as c:
        return await c.call_tool(tool, args)


def test_the_default_url_is_the_loopback_api(cfg: Config) -> None:
    assert f"http://{cfg.vigild.api.host}:{cfg.vigild.api.port}" == DEFAULT_URL


async def test_the_tool_set_is_exactly_the_eight() -> None:
    async with Client(_server(Seen())) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"submit_job", "job_status", "list_inbox", "inbox_act", "kill",
                     "submit_batch", "batch_status", "approve_all"}  # fmt: skip
    assert names == set(TOOLS)


async def test_submit_job_posts_the_job_fields() -> None:
    seen = Seen(202, {"id": "pa-0001"})
    r = await _call(seen, "submit_job", {**REQ, "notes": ["n"], "issue": 7})
    assert not r.is_error
    [req] = seen.requests
    assert (req.method, req.url.path) == ("POST", "/api/jobs")
    assert json.loads(req.content) == {**REQ, "base": "main", "notes": ["n"], "issue": 7,
                                       "labels": []}  # fmt: skip


@pytest.mark.parametrize(
    ("tool", "args", "method", "path"),
    [
        ("job_status", {"job_id": "pa-0001"}, "GET", "/api/jobs/pa-0001"),
        ("list_inbox", {}, "GET", "/api/inbox"),
        ("kill", {"job_id": "pa-0001"}, "POST", "/api/jobs/pa-0001/kill"),
        ("submit_batch", {"name": "n", "jobs": [REQ]}, "POST", "/api/batch"),
        ("batch_status", {"batch_id": "b-0001"}, "GET", "/api/batch/b-0001"),
        ("approve_all", {"batch_id": "b-0001"}, "POST", "/api/batch/b-0001/approve-all"),
        ("inbox_act", {"item_id": 4, "action": "approve"}, "POST", "/api/inbox/4/approve"),
        ("inbox_act", {"item_id": 4, "action": "retry"}, "POST", "/api/inbox/4/retry"),
        ("inbox_act", {"item_id": 4, "action": "abandon"}, "POST", "/api/inbox/4/abandon"),
        ("inbox_act", {"item_id": 4, "action": "reset"}, "POST", "/api/inbox/4/reset"),
        ("inbox_act", {"item_id": 4, "action": "replan", "note": "x"}, "POST", "/api/inbox/4/replan"),
    ],
)  # fmt: skip
async def test_each_tool_maps_to_its_route(
    tool: str, args: dict[str, Any], method: str, path: str
) -> None:
    seen = Seen()
    r = await _call(seen, tool, args)
    assert not r.is_error
    [req] = seen.requests
    assert (req.method, req.url.path) == (method, path)
    assert req.headers["x-vigil-channel"] == "mcp"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


async def test_bodies_carry_the_note_and_the_kill_channel() -> None:
    seen = Seen()
    await _call(seen, "inbox_act", {"item_id": 4, "action": "replan", "note": "use B"})
    await _call(seen, "kill", {"job_id": "pa-0001"})
    assert json.loads(seen.requests[0].content) == {"note": "use B"}
    assert json.loads(seen.requests[1].content) == {"channel": "mcp"}


async def test_replan_without_a_note_is_refused_before_any_call() -> None:
    seen = Seen()
    r = await _call(seen, "inbox_act", {"item_id": 4, "action": "replan"})
    assert r.is_error and seen.requests == []


EVIL = ["a/../../inbox/1/dismiss", "../x", "pa-0001/kill", "pa-0001?x=1", "pa 0001", "", "%2e%2e"]


@pytest.mark.parametrize("bad", EVIL)
@pytest.mark.parametrize("tool", ["job_status", "kill"])
async def test_a_traversal_job_id_is_refused_before_any_call(tool: str, bad: str) -> None:
    seen = Seen()
    r = await _call(seen, tool, {"job_id": bad})
    assert r.is_error and seen.requests == []


BAD_BATCH = ["../x", "b-1/kill", "b-1?x=1", "b 1", "", "pa-0001", "b-", "b-1\n", "%2e%2e"]


@pytest.mark.parametrize("bad", BAD_BATCH)
@pytest.mark.parametrize("tool", ["batch_status", "approve_all"])
async def test_a_bad_batch_id_is_refused_before_any_call(tool: str, bad: str) -> None:
    seen = Seen()
    r = await _call(seen, tool, {"batch_id": bad})
    assert r.is_error and seen.requests == []


async def test_submit_batch_posts_name_and_jobs() -> None:
    seen = Seen(202, {"batch_id": "b-0001"})
    await _call(seen, "submit_batch", {"name": "n", "jobs": [REQ]})
    assert json.loads(seen.requests[0].content) == {"name": "n", "jobs": [REQ]}


async def test_batch_tools_never_show_the_token() -> None:
    for tool, args in (("batch_status", {"batch_id": "b-1"}), ("approve_all", {"batch_id": "b-1"}),
                       ("submit_batch", {"name": "n", "jobs": [REQ]})):  # fmt: skip
        r = await _call(Seen(500, {"detail": f"Bearer {TOKEN}"}), tool, args)
        assert r.is_error and TOKEN not in r.content[0].text


async def test_abandon_with_a_bad_job_id_and_a_bad_item_id_are_refused() -> None:
    seen = Seen()
    r = await _call(seen, "inbox_act", {"item_id": 4, "action": "abandon", "job_id": "../x"})
    assert r.is_error
    for item in (0, -1):
        r = await _call(seen, "inbox_act", {"item_id": item, "action": "retry"})
        assert r.is_error
    assert seen.requests == []


async def test_an_unknown_action_is_refused() -> None:
    seen = Seen()
    r = await _call(seen, "inbox_act", {"item_id": 4, "action": "merge"})
    assert r.is_error and seen.requests == []


async def test_an_api_error_is_a_tool_error_with_its_status() -> None:
    r = await _call(Seen(422, {"detail": {"errors": ["why is empty"]}}), "submit_job", REQ)
    assert r.is_error
    text = r.content[0].text
    assert "422" in text and "why is empty" in text


async def test_the_token_never_appears_in_output() -> None:
    # even if vigild (or a proxy) echoed it back
    for status in (200, 500):
        r = await _call(
            Seen(status, {"detail": f"bad Bearer {TOKEN}", "x": TOKEN}), "list_inbox", {}
        )
        assert TOKEN not in json.dumps([c.model_dump() for c in r.content])
        assert TOKEN not in json.dumps(r.structured_content)


async def test_an_unreachable_vigild_is_a_tool_error_without_the_token() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"refused for Bearer {TOKEN}")

    client = httpx2.Client(base_url="http://x.test", transport=httpx2.MockTransport(boom))
    async with Client(build_server(client, TOKEN)) as c:
        r = await c.call_tool("list_inbox", {})
    assert r.is_error and TOKEN not in r.content[0].text


# --- end to end: the real stdio process against a real (scratch) vigild -----------------


class InboxRunner:
    """Stands in for the coord: a new free-form job gets its spec item in the inbox."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def on_job_created(self, job_id: str, needs_spec: bool) -> None:
        self.store.create_inbox_item("specs", "spec", job_id, "spec", {})

    def busy(self, job_id: str) -> bool:
        return False


@pytest.fixture
def vigild(cfg: Config, store: Store, tokens: Tokens) -> Iterator[str]:
    services = Services(cfg=cfg, store=store, tokens=tokens, jobs=JobService(cfg, store, InboxRunner(store)))  # type: ignore[arg-type]  # fmt: skip
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(services), host="127.0.0.1", port=port, log_level="error")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(10)


async def test_a_job_submitted_over_stdio_lands_in_the_inbox(
    vigild: str, tokens: Tokens, store: Store, tmp_path: Path
) -> None:
    env = {"PATH": os.environ["PATH"], "VIGIL_API_URL": vigild, "VIGIL_API_TOKEN": tokens.admin,
           "HOME": str(tmp_path)}  # no ~/.config/vigil to read  # fmt: skip
    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "cli" / "vigil"), "mcp"], env=env
    )
    async with Client(params) as c:
        out = await c.call_tool("submit_job", REQ)
        assert not out.is_error, out
        job_id = out.structured_content["id"]  # type: ignore[index]
        status = await c.call_tool("job_status", {"job_id": job_id})
        assert status.structured_content["state"] == "spec_pending"  # type: ignore[index]
        assert tokens.admin not in json.dumps(out.model_dump(mode="json"))
    inbox = httpx2.get(f"{vigild}/api/inbox", headers=auth(tokens.read)).json()
    assert [i["job_id"] for i in inbox["specs"]] == [job_id]
    calls = [e.data for e in store.events(job_id=job_id) if e.type == "mcp_call"]
    assert {c["route"] for c in calls} == {"/api/jobs", "/api/jobs/{job_id}"}
    assert all(c["channel"] == "mcp" for c in calls)


async def test_a_batch_over_stdio_is_submitted_seen_and_approved(
    vigild: str, tokens: Tokens, store: Store, tmp_path: Path
) -> None:
    env = {"PATH": os.environ["PATH"], "VIGIL_API_URL": vigild, "VIGIL_API_TOKEN": tokens.admin,
           "HOME": str(tmp_path)}  # fmt: skip
    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "cli" / "vigil"), "mcp"], env=env
    )
    async with Client(params) as c:
        out = await c.call_tool(
            "submit_batch", {"name": "pair", "jobs": [REQ, {**REQ, "title": "T2"}]}
        )
        assert not out.is_error, out
        batch_id = out.structured_content["batch_id"]  # type: ignore[index]
        ids = out.structured_content["jobs"]  # type: ignore[index]
        assert len(ids) == 2
        st = await c.call_tool("batch_status", {"batch_id": batch_id})
        assert not st.is_error, st
        assert tokens.admin not in json.dumps(st.model_dump(mode="json"))
        inbox = httpx2.get(f"{vigild}/api/inbox", headers=auth(tokens.read)).json()
        assert sorted(i["job_id"] for i in inbox["specs"]) == sorted(ids)
        ap = await c.call_tool("approve_all", {"batch_id": batch_id})
        assert not ap.is_error, ap
        assert tokens.admin not in json.dumps(ap.model_dump(mode="json"))
    routes = {e.data["route"] for e in store.events() if e.type == "mcp_call"}
    assert {"/api/batch", "/api/batch/{batch_id}", "/api/batch/{batch_id}/approve-all"} <= routes
