"""No Claude-generated text can enter a training export."""

from __future__ import annotations

import sqlite3

import pytest

from vigild.db import Store
from vigild.jobs.model import Role
from vigild.provenance import ProvenanceError, check_exportable, export_training_rows

CLAUDE_FIXTURE = {"id": 1, "text": "a diff Claude wrote", "provenance": "claude"}
LOCAL_FIXTURE = {"id": 2, "text": "a diff the tower wrote", "provenance": "local"}


def test_claude_tagged_fixture_is_rejected_by_the_exporter() -> None:
    with pytest.raises(ProvenanceError):
        list(export_training_rows([LOCAL_FIXTURE, CLAUDE_FIXTURE]))
    with pytest.raises(ProvenanceError):
        check_exportable(CLAUDE_FIXTURE)


def test_untagged_or_unknown_rows_are_rejected() -> None:
    with pytest.raises(ProvenanceError):
        check_exportable({"text": "x"})
    with pytest.raises(ProvenanceError):
        list(export_training_rows([{"text": "x", "provenance": "maybe"}]))


def test_exporter_takes_local_only() -> None:
    rows = [LOCAL_FIXTURE, {"id": 3, "text": "your words", "provenance": "human"}]
    assert list(export_training_rows(rows)) == [LOCAL_FIXTURE]


@pytest.mark.parametrize(
    "table",
    [
        "jobs",
        "tasks",
        "sessions",
        "events",
        "artifacts",
        "inbox_items",
        "approvals",
        "model_calls",
        "decisions",
    ],
)
def test_every_record_table_requires_provenance(store: Store, table: str) -> None:
    cols = [r["name"] for r in store.q(f"PRAGMA table_info({table})")]
    assert "provenance" in cols
    info = {r["name"]: r for r in store.q(f"PRAGMA table_info({table})")}
    assert info["provenance"]["notnull"] == 1


def test_db_refuses_an_unknown_provenance(store: Store) -> None:
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    with pytest.raises(sqlite3.IntegrityError):
        store.x(
            "INSERT INTO artifacts (job_id, name, content, sha256, created_at, provenance)"
            " VALUES (?,?,?,?,?,?)",
            (job.id, "spec.md", "x", "0", "now", "gpt"),
        )
    with pytest.raises(ValueError):
        store.add_artifact(job.id, "spec.md", "x", "unknown")


def test_stored_artifacts_and_sessions_carry_provenance(store: Store) -> None:
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    store.add_artifact(job.id, "spec.md", "# s", "claude")
    s = store.create_session(job.id, Role.WORKER)
    assert store.list_artifacts(job.id)[0].provenance == "claude"
    assert s.provenance == "claude"
