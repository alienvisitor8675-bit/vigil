"""The worker's Stop hook (plan 2a, item 6), from the executor layer's managed
settings. The CLI runs it, as the agent uid, whenever the model tries to end
the session; a `{"decision": "block", "reason": ...}` answer sends the model
back with the reason in its transcript.

**It is advisory.** It saves turns by catching a stop that would fail, but it
decides nothing about acceptance: the verifier, on vault commits in fresh
containers, is the only source of test results (B33). The agent can write
what the hook reads from the workspace (the report, `ask_coord.json`) and the
transcript the CLI writes as it; at worst that lets it stop early, and the
verifier still judges the work.

A worker may stop only when:
- `.vigil/<job>/<task>/worker_report.json` exists and is valid against the
  worker-report schema (baked into the layer), and
- the task's `test_cmd`, run here and now with a timeout, passes. The hook
  runs the test itself.

What it decides with comes from the harness, not the workspace (2a round 2,
review A6): before the agent starts, the root harness writes the job and task
ids, `test_cmd` and the turn limit to `/run/vigil/task.json`, in a root-owned
tmpfs the agent uid can read but not write; it keeps the count of blocked
stops in `/run/vigil/stop.json` from the hook events it sees. The hook never
writes state of its own.

**Turns** (review B9): vigild passes this session's remainder of the task's
turn cap (`VIGIL_MAX_TURNS`), and the hook counts this session's model
requests only: the CLI writes an `api-request` line per request before the
hook runs, and a resumed transcript (copied in from vigild's store) carries
none. A resumed worker at 40 of 60 turns gets about 20 more, checked.

A worker that asks its coord or reports `needs_deps` may stop without a green
test. A failing stop is let through on the session's last turn (a block there
only ends in the CLI's max-turns error, which loses the result) or after
three blocked stops, and says `stop_hook_exhausted`, which the harness puts on
the result; a stop whose checks pass is never flagged. Coords, reviewers and
auditors are never held.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

WORK = Path(os.environ.get("VIGIL_WORK", "/work"))
HOOK_DIR = Path(os.environ.get("VIGIL_HOOK_DIR", "/run/vigil"))  # the harness's, read-only here
SCHEMA = Path(os.environ.get("VIGIL_REPORT_SCHEMA", "/opt/vigil/worker_report.schema.json"))
MAX_BLOCKS = 3
TIMEOUT = int(os.environ.get("VIGIL_STOP_HOOK_TIMEOUT", "600"))
TAIL = 3000


def allow(note: str = "") -> int:
    if note:
        print(json.dumps({"systemMessage": note}))
    return 0


def block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": f"vigil stop hook: {reason}"}))
    return 0


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return {}
    return doc if isinstance(doc, dict) else {}


def turns_used(transcript: str | None) -> int:
    """This session's turns so far: its `api-request` lines (see the module
    docstring). A transcript the hook can't read counts none."""
    if not transcript:
        return 0
    try:
        lines = Path(transcript).read_text(errors="replace").splitlines()
    except OSError:
        return 0
    n = 0
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += isinstance(e, dict) and e.get("type") == "api-request"
    return n


TYPES: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def check(doc: Any, schema: dict[str, Any], defs: dict[str, Any], where: str = "") -> list[str]:
    """The JSON-schema keywords the worker-report schema uses (it's generated
    from vigild's pydantic model): $ref, type, enum, required, properties,
    additionalProperties, items, minLength, maxLength."""
    if "$ref" in schema:
        return check(doc, defs[schema["$ref"].rsplit("/", 1)[-1]], defs, where)
    at = where or "(top)"
    want = schema.get("type")
    if want == "integer":
        if not isinstance(doc, int) or isinstance(doc, bool):
            return [f"{at}: not an integer"]
    elif want is not None:
        kind = TYPES.get(want)
        if (
            kind is None
            or not isinstance(doc, kind)
            or (kind is not bool and isinstance(doc, bool))
        ):
            return [f"{at}: not a {want}"]
    if "enum" in schema and doc not in schema["enum"]:
        return [f"{at}: {doc!r} is not one of {schema['enum']}"]
    errs: list[str] = []
    too_short = isinstance(doc, str) and len(doc) < schema.get("minLength", 0)
    if too_short or (isinstance(doc, str) and len(doc) > schema.get("maxLength", 1 << 30)):
        errs.append(f"{at}: wrong length")
    if isinstance(doc, list) and "items" in schema:
        for i, item in enumerate(doc):
            errs += check(item, schema["items"], defs, f"{where}/{i}")
    if isinstance(doc, dict):
        props = schema.get("properties") or {}
        errs += [f"{where}/{k}: required" for k in schema.get("required") or [] if k not in doc]
        if schema.get("additionalProperties") is False:
            errs += [f"{where}/{k}: not allowed" for k in doc if k not in props]
        for k, v in doc.items():
            if k in props:
                errs += check(v, props[k], defs, f"{where}/{k}")
    return errs


def report_errors(path: Path) -> list[str]:
    rel = path.relative_to(WORK)
    if not path.is_file():
        return [f"{rel} is missing: write it (the packet's output schema) and commit"]
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [f"{rel} isn't valid JSON: {e}"]
    schema = json.loads(SCHEMA.read_text())
    errs = check(doc, schema, schema.get("$defs") or {})
    return [f"{rel} is invalid: " + "; ".join(errs[:10])] if errs else []


def failed_check(task: dict[str, Any]) -> str | None:
    """Why this stop would fail, or None when the worker may stop."""
    art = WORK / ".vigil" / str(task.get("job_id")) / str(task.get("task_id"))
    if (art / "ask_coord.json").is_file():
        return None  # a question for the coord ends the session by design
    errors = report_errors(art / "worker_report.json")
    if errors:
        return "; ".join(errors)
    report = json.loads((art / "worker_report.json").read_text())
    if report.get("needs_deps"):
        return None  # the test can't pass without the dependency; vigild takes it from here
    test_cmd = str(task.get("test_cmd") or "").strip()
    if not test_cmd:
        return None
    try:
        p = subprocess.run(
            ["bash", "-o", "pipefail", "-o", "errexit", "-c", test_cmd],
            cwd=WORK,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"`{test_cmd}` took over {TIMEOUT} s; make it pass, then stop"
    if p.returncode != 0:
        tail = (p.stdout + p.stderr)[-TAIL:]
        return (
            f"`{test_cmd}` is red (exit {p.returncode}). Fix the code (never the test) until it "
            f"passes, update your report, commit, then stop. Its output:\n{tail}"
        )
    return None


def main() -> int:
    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook = {}
    if os.environ.get("VIGIL_ROLE", "worker") != "worker":
        return allow()
    task = _read(HOOK_DIR / "task.json")
    if not task.get("task_id"):
        return allow()  # not a task the harness set up: nothing to hold it to
    why = failed_check(task)
    if why is None:
        return allow()
    blocks = int(_read(HOOK_DIR / "stop.json").get("blocks") or 0)
    if blocks >= MAX_BLOCKS:
        return allow(f"vigil: stop allowed after {MAX_BLOCKS} blocked stops (stop_hook_exhausted)")
    limit = int(task.get("max_turns") or os.environ.get("VIGIL_MAX_TURNS") or 60)
    # a block buys one more turn; on the last one it only ends in the CLI's
    # max-turns error, and the session's result is lost
    if turns_used(hook.get("transcript_path")) + 1 >= limit:
        return allow(f"vigil: stop allowed on the last turn of {limit} (stop_hook_exhausted)")
    return block(why)


if __name__ == "__main__":
    sys.exit(main())
