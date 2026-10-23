"""The executor harness's own pieces that run without a container: redaction
(item 13), and committing with git pinned against a hostile .git (round 2, N1)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from vigild.redact import redact as vigild_redact

ROOT = Path(__file__).resolve().parents[2]


def _entrypoint() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "entrypoint", ROOT / "images" / "claude-executor" / "entrypoint.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOKEN = "vgw_" + "A" * 43


@pytest.mark.parametrize("redact", [lambda v: _entrypoint().redact(v, TOKEN), vigild_redact])
def test_tokens_are_redacted_from_events(redact: object) -> None:
    event = {
        "type": "tool_call",
        "data": {
            "input": {"command": f"echo $ANTHROPIC_AUTH_TOKEN # {TOKEN}"},
            "content": ["sk-ant-api03-abcdefghijkl", "ghs_abcdefghijklmnop", "ok"],
        },
    }
    out = redact(event)  # type: ignore[operator]
    flat = str(out)
    assert TOKEN not in flat and "sk-ant-api03" not in flat and "ghs_abcdef" not in flat
    assert "ok" in flat and "[redacted]" in flat


def test_the_bundle_replaces_a_planted_symlink(tmp_path: Path) -> None:
    work, base = _work(tmp_path)
    (work / "b.txt").write_text("b\n")
    ep = _harness(tmp_path, work)
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    os.symlink(target, ep.OUT / "result.bundle")
    ep.snapshot(_task(base), "worker")
    assert target.read_text() == "untouched"
    assert not (ep.OUT / "result.bundle").is_symlink()


# --- Review round 2, N1: the record can't be forged through git --------------------------

BRANCH = "vigil/fx-0001-w1"


def _run(work: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=work, capture_output=True, text=True, check=True
    ).stdout.strip()


def _work(tmp_path: Path) -> tuple[Path, str]:
    """A clone the way vigild leaves it: base commit, task branch, task.json excluded."""
    work = tmp_path / "work"
    work.mkdir()
    _run(work, "init", "-q", "-b", "main")
    _run(work, "config", "user.email", "t@vigil.invalid")
    _run(work, "config", "user.name", "t")
    (work / "a.txt").write_text("a\n")
    _run(work, "add", "-A")
    _run(work, "commit", "-q", "-m", "base")
    _run(work, "checkout", "-q", "-b", BRANCH)
    base = _run(work, "rev-parse", "HEAD")
    (work / ".git" / "info").mkdir(exist_ok=True)
    (work / ".git" / "info" / "exclude").write_text("/.vigil/task.json\n")
    (work / ".vigil").mkdir()
    return work, base


def _task(base: str, scope: list[str] | None = None) -> dict[str, Any]:
    return {
        "branch": BRANCH,
        "base_sha": base,
        "task_id": "fx-0001-w1",
        "job_id": "fx-0001",
        "spec": {"files_in_scope": scope if scope is not None else ["b.txt"]},
    }


def _hostile_git(work: Path, marker: Path) -> None:
    """What the reviewer reproduced: code git runs on status, add and commit."""
    script = work.parent / "evil.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    script.chmod(0o755)
    _run(work, "config", "core.fsmonitor", str(script))
    hook = work / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    hook.chmod(0o755)


def _harness(tmp_path: Path, work: Path) -> ModuleType:
    ep = _entrypoint()
    ep.WORK = work
    ep.OUT = tmp_path / "out"
    ep.OUT.mkdir()
    ep.PACKET = tmp_path / "packet.md"
    ep.PACKET.write_text("(packet)")
    return ep


def test_harness_git_runs_no_repo_hook_or_fsmonitor(tmp_path: Path) -> None:
    work, base = _work(tmp_path)
    marker = tmp_path / "marker"
    _hostile_git(work, marker)
    (work / "b.txt").write_text("b\n")
    ep = _harness(tmp_path, work)
    head, _ = ep.snapshot(_task(base), "worker")
    assert not marker.exists(), "git ran the repo's fsmonitor or hook"
    assert len(head) == 40 and head != base
    assert (ep.OUT / "result.bundle").is_file()


@pytest.mark.parametrize("plant", ["commondir", "objects/info/alternates"])
def test_harness_refuses_a_git_dir_that_points_elsewhere(tmp_path: Path, plant: str) -> None:
    work, base = _work(tmp_path)
    (work / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (work / ".git" / plant).write_text(str(tmp_path / "elsewhere") + "\n")
    ep = _harness(tmp_path, work)
    with pytest.raises(ep.HarnessRefused):
        ep.snapshot(_task(base), "worker")
    assert not (ep.OUT / "result.bundle").exists()


def test_harness_refuses_a_git_file_in_place_of_the_dir(tmp_path: Path) -> None:
    work, base = _work(tmp_path)
    os.rename(work / ".git", tmp_path / "moved.git")
    (work / ".git").write_text(f"gitdir: {tmp_path / 'moved.git'}\n")
    ep = _harness(tmp_path, work)
    with pytest.raises(ep.HarnessRefused):
        ep.snapshot(_task(base), "worker")


def test_harness_refuses_a_head_off_the_task_branch(tmp_path: Path) -> None:
    work, base = _work(tmp_path)
    _run(work, "checkout", "-q", "-b", "other")
    ep = _harness(tmp_path, work)
    with pytest.raises(ep.HarnessRefused):
        ep.snapshot(_task(base), "worker")


def test_the_harness_runs_no_test_and_names_the_head_on_its_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tests run only in vigild's verifier (owner direction, round 2): the
    harness never runs test_cmd and writes no record; it commits with pinned
    git and names the head on PID 1's stdout."""
    work, base = _work(tmp_path)
    marker = tmp_path / "marker"
    task = {
        "task_id": "fx-0001-w1",
        "job_id": "fx-0001",
        "branch": BRANCH,
        "base_sha": base,
        "spec": {"test_cmd": f"echo ran >> {marker}", "files_in_scope": ["b.txt"]},
    }
    (work / ".vigil" / "task.json").write_text(json.dumps(task))
    ep = _harness(tmp_path, work)

    async def agent(packet: str, role: str, token: str) -> dict[str, Any]:
        (work / "b.txt").write_text("b\n")
        _hostile_git(work, marker)
        return {"plugins": ["guard"], "head": "0" * 40}  # can't override the harness's own

    monkeypatch.setattr(ep, "run_agent", agent)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"gateway_token": TOKEN}) + "\n"))
    assert ep.main() == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    result = lines[-1]["vigil_result"]
    assert "harness" not in result
    assert not (ep.OUT / "harness_tests.json").exists()
    assert not marker.exists(), "test_cmd, a hook or fsmonitor ran"
    assert result["head"] == _run(work, "rev-parse", BRANCH) != "0" * 40
    files = _run(work, "ls-tree", "-r", "--name-only", result["head"]).split()
    assert "b.txt" in files


