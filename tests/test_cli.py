"""The `vigil` CLI's own behaviour (phase 1 commands)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))

from vigil.main import app  # noqa: E402


def test_dev_image_closes_its_docker_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 2a, item 0: an unclosed client printed `ValueError: I/O operation
    on closed file` at exit."""
    import vigild.server as server

    closed: list[str] = []

    class Images:
        def ensure_fresh(self, repo: str, base: str, *, force: bool = False) -> str:
            return "vigil-dev/x:1"

        def latest(self, repo: str) -> dict[str, Any] | None:
            return None

    class Runner:
        dev_images = Images()
        session_images = None

        class workspace:
            @staticmethod
            def head_sha(repo: str, branch: str) -> str:
                return "a" * 40

        def close(self) -> None:
            closed.append("runner")

    monkeypatch.setattr(server, "build", lambda **kw: object())
    monkeypatch.setattr(server, "_github_app", lambda v: None)
    monkeypatch.setattr(server, "build_runner", lambda v, app: Runner())
    out = CliRunner().invoke(app, ["dev-image", "--repo", "o/x"])
    assert out.exit_code == 0, out.output
    assert closed == ["runner"]


# --- vigil kill (issue #52) -----------------------------------------------------------------


def _api(monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any]) -> list[Any]:
    """Point the CLI's API client at a stand-in vigild that answers `status`, `body`."""
    import httpx2
    import vigil.main as cli

    seen: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body)

    monkeypatch.setenv("VIGIL_API_TOKEN", "t" * 8)
    monkeypatch.setattr(
        cli,
        "_api_client",
        lambda: httpx2.Client(
            base_url="http://vigild.test", transport=httpx2.MockTransport(handler)
        ),
    )
    return seen


def test_kill_posts_to_the_api_as_the_cli_and_prints_what_it_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    seen = _api(monkeypatch, 200, {"job_id": "pa-0007", "killed": True, "already": None,
                                   "state": "abandoned", "message": "killed pa-0007 (was building)"})  # fmt: skip
    out = CliRunner().invoke(app, ["kill", "pa-0007"])
    assert out.exit_code == 0, out.output
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/api/jobs/pa-0007/kill"
    assert json.loads(req.content) == {"channel": "cli"}
    assert "killed pa-0007" in out.output


def test_kill_of_a_finished_job_says_so_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _api(monkeypatch, 200, {"job_id": "pa-0007", "killed": False, "already": "abandoned",
                            "state": "abandoned", "message": "pa-0007 is already abandoned; nothing to kill"})  # fmt: skip
    out = CliRunner().invoke(app, ["kill", "pa-0007"])
    assert out.exit_code == 0 and "already abandoned" in out.output


@pytest.mark.parametrize("status", [404, 409, 401])
def test_kill_fails_on_an_api_error(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _api(monkeypatch, status, {"detail": "no such job"})
    out = CliRunner().invoke(app, ["kill", "nope"])
    assert out.exit_code == 1 and str(status) in out.output


# --- vigil batch (issue #54) ----------------------------------------------------------------

BATCH_YAML = """\
name: docs sweep
jobs:
  - repo: o/r
    title: First
    request: Do the first thing.
    why: because
    value: 3
    risk: 1
  - repo: o/r
    title: Second
    request: Do the second thing.
    why: because
    value: 2
    risk: 1
    issue: 12
    notes: [keep it short]
"""


def test_batch_submit_parses_the_yaml_and_posts_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import json

    f = tmp_path / "b.yaml"
    f.write_text(BATCH_YAML)
    seen = _api(
        monkeypatch, 202, {"batch_id": "b-0001", "name": "docs sweep", "jobs": ["r-0001", "r-0002"]}
    )
    out = CliRunner().invoke(app, ["batch", "submit", str(f)])
    assert out.exit_code == 0, out.output
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/api/batch"
    body = json.loads(req.content)
    assert body["name"] == "docs sweep" and [j["title"] for j in body["jobs"]] == [
        "First",
        "Second",
    ]
    assert body["jobs"][1]["issue"] == 12 and body["jobs"][1]["notes"] == ["keep it short"]
    assert "b-0001" in out.output and "r-0002" in out.output


def test_batch_submit_refusal_names_the_job_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    f = tmp_path / "b.yaml"
    f.write_text(BATCH_YAML)
    _api(monkeypatch, 422, {"detail": {"errors": ["jobs[1]: no such repo"]}})
    out = CliRunner().invoke(app, ["batch", "submit", str(f)])
    assert out.exit_code == 1 and "jobs[1]: no such repo" in out.output


def test_batch_submit_rejects_a_file_that_is_not_a_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    seen = _api(monkeypatch, 202, {})
    for text in ("- just\n- a list\n", "name: x\n", "name: x\njobs: nope\n", "{{{"):
        f = tmp_path / "bad.yaml"
        f.write_text(text)
        out = CliRunner().invoke(app, ["batch", "submit", str(f)])
        assert out.exit_code == 1, text
    assert CliRunner().invoke(app, ["batch", "submit", str(tmp_path / "none.yaml")]).exit_code == 1
    assert seen == []  # nothing was sent


def test_batch_status_prints_each_job_and_the_total(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "batch_id": "b-0001", "name": "docs sweep", "created_at": "t",
        "jobs": [
            {"job_id": "r-0001", "title": "First", "state": "building", "spent_usd": 0.25, "held_usd": 0.5, "total_usd": 0.75},
            {"job_id": "r-0002", "title": "Second", "state": "spec_pending", "spent_usd": 0.0, "held_usd": 0.0, "total_usd": 0.0},
        ],
        "cost": {"spent_usd": 0.25, "held_usd": 0.5, "total_usd": 0.75},
    }  # fmt: skip
    seen = _api(monkeypatch, 200, body)
    out = CliRunner().invoke(app, ["batch", "status", "b-0001"])
    assert out.exit_code == 0, out.output
    assert seen[0].method == "GET" and seen[0].url.path == "/api/batch/b-0001"
    assert "r-0001" in out.output and "building" in out.output and "0.75" in out.output
    assert "docs sweep" in out.output


def test_batch_status_of_an_unknown_batch_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _api(monkeypatch, 404, {"detail": "no such batch"})
    assert CliRunner().invoke(app, ["batch", "status", "b-9"]).exit_code == 1


def test_batch_status_refuses_an_id_that_is_not_a_batch_id_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _api(monkeypatch, 200, {})
    for bad in ("../x", "b-1/../../jobs", "b-0001?x=1", "x", "b-", "B-1", ""):
        out = CliRunner().invoke(app, ["batch", "status", bad])
        assert out.exit_code == 1, bad
    assert seen == []
