"""`vigil hidden show <task>`: the owner's dispute path, on the host. It reads
vigild's store and vault directly (VIGIL_DATA_DIR honoured), prints the hidden
tests, and records `hidden_tests_shown` (provenance human). No API route or
MCP tool serves it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.harness import finish_session
from vigild.db import Store
from vigild.provenance import Provenance
from vigild.workspace import Workspace

REPO = "o/fixture"


@pytest.fixture
def data(tmp_path: Path, fixture_repo: dict[str, Path]) -> Path:
    """A vigild data dir: a store with a job, a task and a hidden_tests artifact
    whose tests sit in the vault."""
    root = tmp_path / "vigil-data"
    root.mkdir()
    store = Store.open(root / "vigil.db")
    store.create_job(repo=REPO, base="main", title="t", request="r", notes=[], source="human")
    job = store.list_jobs()[0]
    task = f"{job.id}-w1"
    folder = f".vigil/{job.id}/hidden/{task}"
    store.create_task(job.id, {"task_id": task, "spec": {}}, Provenance.CLAUDE)
    ws = Workspace(root, remote_url=lambda r: str(fixture_repo["bare"]))
    clone = ws.clone(REPO, f"{task}-ht1", branch=f"vigil/{task}-hidden1", base="main")
    base = ws.base_sha(f"{task}-ht1")
    (clone / folder).mkdir(parents=True)
    (clone / folder / "test_hidden.py").write_text("def test_it():\n    assert 1 == 2\n")
    out = tmp_path / "out"
    r = finish_session(clone, out, branch=f"vigil/{task}-hidden1", base_sha=base)
    sha = ws.import_result(REPO, f"{task}-ht1", out, f"vigil/{task}-hidden1", base, head=r["head"])
    entry = {
        "status": "ok",
        "sha": sha,
        "prefix": folder,
        "files": [f"{folder}/test_hidden.py"],
        "test_cmd": "pytest -q x",
        "tests": [{"name": "it fails first", "category": "regression"}],
    }
    store.add_artifact(
        job.id,
        "hidden_tests.json",
        json.dumps({"provenance": "claude", "tasks": {f"{job.id}-w1": entry}}),
        Provenance.CLAUDE,
    )
    (root / "ids").write_text(f"{job.id}\n")  # for the test to read
    return root


def _run(data: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str]:
    monkeypatch.setenv("VIGIL_DATA_DIR", str(data))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
    from vigil.main import app

    res = CliRunner().invoke(app, ["hidden", *args])
    return res.exit_code, res.output


def test_show_prints_the_tests_and_records_that_a_human_saw_them(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = (data / "ids").read_text().strip()
    code, out = _run(data, monkeypatch, "show", f"{job}-w1")
    assert code == 0, out
    assert "[regression] it fails first" in out and "assert 1 == 2" in out
    store = Store.open(data / "vigil.db")
    rows = store.q("SELECT type, provenance, data FROM events WHERE type='hidden_tests_shown'")
    assert [(r["provenance"]) for r in rows] == ["human"]
    assert json.loads(rows[0]["data"])["task"] == f"{job}-w1"


def test_show_for_a_task_without_hidden_tests_fails_and_records_nothing(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _run(data, monkeypatch, "show", "nope-0001-w1")
    assert code == 1 and "no task" in out
    store = Store.open(data / "vigil.db")
    assert store.q("SELECT 1 FROM events WHERE type='hidden_tests_shown'") == []


def test_show_without_vigild_data_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = _run(tmp_path / "empty", monkeypatch, "show", "x-0001-w1")
    assert code == 1 and "no vigild data" in out


def test_the_repo_has_no_api_or_mcp_surface_for_hidden_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in (root / "vigild" / "api").rglob("*.py"):
        text = path.read_text()
        assert "hidden_tests" not in text and "hidden show" not in text, path