def test_one_redactor_and_the_harness_matches_it() -> None:
    from vigild.gateway import service
    from vigild.jobs import runner
    from vigild.redact import PEM_KEY, SECRETISH, redact

    assert runner.redact is redact and service.redact is redact
    assert _entrypoint().SECRETISH.pattern == SECRETISH.pattern
    assert _entrypoint().PEM_KEY.pattern == PEM_KEY.pattern


# --- round 3: a session image's prepare hook ------------------------------------------------


def test_the_session_prepare_hook_runs_before_the_agent(tmp_path: Path) -> None:
    work, _ = _work(tmp_path)
    ep = _harness(tmp_path, work)
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\nmkdir -p node_modules && touch node_modules/.ready\n")
    hook.chmod(0o755)
    ep.SESSION_PREPARE = str(hook)
    ep.prepare_session()
    assert (work / "node_modules" / ".ready").exists()
    hook.write_text("#!/bin/sh\necho no postgres >&2; exit 3\n")
    with pytest.raises(ep.HarnessRefused, match="no postgres"):
        ep.prepare_session()
    ep.SESSION_PREPARE = str(tmp_path / "absent")
    ep.prepare_session()  # no hook (the generic image): nothing to do


# --- review round 4, item 7: only what the task may change is committed ------------------


def test_build_output_outside_scope_is_left_out_not_committed(tmp_path: Path) -> None:
    """A worker that ran pytest leaves __pycache__ and build output behind:
    they stay out of the commit (and are listed), so scope still passes."""
    work, base = _work(tmp_path)
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("x = 1\n")
    (work / "src" / "__pycache__").mkdir()
    (work / "src" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\0")
    (work / "build").mkdir()
    (work / "build" / "out.txt").write_text("built\n")
    report = work / ".vigil" / "fx-0001" / "fx-0001-w1"
    report.mkdir(parents=True)
    (report / "worker_report.json").write_text("{}")
    (work / "a.txt").write_text("a, changed\n")  # tracked: always committed
    ep = _harness(tmp_path, work)
    head, left_out = ep.snapshot(_task(base, ["src/**"]), "worker")
    files = _run(work, "diff", "--name-only", base, head).split()
    assert sorted(files) == [".vigil/fx-0001/fx-0001-w1/worker_report.json", "a.txt", "src/a.py"]
    assert left_out == ["build/out.txt", "src/__pycache__/a.cpython-314.pyc"]


def test_left_out_files_are_named_on_the_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work, base = _work(tmp_path)
    (work / ".vigil" / "task.json").write_text(json.dumps(_task(base)))
    ep = _harness(tmp_path, work)

    async def agent(packet: str, role: str, token: str) -> dict[str, Any]:
        (work / "b.txt").write_text("b\n")
        (work / "junk.log").write_text("x\n")
        return {"plugins": ["guard"]}

    monkeypatch.setattr(ep, "run_agent", agent)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"gateway_token": TOKEN}) + "\n"))
    assert ep.main() == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["vigil_result"]
    assert result["not_committed"] == ["junk.log"]


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Run run_agent against a stand-in SDK and return the env it gave the CLI."""
    import asyncio
    import types

    seen: dict[str, Any] = {}

    class Msg:  # every message type the harness isinstance-checks
        pass

    async def query(prompt: str, options: Any) -> Any:
        seen.update(options.kwargs)
        return
        yield

    class Options:
        def __init__(self, **kw: Any) -> None:
            self.kwargs = kw

    sdk = types.ModuleType("claude_agent_sdk")
    for name in (
        "AssistantMessage",
        "ResultMessage",
        "SystemMessage",
        "ToolResultBlock",
        "ToolUseBlock",
        "UserMessage",
    ):
        setattr(sdk, name, type(name, (Msg,), {}))
    sdk.ClaudeAgentOptions = Options  # type: ignore[attr-defined]
    sdk.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setenv("VIGIL_GATEWAY_URL", "http://gw")
    asyncio.run(_entrypoint().run_agent("packet", "coord", TOKEN))
    env: dict[str, str] = seen["env"]
    return env


def test_the_entrypoint_maps_the_output_cap_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_MAX_OUTPUT_TOKENS", "16384")
    assert _agent_env(monkeypatch)["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"
    monkeypatch.delenv("VIGIL_MAX_OUTPUT_TOKENS")
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in _agent_env(monkeypatch)
    monkeypatch.setenv("VIGIL_MAX_OUTPUT_TOKENS", "lots")  # not a number: ignored
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in _agent_env(monkeypatch)
