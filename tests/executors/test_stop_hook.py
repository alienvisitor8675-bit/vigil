"""The worker's Stop hook (plan 2a, item 6), run as the CLI runs it: a
command from managed settings, the hook's input on stdin, its answer on stdout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "images" / "claude-executor" / "stop_hook.py"
SCHEMA = ROOT / "schemas" / "worker_report.schema.json"
JOB, TASK = "fx-0001", "fx-0001-w1"
REPORT = {
    "task_id": TASK, "job_id": JOB, "files_changed": ["a.py"], "summary": "done",
    "self_check": {"test_failed_first": True, "outside_scope": [], "not_done": "nothing",
                   "reviewer_would_flag": "-", "could_delete": "-"},
    "provenance": "claude",
}  # fmt: skip


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    (w / ".vigil" / JOB / TASK).mkdir(parents=True)
    (w / ".vigil" / "task.json").write_text(
        json.dumps({"job_id": JOB, "task_id": TASK, "spec": {"test_cmd": "test -f ok.txt"}})
    )
    return w


def _report(work: Path, doc: dict[str, Any] | None = REPORT) -> None:
    (work / ".vigil" / JOB / TASK / "worker_report.json").write_text(json.dumps(doc))


def _stop(
    work: Path, *, role: str = "worker", turns: int = 0, max_turns: int = 60
) -> dict[str, Any]:
    transcript = work.parent / "t.jsonl"
    transcript.write_text("".join(
        json.dumps({"type": "assistant", "message": {"id": f"m{i}"}}) + "\n" for i in range(turns)
    ))  # fmt: skip
    env = {
        **os.environ,
        "VIGIL_WORK": str(work),
        "VIGIL_STOP_STATE": str(work.parent / "state.json"),
        "VIGIL_REPORT_SCHEMA": str(SCHEMA),
        "VIGIL_ROLE": role,
        "VIGIL_MAX_TURNS": str(max_turns),
    }
    hook_input = {"session_id": "s", "transcript_path": str(transcript), "stop_hook_active": False}
    p = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(hook_input), env=env,
        capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    return json.loads(p.stdout) if p.stdout.strip() else {}


def test_a_red_test_sends_the_worker_back_with_the_hooks_message(work: Path) -> None:
    _report(work)
    out = _stop(work)
    assert out["decision"] == "block"
    assert "`test -f ok.txt` is red" in out["reason"] and "never the test" in out["reason"]
    (work / "ok.txt").write_text("ok")
    assert _stop(work) == {}  # green, with a valid report: the stop goes through


def test_a_missing_or_invalid_report_blocks_the_stop(work: Path) -> None:
    (work / "ok.txt").write_text("ok")
    out = _stop(work)
    assert out["decision"] == "block" and "worker_report.json is missing" in out["reason"]
    _report(work, {**REPORT, "self_check": {"test_failed_first": "yes"}})
    out = _stop(work)
    assert out["decision"] == "block" and "worker_report.json is invalid" in out["reason"]
    _report(work, {**REPORT, "surprise": 1})
    assert "not allowed" in _stop(work)["reason"]


def test_the_hook_runs_the_test_itself_and_trusts_no_marker(work: Path) -> None:
    _report(work)
    for marker in ("tests_passed", ".vigil/green", "harness_tests.json"):
        (work / marker).parent.mkdir(parents=True, exist_ok=True)
        (work / marker).write_text('{"after": "pass"}')
    assert _stop(work)["decision"] == "block"


def test_the_cap_releases_it(work: Path) -> None:
    _report(work)
    for _ in range(3):
        assert _stop(work)["decision"] == "block"
    out = _stop(work)
    assert "decision" not in out and "stop_hook_exhausted" in out["systemMessage"]


def test_the_turn_limit_releases_it(work: Path) -> None:
    out = _stop(work, turns=12, max_turns=12)
    assert "decision" not in out and "stop_hook_exhausted" in out["systemMessage"]


def test_asking_the_coord_or_needing_a_dependency_may_stop(work: Path) -> None:
    (work / ".vigil" / JOB / TASK / "ask_coord.json").write_text('{"question": "?"}')
    assert _stop(work) == {}
    (work / ".vigil" / JOB / TASK / "ask_coord.json").unlink()
    _report(work, {**REPORT, "needs_deps": [{"ecosystem": "pypi", "package": "six",
                                              "version": "1.17.0", "why": "x"}]})  # fmt: skip
    assert _stop(work) == {}


@pytest.mark.parametrize("role", ["coord", "reviewer", "auditor"])
def test_only_workers_are_held(work: Path, role: str) -> None:
    assert _stop(work, role=role) == {}


def test_managed_settings_run_the_hook_and_the_layer_has_it() -> None:
    ms = json.loads((ROOT / "configs" / "managed-settings.json").read_text())
    (entry,) = ms["hooks"]["Stop"]
    (hook,) = entry["hooks"]
    assert hook["command"] == "/opt/vigil-venv/bin/python /opt/vigil/stop_hook.py"
    df = (ROOT / "images" / "claude-executor" / "Dockerfile").read_text()
    assert "/opt/vigil/stop_hook.py" in df and "/opt/vigil/worker_report.schema.json" in df


def test_the_harness_records_the_hooks_answers() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ep", ROOT / "images/claude-executor/entrypoint.py"
    )
    assert spec and spec.loader
    ep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ep)
    blocked = ep.stop_hook_event("Stop", "hook_response", {
        "output": '{"decision": "block", "reason": "vigil stop hook: red"}', "outcome": "success"})  # fmt: skip
    assert blocked["type"] == "stop_hook" and blocked["data"]["blocked"] is True
    done = ep.stop_hook_event(
        "Stop", "hook_response", {"output": '{"systemMessage": "stop_hook_exhausted"}'}
    )
    assert done["data"]["exhausted"] is True and done["data"]["blocked"] is False
    assert ep.stop_hook_event("PreToolUse", "hook_response", {}) is None
    assert ep.stop_hook_event("Stop", "hook_started", {}) is None
