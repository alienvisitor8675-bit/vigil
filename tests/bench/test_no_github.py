"""Bench 1a: a bench job never reaches GitHub. Enforced at the App's token lease
(keyed off the job and off the `bench/` repo namespace), the publisher is a no-op,
and a whole pipeline with stubbed sessions makes no request at all."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.bench.conftest import BENCH, BENCH_REPO, NoGitHub
from vigild.db import Store
from vigild.github.app import BenchGitHubRefused, GitHubApp, TokenPolicyError
from vigild.jobs.model import JobState
from vigild.jobs.service import JobService, SubmitError
from vigild.workspace import Workspace, WorkspaceError


def _bench_job(store: Store) -> str:
    return store.create_job(
        repo=BENCH_REPO, base="main", title="t", request="r", notes=[], source="bench"
    ).id


def test_minting_for_a_bench_job_refuses(app: GitHubApp, store: Store, net: NoGitHub) -> None:
    job = _bench_job(store)
    perms = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    with pytest.raises(BenchGitHubRefused, match="bench"):
        app.mint("o/real", perms, job_id=job)  # even a real repo, asked for by a bench job
    with pytest.raises(BenchGitHubRefused), app.lease("o/real", perms, job_id=job):
        pass
    with pytest.raises(BenchGitHubRefused):  # the visibility check
        app.is_private("o/real", job_id=job)
    assert net.requests == []


def test_a_bench_repo_never_mints_whoever_asks(app: GitHubApp, net: NoGitHub) -> None:
    with pytest.raises(TokenPolicyError):
        app.mint(BENCH_REPO, {"contents": "read"})
    with pytest.raises(TokenPolicyError):
        app.is_private(BENCH_REPO)
    with pytest.raises(TokenPolicyError), app.lease(BENCH_REPO, {"issues": "read"}):
        pass
    assert net.requests == []


def test_an_unknown_job_fails_closed(app: GitHubApp, net: NoGitHub) -> None:
    with pytest.raises(BenchGitHubRefused):
        app.mint("o/real", {"contents": "read"}, job_id="nope-0001")
    assert net.requests == []


def test_the_repo_namespace_and_the_source_go_together(store: Store) -> None:
    with pytest.raises(ValueError, match="bench"):
        store.create_job(repo=BENCH_REPO, base="main", title="t", request="r", notes=[])
    with pytest.raises(ValueError, match="bench"):
        store.create_job(
            repo="o/real", base="main", title="t", request="r", notes=[], source="bench"
        )


def test_a_bench_repo_is_never_fetched_from_github(tmp_path: Path) -> None:
    asked: list[str] = []

    def token_for(repo: str) -> Any:
        asked.append(repo)
        raise AssertionError("a bench repo asked for a token")

    ws = Workspace(tmp_path / "data", remote_url=lambda r: "/nonexistent", token_for=token_for)
    with pytest.raises(WorkspaceError, match="imported"):
        ws.mirror(BENCH_REPO)
    assert asked == []


def test_a_bench_pipeline_makes_no_github_call(world: dict[str, Any], net: NoGitHub) -> None:
    jobs: JobService = world["jobs"]
    store: Store = world["store"]
    job = jobs.submit_bench(
        repo=BENCH_REPO, base="main", title="Document the rate limit",
        request="The limit is undocumented.", bench=BENCH,
    )  # fmt: skip
    world["runner"].poll_prs()  # the PR poll skips it too
    assert net.requests == []

    # coord -> gate -> approved by bench -> plan -> two workers -> review -> ready
    assert store.get_job(job.id).state is JobState.READY
    assert world["standin"].calls[0][0] == "coord"
    assert store.list_inbox() == []  # nothing for the owner
    states = [e for e in store.events(job_id=job.id) if e.type == "state"]
    approval = next(e for e in states if e.data["to"] == "approved")
    assert approval.data["actor"] == "vigild" and approval.data["evidence"]["bench"] is True
    assert approval.data["evidence"]["approval"] == "bench"
    assert json.loads(store.get_artifact(job.id, "approval.json").content)["approval"] == "bench"  # type: ignore[union-attr]
    # the spec gate's checks ran and were recorded
    assert json.loads(store.get_artifact(job.id, "spec_gate.json").content)["ok"] is True  # type: ignore[union-attr]

    # the publisher recorded why nothing was published
    nope = [e for e in store.events(job_id=job.id) if e.type == "bench_not_published"]
    assert len(nope) == 2 and all(e.data["reason"] == "not published (bench)" for e in nope)
    opened = [e for e in store.events(job_id=job.id) if e.type == "pr_open"]
    assert opened and all(e.data["pr_url"].startswith("bench:") for e in opened)

    # every row of the job is eval-only; its money came from the experiment line
    for table, col in (("events", "job_id"), ("artifacts", "job_id"), ("sessions", "job_id"),
                       ("tasks", "job_id"), ("jobs", "id")):  # fmt: skip
        rows = store.q(f"SELECT provenance FROM {table} WHERE {col}=?", (job.id,))  # noqa: S608
        assert rows and {r["provenance"] for r in rows} == {"eval-only"}, table
    assert {r["pool"] for r in store.q("SELECT pool FROM reservations")} == {"experiment"}
    assert world["budget"].spent_today() == 0.0
    assert net.requests == []


def test_a_bench_pipeline_cannot_be_started_over_rest(world: dict[str, Any]) -> None:
    jobs: JobService = world["jobs"]
    from vigild.jobs.service import JobRequest

    with pytest.raises(ValueError, match="bench"):
        JobRequest(repo="o/real", title="t", request="r", why="w", value=3, risk=1, source="bench")  # type: ignore[arg-type]
    req = JobRequest(repo=BENCH_REPO, title="t", request="r", why="w", value=3, risk=1)
    with pytest.raises(SubmitError, match="bench"):
        jobs.submit_request(req)


def test_the_no_op_publisher_records_the_reason(world: dict[str, Any]) -> None:
    from vigild.jobs.bench import BenchPublisher

    pub = BenchPublisher(world["runner"].workspace)
    assert pub.close(BENCH_REPO, "bench:not-published/x") is None  # nothing to close
    assert pub.records == [{"op": "close", "repo": BENCH_REPO, "reason": "not published (bench)"}]
