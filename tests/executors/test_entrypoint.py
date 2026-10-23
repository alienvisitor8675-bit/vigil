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
    from vigild.redact import SECRETISH, redact

    assert runner.redact is redact and service.redact is redact
    assert _entrypoint().SECRETISH.pattern == SECRETISH.pattern


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


# --- phase 2a, item 1: reviewers and auditors read and answer ----------------------------


def test_reviewers_get_read_only_tools_and_structured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = _entrypoint()
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    (tmp_path / "schema.json").write_text(json.dumps(schema))
    monkeypatch.setenv("VIGIL_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("VIGIL_OUTPUT_SCHEMA", str(tmp_path / "schema.json"))
    for role in ("reviewer", "auditor"):
        opts = ep.agent_options(role, TOKEN, print)
        assert opts["tools"] == ["Read", "Grep", "Glob"]
        assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(opts["disallowed_tools"])
        assert opts["output_format"] == {"type": "json_schema", "schema": schema}
    monkeypatch.delenv("VIGIL_OUTPUT_SCHEMA")
    worker = ep.agent_options("worker", TOKEN, print)
    assert "tools" not in worker and "output_format" not in worker


def test_a_review_commits_nothing_and_answers_on_the_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work, base = _work(tmp_path)
    (work / ".vigil" / "task.json").write_text(json.dumps(_task(base)))
    ep = _harness(tmp_path, work)
    answer = {"verdict": "clean", "findings": []}

    async def agent(packet: str, role: str, token: str) -> dict[str, Any]:
        return {"plugins": ["guard"], "structured_output": answer}

    monkeypatch.setattr(ep, "run_agent", agent)
    monkeypatch.setenv("VIGIL_ROLE", "reviewer")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"gateway_token": TOKEN}) + "\n"))
    assert ep.main() == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["vigil_result"]
    assert result["status"] == "done" and result["structured_output"] == answer
    assert "head" not in result
    assert not (ep.OUT / "result.bundle").exists()
    assert _run(work, "rev-parse", BRANCH) == base


# --- phase 2a, items 2-3: transcripts go out as they're written; resume reads them back ---


def test_the_transcript_store_mirrors_lines_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    ep = _entrypoint()
    store = ep.TranscriptStore(TOKEN)
    entries = [{"type": "user", "uuid": "u1", "message": {"content": f"token {TOKEN}"}}]
    asyncio.run(store.append({"project_key": "-work", "session_id": "sdk-1"}, entries))
    (line,) = capsys.readouterr().out.splitlines()
    out = json.loads(line)["vigil_transcript"]
    assert out["session_id"] == "sdk-1" and out["subpath"] is None
    assert TOKEN not in line and out["entries"][0]["uuid"] == "u1"
    # it never loads: a store-backed resume lands in a root-only temp dir
    assert asyncio.run(store.load({"project_key": "-work", "session_id": "sdk-1"})) is None


SID = "0b6f1a52-6c4e-4c5e-9a43-1f2d3e4c5b6a"


def test_a_resume_places_vigilds_transcript_where_the_cli_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = _entrypoint()
    ep.WORK = Path("/work")
    ep.RESUME = tmp_path / "resume.jsonl"
    ep.RESUME.write_text('{"type": "user", "uuid": "u1"}\n{"type": "assistant", "uuid": "a1"}\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    monkeypatch.setenv("VIGIL_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("VIGIL_RESUME_SESSION", SID)
    monkeypatch.setenv("VIGIL_RESUME_FORK", "1")
    assert ep.place_resume() == SID
    placed = tmp_path / "home" / ".claude" / "projects" / "-work" / f"{SID}.jsonl"
    assert placed.read_text() == ep.RESUME.read_text()
    opts = ep.agent_options("worker", TOKEN, print)
    assert opts["resume"] == SID and opts["fork_session"] is True
    assert opts["session_store_flush"] == "eager" and opts["session_store"] is not None
    monkeypatch.setenv("VIGIL_RESUME_SESSION", "../../etc/passwd")
    with pytest.raises(ep.HarnessRefused):
        ep.place_resume()
    monkeypatch.delenv("VIGIL_RESUME_SESSION")
    assert ep.place_resume() is None
    assert "resume" not in ep.agent_options("worker", TOKEN, print)


def test_a_checkpoint_bundles_the_work_without_touching_the_branch(tmp_path: Path) -> None:
    """Phase 2a, item 3: the work so far as a commit off the branch; the
    branch, HEAD and the agent's index stay as they were."""
    work, base = _work(tmp_path)
    (work / "a.txt").write_text("changed\n")
    (work / "b.txt").write_text("new, in scope\n")
    (work / "junk.log").write_text("not the work\n")
    _run(work, "add", "a.txt")  # the agent staged something itself
    staged = _run(work, "diff", "--cached", "--name-only")
    ep = _harness(tmp_path, work)
    commit = ep.checkpoint(_task(base), 4)
    assert _run(work, "rev-parse", BRANCH) == base  # the branch didn't move
    assert _run(work, "diff", "--cached", "--name-only") == staged  # nor did the index
    files = _run(work, "ls-tree", "-r", "--name-only", commit).split()
    assert {"a.txt", "b.txt"} <= set(files) and "junk.log" not in files
    assert _run(work, "show", f"{commit}:a.txt") == "changed"
    heads = _run(work, "bundle", "list-heads", str(ep.OUT / "checkpoint.bundle"))
    assert heads == f"{commit} refs/vigil/checkpoint"


def test_a_review_caps_each_calls_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway holds a call's worst case (max_tokens of output) against the
    reservation; a reviewer's cap keeps that inside a review's reservation."""
    ep = _entrypoint()
    monkeypatch.setenv("VIGIL_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("VIGIL_MAX_OUTPUT_TOKENS", "8192")
    assert (
        ep.agent_options("reviewer", TOKEN, print)["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    )
    monkeypatch.delenv("VIGIL_MAX_OUTPUT_TOKENS")
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in ep.agent_options("worker", TOKEN, print)["env"]
