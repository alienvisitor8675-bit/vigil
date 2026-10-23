"""Hidden tests for runner-file tasks (owner ruling, 2026-10-23; learning
rulings 10, 13 and 14, built early for this class only).

A fresh `hidden_test_author` session writes acceptance tests at spec time from
spec.md and the task contract. They are kept in vigild's vault as a commit on a
branch that is never pushed, and listed in the job's `hidden_tests.json`
artifact (names and categories only for the owner). They are never on the
task's branch, never in a worker's packet, clone or container, and run only in
the verifier (`Verifier._hidden_pair`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigild.intake.paths import scope_entry_error
from vigild.packets.assemble import hidden_dir
from vigild.verifier import HiddenSuite
from vigild.workspace import Workspace

ARTIFACT = "hidden_tests.json"
MAX_FILES = 40
MAX_TESTS = 200
CATEGORY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CONFIG_NAMES = ("conftest.py", "pytest.ini", "package.json", "tox.ini", "setup.cfg")
AUTHOR_ATTEMPTS = 2  # one try and one regeneration


class HiddenError(Exception):
    """Why a session's output can't be used as hidden tests."""


class HiddenTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    category: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_cmd: str
    tests: list[HiddenTest] = Field(min_length=1, max_length=MAX_TESTS)


def load_suite(
    ws: Workspace,
    repo: str,
    sha: str,
    base_sha: str,
    job_id: str,
    task_id: str,
    test_cmd_problem: Any,
) -> tuple[HiddenSuite, Manifest]:
    """The author's output at `sha` (a vault commit), checked. `test_cmd_problem`
    is the spec gate's resolver, given the base's file reader."""
    folder = hidden_dir(job_id, task_id)
    area = f".vigil/{job_id}/hidden/"
    raw = ws.read_blob(repo, sha, f"{folder}.json", 64 << 10)
    if raw is None:
        raise HiddenError(f"no usable {folder}.json was written")
    try:
        manifest = Manifest.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise HiddenError(f"{folder}.json is not valid: {str(e)[:300]}") from e
    for t in manifest.tests:
        if not CATEGORY.match(t.category):
            raise HiddenError(f"category {t.category!r} must be one lowercase word")
    changed = ws.changed(repo, base_sha, sha)
    outside = sorted(p for _, p in changed if not p.startswith(area))
    if outside:
        raise HiddenError(f"the author changed files outside the hidden directory: {outside[:5]}")
    files = sorted(p for s, p in changed if p.startswith(f"{folder}/") and s[:1] in ("A", "M"))
    if not files:
        raise HiddenError(f"no test files under {folder}/")
    if len(files) > MAX_FILES:
        raise HiddenError(f"{len(files)} files; at most {MAX_FILES}")
    for f in files:
        if scope_entry_error(f) or f.rsplit("/", 1)[-1] in CONFIG_NAMES:
            raise HiddenError(f"{f} is not an allowed hidden test file")
        if ws.read_blob(repo, sha, f) is None:
            raise HiddenError(f"{f} is not a regular file of a usable size")
    why = test_cmd_problem(manifest.test_cmd, lambda p: ws.read_blob(repo, base_sha, p))
    if why:
        raise HiddenError(why)
    return HiddenSuite(sha, folder, tuple(files), manifest.test_cmd), manifest


def entry_suite(entry: dict[str, Any] | None) -> HiddenSuite | None:
    """The stored suite of one task, or None when none was produced."""
    if not entry or entry.get("status") != "ok":
        return None
    return HiddenSuite(
        str(entry["sha"]), str(entry["prefix"]), tuple(entry["files"]), str(entry["test_cmd"])
    )


def public_view(entry: dict[str, Any]) -> dict[str, Any]:
    """What the spec inbox shows: names and categories, never the code."""
    if entry.get("status") != "ok":
        return {
            "status": "missing",
            "reason": entry.get("reason", ""),
            "note": "hidden tests missing; this task can only escalate",
        }
    return {"status": "ok", "tests": entry["tests"], "attempts": entry.get("attempts", 1)}


def show(store: Any, ws: Workspace, task_id: str) -> str:
    """The owner's dispute path (`vigil hidden show <task>`, on the host only):
    the task's hidden tests, in full. Reads vigild's store and vault directly;
    nothing serves it over the API or MCP. Records `hidden_tests_shown`
    (provenance human): shown to a human, not to a model."""
    try:
        task = store.get_task(task_id)
    except KeyError as e:
        raise HiddenError(f"no task {task_id}") from e
    job_id = task.job_id
    art = store.get_artifact(job_id, ARTIFACT) if job_id else None
    entry = (json.loads(art.content).get("tasks", {}) if art else {}).get(task_id)
    suite = entry_suite(entry)
    if entry is None or suite is None:
        reason = (entry or {}).get("reason", "no hidden tests were produced for this task")
        raise HiddenError(f"{task_id} has no hidden tests: {reason}")
    repo = store.get_job(str(job_id)).repo
    out = [f"# hidden tests for {task_id}", f"run: {suite.test_cmd}", ""]
    for t in entry["tests"]:
        out.append(f"- [{t['category']}] {t['name']}")
    for f in suite.files:
        out += ["", f"## {f}", ws.read_blob(repo, suite.sha, f) or "(unreadable)"]
    store.add_event(
        "hidden_tests_shown",
        {"task": task_id, "files": list(suite.files), "to": "owner, on the host"},
        "human",
        job_id=job_id,
        task_id=task_id,
    )
    return "\n".join(out) + "\n"
