"""The worker's Stop hook (plan 2a, item 6; 2a round 2, reviews A6, B9, B12),
run as the CLI runs it: a command from managed settings, the hook's input on
stdin, its answer on stdout.

The hook is advisory: the verifier decides acceptance. What it reads to
decide (the test command, the ids, the turn limit, the block count) comes
from a root-owned, read-only directory the harness writes before the agent
starts (`/run/vigil`), never from the agent's workspace."""

from __future__ import annotations

import json
import os
import stat
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
    # the agent can write this one: the hook must not read its test_cmd from it
    (w / ".vigil" / "task.json").write_text(
        json.dumps({"job_id": JOB, "task_id": TASK, "spec": {"test_cmd": "true"}})
    )
    run = tmp_path / "run"  # stands in for the harness's root-owned /run/vigil
    run.mkdir()
    (run / "task.json").write_text(
        json.dumps({"job_id": JOB, "task_id": TASK, "test_cmd": "test -f ok.txt"})
    )
    return w


def _report(work: Path, doc: dict[str, Any] | None = REPORT) -> None:
    (work / ".vigil" / JOB / TASK / "worker_report.json").write_text(json.dumps(doc))


def _blocks(work: Path, n: int) -> None:
    """What the harness keeps: the stops the hook has blocked so far."""
    (work.parent / "run" / "stop.json").write_text(json.dumps({"blocks": n}))


def _stop(
    work: Path,
    *,
    role: str = "worker",
    requests: int = 0,
    history: int = 0,
    max_turns: int = 60,
) -> dict[str, Any]:
    """`requests`: this session's model requests (`api-request` lines);
    `history`: assistant entries a resume copied in from earlier sessions."""
    transcript = work.parent / "t.jsonl"
    lines = [{"type": "assistant", "message": {"id": f"old{i}"}} for i in range(history)]
    lines += [{"type": "api-request"} for _ in range(requests)]
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines))
    env = {
        **os.environ,
        "VIGIL_WORK": str(work),
        "VIGIL_HOOK_DIR": str(work.parent / "run"),
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


def test_the_test_command_comes_from_the_harness_copy_not_the_workspace(work: Path) -> None:
    """Review A6: `.vigil/task.json` is the agent's to write (here it says
    `true`); the hook reads the root-owned copy the harness made first."""
    _report(work)
    assert _stop(work)["decision"] == "block"


def test_without_the_harness_copy_nothing_is_held(work: Path) -> None:
    (work.parent / "run" / "task.json").unlink()
    assert _stop(work) == {}


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


def test_the_cap_the_harness_keeps_releases_it(work: Path) -> None:
    """Review A6, B12: the block count is the harness's (root-owned); the hook
    only reads it and never writes a counter the agent could reset."""
    _report(work)
    for n in range(3):
        _blocks(work, n)
        assert _stop(work)["decision"] == "block"
    assert json.loads((work.parent / "run" / "stop.json").read_text()) == {"blocks": 2}
    _blocks(work, 3)
    out = _stop(work)
    assert "decision" not in out and "stop_hook_exhausted" in out["systemMessage"]


def test_the_last_turn_releases_it_flagged_only_when_a_check_failed(work: Path) -> None:
    """A block on the last turn only ends in the CLI's max-turns error, which
    loses the session's result. `stop_hook_exhausted` means a check failed
    (review B12): a green last turn stops plainly."""
    _report(work)
    out = _stop(work, requests=2, max_turns=3)
    assert "decision" not in out and "stop_hook_exhausted" in out["systemMessage"]
    assert _stop(work, requests=1, max_turns=3)["decision"] == "block"
    (work / "ok.txt").write_text("ok")
    assert _stop(work, requests=2, max_turns=3) == {}


def test_a_resumed_worker_gets_its_remaining_turns_with_checks(work: Path) -> None:
    """Review B9, one model: vigild passes the remainder (60 - 40 = 20) and
    the hook counts only this session's requests, never the turns a resume
    copied in. At 40 of 60 the worker gets about 20 more turns, checked."""
    _report(work)
    assert _stop(work, history=40, requests=1, max_turns=20)["decision"] == "block"
    assert _stop(work, history=40, requests=18, max_turns=20)["decision"] == "block"
    out = _stop(work, history=40, requests=19, max_turns=20)
    assert "decision" not in out and "stop_hook_exhausted" in out["systemMessage"]


def test_turns_are_this_sessions_model_requests() -> None:
    """CLI 2.1.306 writes one `api-request` line per model call before the
    Stop hook runs, and the turn's assistant entry after it (seen live).
    vigild's stored transcript, which a resume copies in, has no
    `api-request` lines (seen live), so they count this session alone."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("stop_hook", HOOK)
    assert spec is not None and spec.loader is not None
    hook = module_from_spec(spec)
    spec.loader.exec_module(hook)
    t = Path(os.environ.get("TMPDIR", "/tmp")) / f"cli-{os.getpid()}.jsonl"
    kinds = ["assistant", "assistant", "queue-operation", "user", "attachment",
             "api-request-shape", "api-request", "last-prompt", "assistant", "user",
             "api-request-blob", "api-request"]  # fmt: skip
    t.write_text("".join(json.dumps({"type": k, "message": {"id": f"m{i}"}}) + "\n"
                         for i, k in enumerate(kinds)))  # fmt: skip
    try:
        assert hook.turns_used(str(t)) == 2
    finally:
        t.unlink()


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


def test_the_docstring_says_the_hook_is_advisory() -> None:
    doc = HOOK.read_text().split('"""')[1]
    assert "advisory" in doc and "verifier" in doc
    assert "No marker file is trusted" not in doc


def test_managed_settings_run_the_hook_and_the_layer_has_it() -> None:
    ms = json.loads((ROOT / "configs" / "managed-settings.json").read_text())
    (entry,) = ms["hooks"]["Stop"]
    (hook,) = entry["hooks"]
    assert hook["command"] == "/opt/vigil-venv/bin/python /opt/vigil/stop_hook.py"
    df = (ROOT / "images" / "claude-executor" / "Dockerfile").read_text()
    assert "/opt/vigil/stop_hook.py" in df and "/opt/vigil/worker_report.schema.json" in df


def _entrypoint() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ep", ROOT / "images/claude-executor/entrypoint.py"
    )
    assert spec and spec.loader
    ep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ep)
    return ep


