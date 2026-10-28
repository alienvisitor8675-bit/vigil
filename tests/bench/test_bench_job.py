"""Bench 1a: the bench job kind, its guarded approval edge, and how a run ends when
the spec gate or the experiment line says no (no owner step, a failure tag)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.bench.conftest import BENCH, BENCH_REPO
from tests.jobs.test_coord_loop import SPEC, plan
from vigild.contract.models import BenchInfo, Source
from vigild.db import Store
from vigild.jobs.fsm import Actor, TransitionError, transition
from vigild.jobs.model import JobState
from vigild.jobs.service import JobService, SubmitError
from vigild.provenance import Provenance

S = JobState


def _drafted(store: Store, source: str, repo: str) -> str:
    job = store.create_job(repo=repo, base="main", title="t", request="r", notes=[], source=source)
    store.add_artifact(job.id, "spec.md", "# spec", Provenance.CLAUDE)
    store.add_artifact(job.id, "plan.md", "# plan", Provenance.CLAUDE)
    # the gate ran (it may have failed: bench measures spec quality)
    store.add_artifact(
        job.id, "spec_gate.json", json.dumps({"ok": False, "errors": ["x"]}), Provenance.HUMAN
    )
    return job.id


def test_source_and_request_fields() -> None:
    assert Source("bench") is Source.BENCH
    assert BENCH.timeout_scale == 1.0 and BENCH.emulated is False
    assert {"run_id", "task_id", "dataset", "config_hash", "emulated", "timeout_scale"} <= set(
        BenchInfo.model_fields
    )
    with pytest.raises(ValidationError):
        BenchInfo(run_id="r", task_id="t", dataset="nope", config_hash="h", emulated=False)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BenchInfo(
            run_id="r", task_id="t", dataset="parkagent", config_hash="h", emulated=True,
            timeout_scale=0,
        )  # fmt: skip
    for ds in ("polybench", "swebench-pro", "parkagent"):
        BenchInfo(run_id="r", task_id="t", dataset=ds, config_hash="h", emulated=True)  # type: ignore[arg-type]


def test_a_bench_job_goes_from_draft_to_the_next_stage_by_vigild(store: Store) -> None:
    jid = _drafted(store, "bench", BENCH_REPO)
    job = transition(
        store, jid, S.APPROVED, Actor.VIGILD, evidence={"bench": True, "approval": "bench"}
    )
    assert job.state is S.APPROVED
    ev = store.events(job_id=jid)[-1]
    assert ev.data["actor"] == "vigild" and ev.data["evidence"]["approval"] == "bench"
    assert ev.provenance == "eval-only"


@pytest.mark.parametrize(
    "actor, evidence",
    [
        (Actor.OWNER, {"bench": True}),  # the owner has no part in a bench job's edge
        (Actor.COORD, {"bench": True}),
        (Actor.VIGILD, {}),
        (Actor.VIGILD, {"bench": False}),
        (Actor.VIGILD, {"bench": "yes"}),
    ],
)
def test_the_bench_edge_has_a_guard(store: Store, actor: Actor, evidence: dict[str, Any]) -> None:
    jid = _drafted(store, "bench", BENCH_REPO)
    with pytest.raises(TransitionError):
        transition(store, jid, S.APPROVED, actor, evidence=evidence)
    assert store.get_job(jid).state is S.SPEC_PENDING


def test_a_bench_edge_needs_the_draft(store: Store) -> None:
    job = store.create_job(
        repo=BENCH_REPO, base="main", title="t", request="r", notes=[], source="bench"
    )
    with pytest.raises(TransitionError, match=r"spec\.md"):
        transition(store, job.id, S.APPROVED, Actor.VIGILD, evidence={"bench": True})


@pytest.mark.parametrize("source", ["human", "self"])
@pytest.mark.parametrize("force", [False, True])
def test_a_non_bench_job_can_never_take_it(store: Store, source: str, force: bool) -> None:
    jid = _drafted(store, source, "o/r")
    for actor in (Actor.VIGILD, Actor.COORD, Actor.OWNER):
        for channel in (None, "inbox"):
            with pytest.raises(TransitionError):
                transition(
                    store, jid, S.APPROVED, actor, channel=channel, force=force,
                    evidence={"bench": True, "approval": "bench"},
                )  # fmt: skip
    assert store.get_job(jid).state is S.SPEC_PENDING


def test_submit_bench_stores_the_block(world: dict[str, Any]) -> None:
    jobs: JobService = world["jobs"]
    store: Store = world["store"]
    job = jobs.submit_bench(
        repo=BENCH_REPO, base="main", title="t", request="r",
        bench=BENCH.model_copy(update={"timeout_scale": 3.0, "emulated": True}),
    )  # fmt: skip
    assert job.source == "bench" and job.provenance == "eval-only"
    block = json.loads(store.get_artifact(job.id, "bench.json").content)  # type: ignore[union-attr]
    assert block == {
        "run_id": "run-1", "task_id": "inst-1", "dataset": "polybench",
        "config_hash": "abc123", "emulated": True, "timeout_scale": 3.0,
    }  # fmt: skip


def test_submit_bench_refuses_what_is_not_a_bench_repo(world: dict[str, Any]) -> None:
    jobs: JobService = world["jobs"]
    with pytest.raises(SubmitError, match="bench/"):
        jobs.submit_bench(repo="o/real", base="main", title="t", request="r", bench=BENCH)
    with pytest.raises(SubmitError):  # not in configs/repos.yaml
        jobs.submit_bench(repo="bench/unknown", base="main", title="t", request="r", bench=BENCH)


def test_there_is_no_rest_route_for_bench(services: Any) -> None:
    from vigild.api.app import create_app

    paths = [getattr(r, "path", "") for r in create_app(services).routes]
    assert not [p for p in paths if "bench" in p]


def test_a_gate_failure_is_tagged_and_the_run_continues(world: dict[str, Any]) -> None:
    store: Store = world["store"]

    def bad(j: str) -> tuple[str, str]:
        return SPEC, plan(j).replace(
            '"test_cmd": "pytest -q tests/test_runbook.py"', '"test_cmd": ""'
        )

    world["standin"].coord_drafts = [bad, bad, bad]
    job = world["jobs"].submit_bench(
        repo=BENCH_REPO, base="main", title="t", request="r", bench=BENCH
    )
    gate = json.loads(store.get_artifact(job.id, "spec_gate.json").content)  # type: ignore[union-attr]
    assert gate["ok"] is False and gate["errors"]  # the checks ran and were recorded
    tags = [e for e in store.events(job_id=job.id) if e.type == "bench_failure"]
    assert [t.data["tag"] for t in tags] == ["spec"]
    states = [e.data["to"] for e in store.events(job_id=job.id) if e.type == "state"]
    assert "approved" in states  # no owner step: the run went on
    assert store.list_inbox() == []


def test_a_full_experiment_line_ends_the_run_with_the_budget_tag(world: dict[str, Any]) -> None:
    store: Store = world["store"]
    world["jobs"].cfg.budgets.experiment_monthly_usd = 0.5  # the coord reserves $2.00
    job = world["jobs"].submit_bench(
        repo=BENCH_REPO, base="main", title="t", request="r", bench=BENCH
    )
    tags = [e for e in store.events(job_id=job.id) if e.type == "bench_failure"]
    assert [t.data["tag"] for t in tags] == ["budget"]
    assert tags[0].data["cap"] == "experiment"
    assert store.list_inbox() == []  # a bench run pages nobody
    assert [e for e in store.events(job_id=job.id) if e.type in ("queued", "dequeued")] == []
    assert world["standin"].calls == []  # no session started
