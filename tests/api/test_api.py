"""REST API: auth scopes, tasks, jobs, the inbox, sessions, health."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import RecordingRunner, auth
from vigild.db import Store
from vigild.jobs.model import Role
from vigild.services import Tokens

PA = "thomasbardhi01/parkagent"


def _task(**over: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "repo": PA,
        "base": "main",
        "why": "the limit is undocumented",
        "value": 3,
        "risk": 1,
        "labels": ["docs"],
        "spec": {
            "goal": "Document the rate limit",
            "test_cmd": "pnpm -C server test",
            "done_when": "the feature map lists the rate limit",
            "files_in_scope": ["docs/feature-map.md"],
        },
    }
    t.update(over)
    return t


# --- auth -------------------------------------------------------------------


def test_no_token_is_401(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 401


def test_wrong_token_is_401(client: TestClient) -> None:
    assert client.get("/api/health", headers=auth("nope")).status_code == 401


def test_read_token_reads_but_cannot_write(client: TestClient, tokens: Tokens) -> None:
    assert client.get("/api/inbox", headers=auth(tokens.read)).status_code == 200
    r = client.post("/api/tasks", json=_task(), headers=auth(tokens.read))
    assert r.status_code == 403


def test_heartbeat_token_can_do_nothing_else(client: TestClient, tokens: Tokens) -> None:
    for path in ("/api/health", "/api/inbox", "/api/tasks", "/api/sessions", "/api/jobs"):
        assert client.get(path, headers=auth(tokens.heartbeat)).status_code == 403, path


def test_unset_token_never_authenticates(services: Any) -> None:
    services.tokens.read = None
    with TestClient(__import__("vigild.api.app", fromlist=["x"]).create_app(services)) as c:
        assert c.get("/api/health", headers={"Authorization": "Bearer "}).status_code == 401
        assert c.get("/api/health", headers=auth("None")).status_code == 401


def test_health(client: TestClient, tokens: Tokens) -> None:
    body = client.get("/api/health", headers=auth(tokens.read)).json()
    assert body["ok"] is True and body["db"] == "ok"
    assert "degraded" in body


# --- tasks become jobs ---------------------------------------------------------


def test_complete_task_becomes_a_job_waiting_for_approval(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:
    r = client.post("/api/tasks", json=_task(), headers=auth(tokens.admin))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["state"] == "spec_pending" and body["needs_spec"] is False
    job_id = body["job_id"]
    spec = store.get_artifact(job_id, "spec.md")
    assert spec is not None and spec.provenance == "human"
    assert store.get_artifact(job_id, "plan.md") is not None
    inbox = client.get("/api/inbox", headers=auth(tokens.read)).json()
    assert [i["job_id"] for i in inbox["specs"]] == [job_id]
    assert inbox["specs"][0]["body"]["workers"][0]["task_id"] == body["task_id"]
    # nothing runs before approval
    assert runner.approved == []


def test_task_without_a_test_becomes_a_spec_writing_job(
    client: TestClient, tokens: Tokens, runner: RecordingRunner
) -> None:
    t = _task()
    del t["spec"]["test_cmd"]
    body = client.post("/api/tasks", json=t, headers=auth(tokens.admin)).json()
    assert body["needs_spec"] is True and body["state"] == "spec_pending"
    assert runner.created == [body["job_id"]]
    inbox = client.get("/api/inbox", headers=auth(tokens.read)).json()
    assert inbox["specs"] == []  # the coord drafts first


def test_invalid_task_is_422(client: TestClient, tokens: Tokens) -> None:
    r = client.post("/api/tasks", json=_task(value=9), headers=auth(tokens.admin))
    assert r.status_code == 422


def test_task_without_why_is_rejected(client: TestClient, tokens: Tokens) -> None:
    r = client.post("/api/tasks", json=_task(why=" "), headers=auth(tokens.admin))
    assert r.status_code == 422 and "why" in r.text


def test_self_generated_low_value_is_proposed(client: TestClient, tokens: Tokens) -> None:
    r = client.post("/api/tasks", json=_task(source="self", value=2), headers=auth(tokens.admin))
    assert r.status_code == 202 and r.json()["state"] == "proposed"
    tasks = client.get("/api/tasks", headers=auth(tokens.read)).json()
    assert tasks[0]["state"] == "proposed"


def test_unknown_repo_is_refused(client: TestClient, tokens: Tokens) -> None:
    r = client.post("/api/tasks", json=_task(repo="someone/else"), headers=auth(tokens.admin))
    assert r.status_code == 422 and "repos.yaml" in r.text


def test_task_reads(client: TestClient, tokens: Tokens) -> None:
    body = client.post("/api/tasks", json=_task(), headers=auth(tokens.admin)).json()
    one = client.get(f"/api/tasks/{body['task_id']}", headers=auth(tokens.read)).json()
    assert one["id"] == body["task_id"] and one["job_id"] == body["job_id"]
    assert client.get("/api/tasks/nope", headers=auth(tokens.read)).status_code == 404


def test_job_request_starts_a_coord(
    client: TestClient, tokens: Tokens, runner: RecordingRunner
) -> None:
    r = client.post(
        "/api/jobs",
        json={
            "repo": PA,
            "title": "Document limits",
            "request": "The rate limits are undocumented.",
            "notes": ["keep it short"],
            "why": "support asks weekly",
            "value": 3,
            "risk": 1,
        },
        headers=auth(tokens.admin),
    )
    assert r.status_code == 202
    assert runner.created == [r.json()["id"]]


# --- the inbox ----------------------------------------------------------------


def _pending(client: TestClient, tokens: Tokens) -> tuple[str, int]:
    body = client.post("/api/tasks", json=_task(), headers=auth(tokens.admin)).json()
    inbox = client.get("/api/inbox", headers=auth(tokens.read)).json()
    return body["job_id"], inbox["specs"][0]["id"]


def test_inbox_has_three_queues(client: TestClient, tokens: Tokens) -> None:
    body = client.get("/api/inbox", headers=auth(tokens.read)).json()
    assert set(body) == {"specs", "ready", "questions"}


def test_approve_moves_the_job_and_is_logged(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:
    job_id, item = _pending(client, tokens)
    r = client.post(
        f"/api/inbox/{item}/approve",
        json={"edits": "also link the runbook"},
        headers=auth(tokens.admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "approved"
    assert runner.approved == [job_id]
    approvals = store.q("SELECT * FROM approvals WHERE job_id=?", (job_id,))
    assert len(approvals) == 1 and approvals[0]["actor"] == "owner"
    assert approvals[0]["edits"] == "also link the runbook"
    kinds = [e.type for e in store.events(job_id=job_id)]
    assert "approval" in kinds and "state" in kinds
    art = store.get_artifact(job_id, "approval.json")
    assert art is not None and art.provenance == "human"
    assert client.get("/api/inbox", headers=auth(tokens.read)).json()["specs"] == []


def test_approve_needs_the_admin_token(client: TestClient, tokens: Tokens) -> None:
    _, item = _pending(client, tokens)
    assert client.post(f"/api/inbox/{item}/approve", headers=auth(tokens.read)).status_code == 403


def test_approve_twice_is_409(client: TestClient, tokens: Tokens) -> None:
    _, item = _pending(client, tokens)
    assert client.post(f"/api/inbox/{item}/approve", headers=auth(tokens.admin)).status_code == 200
    assert client.post(f"/api/inbox/{item}/approve", headers=auth(tokens.admin)).status_code == 409


def test_approving_a_page_is_409(client: TestClient, tokens: Tokens, store: Store) -> None:
    item = store.create_inbox_item("questions", "page", None, "budget hit", {})
    r = client.post(f"/api/inbox/{item.id}/approve", headers=auth(tokens.admin))
    assert r.status_code == 409


def test_approve_unknown_is_404(client: TestClient, tokens: Tokens) -> None:
    assert client.post("/api/inbox/999/approve", headers=auth(tokens.admin)).status_code == 404


# --- jobs, tree, sessions -------------------------------------------------------


def test_tree_endpoint(client: TestClient, tokens: Tokens, store: Store) -> None:
    job_id, _ = _pending(client, tokens)
    coord = store.create_session(job_id, Role.COORD, backend_preset="claude-opus")
    store.create_session(job_id, Role.WORKER, parent_session_id=coord.id)
    tree = client.get(f"/api/jobs/{job_id}/tree", headers=auth(tokens.read)).json()
    assert tree["job"]["id"] == job_id
    assert tree["sessions"][0]["children"][0]["role"] == "worker"
    assert client.get("/api/jobs/zz-9999/tree", headers=auth(tokens.read)).status_code == 404


def test_jobs_list_and_get(client: TestClient, tokens: Tokens) -> None:
    job_id, _ = _pending(client, tokens)
    assert [j["id"] for j in client.get("/api/jobs", headers=auth(tokens.read)).json()] == [job_id]
    assert (
        client.get(f"/api/jobs/{job_id}", headers=auth(tokens.read)).json()["state"]
        == "spec_pending"
    )


def test_sessions_reads(client: TestClient, tokens: Tokens, store: Store) -> None:
    job_id, _ = _pending(client, tokens)
    s = store.create_session(job_id, Role.COORD, backend_preset="claude-opus")
    rows = client.get("/api/sessions", headers=auth(tokens.read)).json()
    assert [x["id"] for x in rows] == [s.id]
    one = client.get(f"/api/sessions/{s.id}", headers=auth(tokens.read)).json()
    assert one["role"] == "coord" and "gateway_token" not in str(one)
    assert client.get("/api/sessions/s-nope", headers=auth(tokens.read)).status_code == 404


@pytest.mark.parametrize("path", ["/api/jobs/x/state", "/api/jobs/x/approve"])
def test_no_backdoor_routes(client: TestClient, tokens: Tokens, path: str) -> None:
    assert client.post(path, headers=auth(tokens.admin)).status_code in (404, 405)


def test_money_path_with_local_ok_label_is_refused(client: TestClient, tokens: Tokens) -> None:
    t = _task(labels=["vigil:local-ok"])
    t["spec"]["files_in_scope"] = ["src/billing/fee.py"]
    r = client.post("/api/tasks", json=t, headers=auth(tokens.admin))
    assert r.status_code == 422 and "local-ok" in r.text
