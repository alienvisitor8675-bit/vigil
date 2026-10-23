"""The worker's Stop hook (plan 2a, item 6), from the executor layer's managed
settings. The CLI runs it, as the agent uid, whenever the model tries to end
the session; a `{"decision": "block", "reason": ...}` answer sends the model
back with the reason in its transcript.

A worker may stop only when:
- `.vigil/<job>/<task>/worker_report.json` exists and is valid against the
  worker-report schema (baked into the layer), and
- the task's `test_cmd`, run here and now with a timeout, passes. No marker
  file is trusted: the hook runs the test itself.

A worker that asks its coord (`ask_coord.json`) or reports `needs_deps` may
stop without a green test. The hook lets the stop through after the task's
turn limit, or after three blocked stops, and says `stop_hook_exhausted`,
which the harness puts on the result. It decides nothing about acceptance:
the verifier stays the only source of test results (B33). Coords, reviewers
and auditors are never held.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

WORK = Path(os.environ.get("VIGIL_WORK", "/work"))
STATE = Path(os.environ.get("VIGIL_STOP_STATE", "/tmp/vigil-stop-hook.json"))  # noqa: S108
SCHEMA = Path(os.environ.get("VIGIL_REPORT_SCHEMA", "/opt/vigil/worker_report.schema.json"))
MAX_BLOCKS = 3
TIMEOUT = int(os.environ.get("VIGIL_STOP_HOOK_TIMEOUT", "600"))
TAIL = 3000


def allow(note: str = "") -> int:
    if note:
        print(json.dumps({"systemMessage": note}))
    return 0


def block(reason: str, state: dict[str, Any]) -> int:
    state["blocks"] = int(state.get("blocks", 0)) + 1
    STATE.write_text(json.dumps(state))
    print(json.dumps({"decision": "block", "reason": f"vigil stop hook: {reason}"}))
    return 0


def turns_used(transcript: str | None) -> int:
    """The session's assistant turns so far, from its own transcript."""
    if not transcript:
        return 0
    ids: set[str] = set()
    try:
        lines = Path(transcript).read_text(errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("type") == "assistant":
            msg = e.get("message") or {}
            ids.add(str(msg.get("id") or e.get("uuid")))
    return len(ids)


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


def main() -> int:
    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook = {}
    if os.environ.get("VIGIL_ROLE", "worker") != "worker":
        return allow()
    try:
        state = json.loads(STATE.read_text())
    except OSError, json.JSONDecodeError:
        state = {}
    if int(state.get("blocks", 0)) >= MAX_BLOCKS:
        return allow(f"vigil: stop allowed after {MAX_BLOCKS} blocked stops (stop_hook_exhausted)")
    limit = int(os.environ.get("VIGIL_MAX_TURNS", "60"))
    if turns_used(hook.get("transcript_path")) >= limit:
        return allow(f"vigil: stop allowed at the turn limit, {limit} (stop_hook_exhausted)")
    try:
        task = json.loads((WORK / ".vigil" / "task.json").read_text())
    except OSError, json.JSONDecodeError:
        return allow()  # not a task vigild set up: nothing to hold it to
    art = WORK / ".vigil" / str(task.get("job_id")) / str(task.get("task_id"))
    if (art / "ask_coord.json").is_file():
        return allow()  # a question for the coord ends the session by design
    errors = report_errors(art / "worker_report.json")
    if errors:
        return block("; ".join(errors), state)
    report = json.loads((art / "worker_report.json").read_text())
    if report.get("needs_deps"):
        return allow()  # the test can't pass without the dependency; vigild takes it from here
    test_cmd = str((task.get("spec") or {}).get("test_cmd") or "").strip()
    if not test_cmd:
        return allow()
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
        return block(f"`{test_cmd}` took over {TIMEOUT} s; make it pass, then stop", state)
    if p.returncode != 0:
        tail = (p.stdout + p.stderr)[-TAIL:]
        return block(
            f"`{test_cmd}` is red (exit {p.returncode}). Fix the code (never the test) until it "
            f"passes, update your report, commit, then stop. Its output:\n{tail}",
            state,
        )
    return allow()


if __name__ == "__main__":
    sys.exit(main())
