"""`vigil review replay-set` (#47): asks the running vigild and prints recall and cost."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))

from vigil.main import app


def test_review_replay_set_prints_recall_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2

    from vigild import config

    seen: dict[str, Any] = {}
    report = {
        "prompt_version": "feat/x", "prompt_sha256": "ab" * 32, "expected": 4, "found": 3,
        "recall": 0.75, "cost_usd": 0.62,
        "cases": [{"case": "pa-0003-first-pass", "cost_usd": 0.3, "replay_id": "rp-1",
                   "expected": [{"ac": "AC7", "found": True}, {"ac": "AC1", "found": False}]}],
    }  # fmt: skip

    class Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return report

    def post(url: str, **kw: Any) -> Resp:
        seen.update(url=url, **kw)
        return Resp()

    monkeypatch.setattr(httpx2, "post", post)
    monkeypatch.setattr(config, "load_secrets", lambda: config.Secrets({"VIGIL_API_TOKEN": "t"}))
    result = CliRunner().invoke(app, ["review", "replay-set", "--prompt-version", "feat/x"])
    assert result.exit_code == 0, result.output
    assert "recall 0.75 (3 of 4" in result.output and "$0.6200" in result.output
    assert "AC1:MISSED" in result.output and "AC7:found" in result.output
    assert seen["url"].endswith("/api/review/replay-set")
    assert seen["json"] == {"prompt_version": "feat/x"}


def test_a_refusal_is_printed_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2

    from vigild import config

    class Resp:
        status_code = 409
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {"detail": "a breaker is open: daily_cap:global"}

    monkeypatch.setattr(httpx2, "post", lambda url, **kw: Resp())
    monkeypatch.setattr(config, "load_secrets", lambda: config.Secrets({"VIGIL_API_TOKEN": "t"}))
    result = CliRunner().invoke(app, ["review", "replay-set", "--prompt-version", "main"])
    assert result.exit_code == 1 and "a breaker is open" in result.output
