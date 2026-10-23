"""Stage artifacts live under .vigil/<job>/ and always carry provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vigild.jobs.artifacts import (
    ArtifactError,
    artifact_path,
    read_artifact,
    read_worker_report,
    write_artifact,
)
from vigild.provenance import Provenance


def test_json_artifact_gets_provenance(tmp_path: Path) -> None:
    p = write_artifact(tmp_path, "pa-0001", "intake.json", {"value": 3}, Provenance.CLAUDE)
    assert p == tmp_path / ".vigil" / "pa-0001" / "intake.json"
    assert json.loads(p.read_text())["provenance"] == "claude"


def test_markdown_artifact_gets_a_provenance_line(tmp_path: Path) -> None:
    p = write_artifact(tmp_path, "pa-0001", "spec.md", "# Spec\n", Provenance.CLAUDE)
    assert p.read_text().startswith("<!-- provenance: claude -->\n")
    text, prov = read_artifact(tmp_path, "pa-0001", "spec.md")
    assert prov is Provenance.CLAUDE and text.startswith("# Spec")


def test_worker_artifacts_are_per_task(tmp_path: Path) -> None:
    p = artifact_path(tmp_path, "pa-0001", "worker_report.json", task_id="pa-0001-w2")
    assert p == tmp_path / ".vigil" / "pa-0001" / "pa-0001-w2" / "worker_report.json"


def test_unknown_artifact_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError):
        write_artifact(tmp_path, "pa-0001", "notes.txt", "x", Provenance.HUMAN)


def test_missing_provenance_is_invalid(tmp_path: Path) -> None:
    d = tmp_path / ".vigil" / "pa-0001"
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# no provenance line\n")
    with pytest.raises(ArtifactError, match="provenance"):
        read_artifact(tmp_path, "pa-0001", "spec.md")


def test_worker_report_must_validate(tmp_path: Path) -> None:
    d = tmp_path / ".vigil" / "pa-0001" / "pa-0001-w1"
    d.mkdir(parents=True)
    (d / "worker_report.json").write_text(
        json.dumps({"task_id": "pa-0001-w1", "provenance": "claude"})
    )
    with pytest.raises(ArtifactError, match="worker_report"):
        read_worker_report(tmp_path, "pa-0001", "pa-0001-w1")


def test_missing_worker_report_is_a_failure(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="missing"):
        read_worker_report(tmp_path, "pa-0001", "pa-0001-w1")


def test_job_id_cannot_escape_the_vigil_dir(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError):
        artifact_path(tmp_path, "../etc", "spec.md")
