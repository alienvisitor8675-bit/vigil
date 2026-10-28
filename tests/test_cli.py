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
