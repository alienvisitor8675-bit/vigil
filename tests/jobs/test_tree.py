"""Jobs as trees: coord, workers, subagents, reviewer, auditor; fix-job lineage."""

from __future__ import annotations

import pytest

from vigild.db import Store
from vigild.jobs.model import FixDepthExceeded, Role
from vigild.jobs.tree import job_tree


@pytest.fixture
def store() -> Store:
    return Store.open(":memory:")


def test_tree_nests_sessions_under_their_parents(store: Store) -> None:
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    coord = store.create_session(job.id, Role.COORD, backend_preset="claude-opus")
    t1 = store.create_task(job.id, {"task_id": f"{job.id}-w1"}, "claude")
    t2 = store.create_task(job.id, {"task_id": f"{job.id}-w2"}, "claude")
    w1 = store.create_session(
        job.id,
        Role.WORKER,
        task_id=t1.id,
        parent_session_id=coord.id,
        backend_preset="claude-sonnet",
    )
    store.create_session(
        job.id,
        Role.WORKER,
        task_id=t2.id,
        parent_session_id=coord.id,
        backend_preset="claude-sonnet",
    )
    store.create_session(
        job.id, Role.SUBAGENT, parent_session_id=w1.id, backend_preset="claude-sonnet"
    )
    rv = store.create_session(
        job.id,
        Role.REVIEWER,
        task_id=t1.id,
        parent_session_id=w1.id,
        backend_preset="claude-sonnet",
    )
    store.create_session(
        job.id, Role.AUDITOR, task_id=t1.id, parent_session_id=rv.id, backend_preset="claude-opus"
    )

    tree = job_tree(store, job.id)
    assert tree["job"]["id"] == job.id and tree["job"]["state"] == "spec_pending"
    (c,) = tree["sessions"]
    assert c["role"] == "coord"
    roles = sorted(n["role"] for n in c["children"])
    assert roles == ["worker", "worker"]
    w = next(n for n in c["children"] if n["id"] == w1.id)
    assert sorted(n["role"] for n in w["children"]) == ["reviewer", "subagent"]
    reviewer = next(n for n in w["children"] if n["role"] == "reviewer")
    assert reviewer["children"][0]["role"] == "auditor"
    for key in (
        "status",
        "cost_usd",
        "tokens_in",
        "tokens_out",
        "pr_url",
        "backend_preset",
        "task_id",
    ):
        assert key in w


def test_fix_job_lineage_is_shown_on_both_jobs(store: Store) -> None:
    parent = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    fix = store.create_job(
        repo="o/r", base="main", title="fix", request="ci red", notes=[], parent_job_id=parent.id
    )
    assert fix.fix_depth == 1 and fix.parent_job_id == parent.id
    assert job_tree(store, fix.id)["lineage"]["parent"] == parent.id
    assert job_tree(store, parent.id)["lineage"]["children"] == [fix.id]


def test_fix_job_depth_is_capped_at_three(store: Store) -> None:
    j = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    for _ in range(3):
        j = store.create_job(
            repo="o/r", base="main", title="fix", request="r", notes=[], parent_job_id=j.id
        )
    assert j.fix_depth == 3
    with pytest.raises(FixDepthExceeded):
        store.create_job(
            repo="o/r", base="main", title="fix", request="r", notes=[], parent_job_id=j.id
        )


def test_tree_includes_artifacts_with_provenance(store: Store) -> None:
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    store.add_artifact(job.id, "spec.md", "# s", "claude")
    tree = job_tree(store, job.id)
    assert tree["artifacts"] == [
        {
            "name": "spec.md",
            "task_id": None,
            "provenance": "claude",
            "sha256": tree["artifacts"][0]["sha256"],
        }
    ]
