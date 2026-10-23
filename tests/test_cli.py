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
