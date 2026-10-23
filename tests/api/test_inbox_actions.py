"""Review round 1: approve is atomic (19); resume and dismiss from the inbox (18);
vigild assigns task ids (16); no orphan job on a gate failure (nit)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import RecordingRunner, auth
from vigild.db import Store
from vigild.jobs.fsm import Actor, transition
from vigild.jobs.model import JobState
from vigild.services import Tokens

PA = "thomasbardhi01/parkagent"


def _task(**over: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "repo": PA, "why": "w", "value": 3, "risk": 1, "labels": ["docs"],
        "spec": {"goal": "Document the limit", "test_cmd": "pnpm -C server test",
                 "done_when": "listed", "files_in_scope": ["docs/a.md"]},
    }  # fmt: skip
    t.update(over)
    return t


def _pending(client: TestClient, tokens: Tokens, **over: Any) -> tuple[str, int]:
    body = client.post("/api/tasks", json=_task(**over), headers=auth(tokens.admin)).json()
    item = client.get("/api/inbox", headers=auth(tokens.read)).json()["specs"][0]
    return body["job_id"], item["id"]


def test_approve_is_atomic(
    client: TestClient,
    tokens: Tokens,
    store: Store,
    runner: RecordingRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, item = _pending(client, tokens)
    real = store.add_artifact

    def boom(job: str, name: str, *a: Any, **k: Any) -> int:
        if name == "approval.json":
            raise RuntimeError("disk full")
        return real(job, name, *a, **k)

    monkeypatch.setattr(store, "add_artifact", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/api/inbox/{item}/approve", headers=auth(tokens.admin), json={})
    assert store.get_job(job_id).state is JobState.SPEC_PENDING
    assert store.q("SELECT COUNT(*) n FROM approvals")[0]["n"] == 0
    assert store.get_inbox_item(item).status == "open"
    assert runner.approved == []


def test_vigild_assigns_the_task_id(client: TestClient, tokens: Tokens) -> None:
    body = client.post(
        "/api/tasks", json=_task(task_id="someone-elses-w1"), headers=auth(tokens.admin)
    ).json()
    assert body["task_id"] == f"{body['job_id']}-w1"


def test_a_task_the_gate_refuses_leaves_no_orphan_job(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    t = _task()
    t["spec"]["test_cmd"] = "pytest -q"  # a runner the ParkAgent dev image lacks
    r = client.post("/api/tasks", json=t, headers=auth(tokens.admin))
    assert r.status_code == 422 and "pytest" in r.text
    assert store.list_jobs() == []


def _blocked_page(store: Store, job_id: str, resumable: bool = True) -> int:
    store._force_state(job_id, JobState.BUILDING)
    transition(store, job_id, JobState.BLOCKED, Actor.VIGILD, evidence={"reason": "budget"})
    return store.create_inbox_item(
        "questions", "page", job_id, "daily cap reached", {"resumable": resumable}
    ).id


def test_resume_from_the_inbox_moves_the_job_back_and_redispatches(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:
    job_id, _ = _pending(client, tokens)
    page = _blocked_page(store, job_id)
    assert client.post(f"/api/inbox/{page}/resume", headers=auth(tokens.read)).status_code == 403
    r = client.post(f"/api/inbox/{page}/resume", headers=auth(tokens.admin))
    assert r.status_code == 200, r.text
    assert store.get_job(job_id).state is JobState.BUILDING
    assert runner.resumed == [job_id]
    assert store.get_inbox_item(page).status == "resolved"
    state = [e for e in store.events(job_id=job_id) if e.type == "state"][-1]
    assert state.data["channel"] == "inbox" and state.data["actor"] == "owner"


def test_a_page_that_is_not_resumable_cannot_resume(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    job_id, _ = _pending(client, tokens)
    page = _blocked_page(store, job_id, resumable=False)
    assert client.post(f"/api/inbox/{page}/resume", headers=auth(tokens.admin)).status_code == 409


def test_dismiss_resolves_a_page(client: TestClient, tokens: Tokens, store: Store) -> None:
    item = store.create_inbox_item("questions", "page", None, "fyi", {})
    assert (
        client.post(f"/api/inbox/{item.id}/dismiss", headers=auth(tokens.admin)).status_code == 200
    )
    assert store.get_inbox_item(item.id).status == "resolved"
    assert (
        client.post(f"/api/inbox/{item.id}/dismiss", headers=auth(tokens.admin)).status_code == 409
    )


def test_a_spec_cannot_be_dismissed_or_resumed(client: TestClient, tokens: Tokens) -> None:
    _, item = _pending(client, tokens)
    assert client.post(f"/api/inbox/{item}/resume", headers=auth(tokens.admin)).status_code == 409


def test_two_resumable_pages_start_one_run(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:  # round 3, F7
    job_id, _ = _pending(client, tokens)
    first = _blocked_page(store, job_id)
    second = store.create_inbox_item(
        "questions", "page", job_id, "another page", {"resumable": True}
    ).id
    assert client.post(f"/api/inbox/{first}/resume", headers=auth(tokens.admin)).status_code == 200
    assert store.get_inbox_item(second).status == "resolved"  # closed with the first
    assert client.post(f"/api/inbox/{second}/resume", headers=auth(tokens.admin)).status_code == 409
    assert runner.resumed == [job_id]


def test_the_gate_resolves_package_scripts_with_the_base_files(
    client: TestClient, tokens: Tokens, store: Store
) -> None:  # round 4, item 6
    svc = client.app.state.services.jobs  # type: ignore[attr-defined]
    pkg = {"server/package.json": '{"scripts": {"test": "vitest run", "lint": "eslint src"}}'}
    svc.files = lambda repo, base: pkg.get
    t = _task()
    t["spec"]["test_cmd"] = "pnpm -C server run lint"
    r = client.post("/api/tasks", json=t, headers=auth(tokens.admin))
    assert r.status_code == 422 and "runs no test runner" in r.text
    t["spec"]["test_cmd"] = "pnpm -C server test"
    assert client.post("/api/tasks", json=t, headers=auth(tokens.admin)).status_code == 202


def test_the_gate_reads_the_tasks_own_base_branch(
    client: TestClient, tokens: Tokens, store: Store
) -> None:  # round 5, item 2
    svc = client.app.state.services.jobs  # type: ignore[attr-defined]
    asked: list[tuple[str, str]] = []
    pkg = {"server/package.json": '{"scripts": {"test": "vitest run"}}'}

    def files(repo: str, base: str) -> Any:
        asked.append((repo, base))
        return pkg.get

    svc.files = files
    t = _task()
    t["base"] = "develop"
    t["spec"]["test_cmd"] = "pnpm -C server test"
    assert client.post("/api/tasks", json=t, headers=auth(tokens.admin)).status_code == 202
    assert asked and all(b == "develop" for _, b in asked)


def test_an_unavailable_mirror_is_said_so(
    client: TestClient, tokens: Tokens, store: Store
) -> None:  # round 5, item 2
    from vigild.verifier import FileNotAvailable

    def files(repo: str, base: str) -> Any:
        def read(path: str) -> str | None:
            raise FileNotAvailable("the repo's mirror isn't available")

        return read

    client.app.state.services.jobs.files = files  # type: ignore[attr-defined]
    t = _task()
    t["spec"]["test_cmd"] = "pnpm -C server test"
    r = client.post("/api/tasks", json=t, headers=auth(tokens.admin))
    assert r.status_code == 422 and "mirror isn't available" in r.text
    assert "runs no test runner" not in r.text
