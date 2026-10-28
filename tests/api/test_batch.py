"""Batch (issue #54): `POST /api/batch` makes N ordinary jobs that share a batch_id,
all or none; `GET /api/batch/{id}` rolls their state and cost up; a kill on a batch is
the job kill, per job. The scheduler and the WIP limit are not touched."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import RecordingRunner, auth
from vigild.db import Store
from vigild.jobs.model import JobState
from vigild.scheduler import wip
from vigild.services import Tokens

PA = "thomasbardhi01/parkagent"


def _job(n: int = 1, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repo": PA,
        "title": f"Document limit {n}",
        "request": "The rate limits are undocumented.",
        "why": "support asks weekly",
        "value": 3,
        "risk": 1,
    }
    return body | over


def _post(
    client: TestClient, tokens: Tokens, jobs: list[dict[str, Any]], name: str = "docs"
) -> Any:
    return client.post("/api/batch", json={"name": name, "jobs": jobs}, headers=auth(tokens.admin))


def test_every_job_of_a_batch_carries_its_batch_id(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:
    r = _post(client, tokens, [_job(1), _job(2), _job(3)])
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["name"] == "docs" and len(body["jobs"]) == 3
    for jid in body["jobs"]:
        j = client.get(f"/api/jobs/{jid}", headers=auth(tokens.read)).json()
        assert j["batch_id"] == body["batch_id"] and j["state"] == "spec_pending"
    assert runner.created == body["jobs"]  # each one's coord starts, as for POST /api/jobs
    # an ordinary job is in no batch
    solo = client.post("/api/jobs", json=_job(9), headers=auth(tokens.admin)).json()
    assert solo["batch_id"] is None


def test_one_bad_job_creates_nothing_and_is_named(
    client: TestClient, tokens: Tokens, store: Store, runner: RecordingRunner
) -> None:
    bad = _job(2, repo="nobody/unknown")
    r = _post(client, tokens, [_job(1), bad, _job(3)])
    assert r.status_code == 422
    assert "jobs[1]" in str(r.json()["detail"]["errors"])
    assert store.list_jobs() == [] and runner.created == []
    assert store.q("SELECT 1 FROM batches") == []


def test_a_scoring_refusal_and_a_repeated_issue_are_named_too(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    r = _post(client, tokens, [_job(1, issue=7), _job(2, issue=7)])
    assert r.status_code == 422
    assert "jobs[1]" in str(r.json()["detail"]["errors"]) and "issue 7" in str(r.json())
    held = client.post("/api/jobs", json=_job(5, issue=8), headers=auth(tokens.admin)).json()
    r = _post(client, tokens, [_job(1), _job(2, issue=8)])
    assert r.status_code == 422 and held["id"] in str(r.json()["detail"]["errors"])
    assert len(store.list_jobs()) == 1  # only the one made on its own


def test_the_body_is_checked(client: TestClient, tokens: Tokens) -> None:
    assert _post(client, tokens, []).status_code == 422
    assert _post(client, tokens, [_job(n) for n in range(11)]).status_code == 422
    r = client.post("/api/batch", json={"jobs": [_job()]}, headers=auth(tokens.admin))
    assert r.status_code == 422  # a batch has a name


def test_post_needs_admin_and_get_needs_read(client: TestClient, tokens: Tokens) -> None:
    body = {"name": "x", "jobs": [_job()]}
    assert client.post("/api/batch", json=body, headers=auth(tokens.read)).status_code == 403
    assert client.post("/api/batch", json=body).status_code in (401, 403)
    assert client.get("/api/batch/b-0001").status_code in (401, 403)
    assert client.get("/api/batch/b-0099", headers=auth(tokens.read)).status_code == 404


def test_the_roll_up_adds_up(client: TestClient, tokens: Tokens, store: Store) -> None:
    ids = _post(client, tokens, [_job(1), _job(2)]).json()
    a, b = ids["jobs"]
    now = "2026-10-28T00:00:00+00:00"
    for jid, sess, spent, held in ((a, "s-a", 0.25, 0.5), (b, "s-b", 0.125, 0.0)):
        store.x(
            "INSERT INTO sessions (id, job_id, role, status, created_at, provenance)"
            " VALUES (?,?,?,?,?,?)",
            (sess, jid, "coord", "done", now, "local"),
        )
        store.x(
            "INSERT INTO ledger (session_id, amount_usd, day, month, ts) VALUES (?,?,?,?,?)",
            (sess, spent, "2026-10-28", "2026-10", now),
        )
        if held:
            store.x(
                "INSERT INTO reservations (session_id, purpose, amount_usd, status, day, month,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (sess, "coord", held, "open", "2026-10-28", "2026-10", now),
            )
        # a settled or released hold is not "held"
        store.x(
            "INSERT INTO reservations (session_id, purpose, amount_usd, status, day, month,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (sess, "coord", 9.0, "settled", "2026-10-28", "2026-10", now),
        )
    r = client.get(f"/api/batch/{ids['batch_id']}", headers=auth(tokens.read))
    assert r.status_code == 200, r.text
    out = r.json()
    per = {j["job_id"]: j for j in out["jobs"]}
    assert per[a]["spent_usd"] == 0.25 and per[a]["held_usd"] == 0.5
    assert per[a]["total_usd"] == 0.75 and per[b]["total_usd"] == 0.125
    assert per[a]["state"] == "spec_pending"
    assert out["cost"] == {"spent_usd": 0.375, "held_usd": 0.5, "total_usd": 0.875}
    assert out["name"] == "docs" and out["batch_id"] == ids["batch_id"]
    # a job outside the batch is not counted
    assert sum(j["total_usd"] for j in out["jobs"]) == out["cost"]["total_usd"]


def test_kill_on_a_batch_kills_every_job_through_the_job_kill(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    ids = _post(client, tokens, [_job(1), _job(2), _job(3)]).json()
    other = client.post("/api/jobs", json=_job(9), headers=auth(tokens.admin)).json()["id"]
    r = client.post(
        f"/api/batch/{ids['batch_id']}/kill", json={"channel": "cli"}, headers=auth(tokens.admin)
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert [j["job_id"] for j in out["jobs"]] == ids["jobs"]
    assert all(j["killed"] is True for j in out["jobs"])
    for jid in ids["jobs"]:
        assert store.get_job(jid).state is JobState.ABANDONED
        (ev,) = [e for e in store.events(job_id=jid) if e.type == "job_killed"]
        assert ev.data["channel"] == "cli"
    assert store.get_job(other).state is JobState.SPEC_PENDING  # not in the batch
    again = client.post(f"/api/batch/{ids['batch_id']}/kill", headers=auth(tokens.admin))
    assert again.status_code == 200
    assert all(j["already"] == "abandoned" for j in again.json()["jobs"])  # idempotent


def test_kill_on_a_batch_needs_admin_and_a_known_batch(client: TestClient, tokens: Tokens) -> None:
    ids = _post(client, tokens, [_job()]).json()
    assert (
        client.post(f"/api/batch/{ids['batch_id']}/kill", headers=auth(tokens.read)).status_code
        == 403
    )
    assert client.post("/api/batch/b-0099/kill", headers=auth(tokens.admin)).status_code == 404
    r = client.post(
        f"/api/batch/{ids['batch_id']}/kill", json={"channel": "email"}, headers=auth(tokens.admin)
    )
    assert r.status_code == 422


def test_a_batch_larger_than_the_wip_limit_leaves_the_wip_alone(
    client: TestClient, tokens: Tokens, store: Store, cfg: Any
) -> None:
    limit = cfg.budgets.wip_per_repo
    assert limit == 4
    before = wip.room(store, PA, limit)
    r = _post(client, tokens, [_job(n) for n in range(limit + 2)])
    assert r.status_code == 202, r.text  # the batch is not capped at the limit
    assert wip.stack(store, PA) == [] and wip.room(store, PA, limit) == before == limit
    # nothing the batch adds changes how many places a repo has
    assert cfg.budgets.wip_per_repo == limit


def test_a_batch_submitted_over_mcp_logs_one_mcp_call(
    client: TestClient, tokens: Tokens, store: Store
) -> None:
    h = auth(tokens.admin) | {"X-Vigil-Channel": "mcp"}
    r = client.post("/api/batch", json={"name": "m", "jobs": [_job()]}, headers=h)
    assert r.status_code == 202
    (ev,) = [e for e in store.events() if e.type == "mcp_call"]
    assert ev.data["route"] == "/api/batch" and ev.data["status"] == 202


def test_kill_on_a_batch_is_200_with_a_failed_list_when_one_job_fails(
    client: TestClient, tokens: Tokens, store: Store, monkeypatch: Any
) -> None:
    from vigild.jobs.service import JobService, KillFailed

    ids = _post(client, tokens, [_job(1), _job(2), _job(3)]).json()
    a, b, c = ids["jobs"]
    real = JobService.kill

    def kill(self: JobService, job_id: str, *args: Any, **kw: Any) -> Any:
        if job_id == b:
            raise KillFailed("runner.kill_job", "boom")
        return real(self, job_id, *args, **kw)

    monkeypatch.setattr(JobService, "kill", kill)
    r = client.post(f"/api/batch/{ids['batch_id']}/kill", headers=auth(tokens.admin))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["failed"] == [b] and [j["job_id"] for j in out["jobs"]] == [a, b, c]
    assert "error" in out["jobs"][1] and out["jobs"][0]["killed"] and out["jobs"][2]["killed"]
    assert store.get_job(a).state is JobState.ABANDONED
    assert store.get_job(c).state is JobState.ABANDONED