def test_the_harness_records_the_hooks_answers() -> None:
    ep = _entrypoint()
    blocked = ep.stop_hook_event("Stop", "hook_response", {
        "output": '{"decision": "block", "reason": "vigil stop hook: red"}', "outcome": "success"})  # fmt: skip
    assert blocked["type"] == "stop_hook" and blocked["data"]["blocked"] is True
    done = ep.stop_hook_event(
        "Stop", "hook_response", {"output": '{"systemMessage": "stop_hook_exhausted"}'}
    )
    assert done["data"]["exhausted"] is True and done["data"]["blocked"] is False
    assert ep.stop_hook_event("PreToolUse", "hook_response", {}) is None
    assert ep.stop_hook_event("Stop", "hook_started", {}) is None


def test_the_harness_writes_the_hooks_inputs_read_only_before_the_agent(tmp_path: Path) -> None:
    ep = _entrypoint()
    run = tmp_path / "run"
    run.mkdir()
    task = {"job_id": JOB, "task_id": TASK, "spec": {"test_cmd": "pytest -q t.py"}, "x": 1}
    ep.write_hook_inputs(run, task, max_turns=20)
    doc = json.loads((run / "task.json").read_text())
    assert doc == {"job_id": JOB, "task_id": TASK, "test_cmd": "pytest -q t.py", "max_turns": 20}
    assert stat.S_IMODE((run / "task.json").stat().st_mode) == 0o444
    ep.write_stop_blocks(run, 2)
    assert json.loads((run / "stop.json").read_text()) == {"blocks": 2}
    assert stat.S_IMODE((run / "stop.json").stat().st_mode) == 0o444
    ep.write_stop_blocks(run, 3)  # replaced, though read-only
    assert json.loads((run / "stop.json").read_text()) == {"blocks": 3}
