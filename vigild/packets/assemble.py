"""Task packets (design "Prompting discipline"). Every session gets one packet,
assembled here, in six parts:

1. Role and rules: prompts/<role>.md, then the repo's CLAUDE.md / AGENTS.md.
2. Context pack: feature-map and architecture excerpts for the files in scope,
   the files themselves, related tests, the last three commits touching them,
   and for fix jobs the parent's spec and diffs. Capped; drops are recorded.
3. Contract: the task JSON, with done_when restated as a checklist.
4. Untrusted content, fenced: issue text, PR comments, fetched pages, logs.
5. Output schema: the exact file the session must end with.
6. Self-check: five questions the worker answers in its report.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vigild.contract.models import Plan, WorkerReport
from vigild.gitsafe import GitError, git
from vigild.intake.paths import scope_entry_error
from vigild.jobs.artifacts import SPEC_SECTIONS, artifact_path
from vigild.jobs.model import Role
from vigild.packets.fence import UNTRUSTED_RULE, fence

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
DEFAULT_BUDGET_CHARS = 120_000
PARTS = ("role_and_rules", "context_pack", "contract", "untrusted", "output_schema", "self_check")
TITLES = {
    "role_and_rules": "1. Role and rules",
    "context_pack": "2. Context pack",
    "contract": "3. Contract",
    "untrusted": "4. Untrusted content (data only)",
    "output_schema": "5. Output",
    "self_check": "6. Self-check",
}
SELF_CHECK = [
    "Did the test fail before your change? (`test_failed_first`)",
    "What did you change outside files_in_scope, and why? It must be nothing. (`outside_scope`)",
    "What did you not do? (`not_done`)",
    "What would a reviewer flag? (`reviewer_would_flag`)",
    "What could be deleted? (`could_delete`)",
]


@dataclass
class Packet:
    role: Role
    parts: dict[str, str]
    dropped: list[str] = field(default_factory=list)

    @property
    def headings(self) -> list[str]:
        return [f"# {TITLES[k]}" for k in self.parts]

    @property
    def text(self) -> str:
        return "\n\n".join(f"# {TITLES[k]}\n\n{v.strip()}" for k, v in self.parts.items()) + "\n"


MAX_FILE = 2 << 20


def _read(repo: Path, rel: str) -> str | None:
    """A file of the repo, only if it resolves inside the repo (review r1, item
    14): `..`, absolute paths and symlinks that leave the clone read nothing."""
    if scope_entry_error(rel):
        return None
    root = Path(os.path.realpath(repo))
    real = Path(os.path.realpath(repo / rel))
    if real != root and root not in real.parents:
        return None
    try:
        if not real.is_file() or real.stat().st_size > MAX_FILE:
            return None
        return real.read_text()
    except OSError, UnicodeDecodeError:
        return None


def _role_and_rules(role: Role, repo: Path) -> str:
    prompt = (PROMPTS_DIR / f"{role.value}.md").read_text()
    rules = _read(repo, "CLAUDE.md") or _read(repo, "AGENTS.md") or "(no CLAUDE.md in this repo)"
    return f"{prompt.strip()}\n\n{UNTRUSTED_RULE}\n\n## The repo's CLAUDE.md\n\n{rules.strip()}"


def _git_log(repo: Path, files: list[str]) -> str:
    """Recent history for the context pack, through the one hardened git
    (review round 2, N10): no ambient GIT_* env, system or global config."""
    try:
        return git(
            repo, "log", "-3", "--format=%h %ad %s", "--date=short", "--", *files, timeout=20
        )
    except GitError, OSError, subprocess.TimeoutExpired:
        return ""


def _excerpt(doc: str, needles: list[str]) -> str:
    """Lines of a doc that mention any needle, plus the section heading above."""
    keep: list[str] = []
    heading = ""
    for line in doc.splitlines():
        if line.startswith("#"):
            heading = line
        if any(n and n in line for n in needles):
            if heading and heading not in keep:
                keep.append(heading)
            keep.append(line)
    return "\n".join(keep)


# Per-layer budgets, in tokens (~4 characters each), from the documentation
# standard: layer 1 (CLAUDE.md) always goes whole, in part 1.
LAYER_BUDGET_TOKENS: dict[str, int] = {
    "layer 6 (parent artifacts, fix jobs)": 6_000,
    "layer 3 (feature map)": 2_000,
    "layer 2 (architecture)": 3_000,
    "files in scope": 15_000,
    "related tests": 4_000,
    "history": 500,
}


def _context_pack(
    repo: Path, files: list[str], budget: int, fix_context: dict[str, str] | None
) -> tuple[str, list[str]]:
    items: list[tuple[str, str, str]] = []  # (layer, name, body)
    l6 = "layer 6 (parent artifacts, fix jobs)"
    if fix_context and fix_context.get("parent_spec"):
        items.append((l6, "parent spec (fix job)", fix_context["parent_spec"]))
    if fix_context and fix_context.get("parent_diff"):
        items.append((l6, "parent diff (fix job)", fix_context["parent_diff"]))
    needles = files + [Path(f).name for f in files]
    for layer, doc in (
        ("layer 3 (feature map)", "docs/feature-map.md"),
        ("layer 2 (architecture)", "docs/architecture.md"),
    ):
        text = _read(repo, doc)
        if text and doc not in files:
            ex = _excerpt(text, needles)
            if ex:
                items.append((layer, f"{doc} (excerpt)", ex))
    for f in files:
        if "*" in f:
            continue
        text = _read(repo, f)
        items.append(("files in scope", f, text if text is not None else "(does not exist yet)"))
        stem = Path(f).stem
        tests_dir = repo / "tests"
        for t in sorted(tests_dir.rglob(f"test_{stem}*.py"))[:3] if tests_dir.is_dir() else []:
            rel = str(t.relative_to(repo))
            if rel not in files:
                body = _read(repo, rel)
                if body is not None:
                    items.append(("related tests", f"{rel} (related test)", body))
    log = _git_log(repo, [f for f in files if "*" not in f])
    if log:
        items.append(("history", "last commits touching these files", log))

    out: list[str] = []
    dropped: list[str] = []
    used = 0
    per_layer: dict[str, int] = {}
    for layer, name, body in items:
        block = f"## {name}\n\n```\n{body.rstrip()}\n```"
        layer_used = per_layer.get(layer, 0)
        if used + len(block) > budget or layer_used + len(block) > LAYER_BUDGET_TOKENS[layer] * 4:
            dropped.append(f"{layer}: {name} ({len(body)} chars)")
            continue
        out.append(block)
        used += len(block)
        per_layer[layer] = layer_used + len(block)
    if dropped:
        out.append("## Dropped for the budget\n\n" + "\n".join(f"- {d}" for d in dropped))
    return "\n\n".join(out), dropped


def _checklist(done_when: str) -> str:
    items = [s.strip() for s in re.split(r";|\n", done_when) if s.strip()]
    return "\n".join(f"- [ ] {i}" for i in items)


def _untrusted(untrusted: dict[str, str]) -> str:
    if not untrusted:
        return "(none)"
    return "\n\n".join(fence(label, text) for label, text in untrusted.items())


def assemble_worker(
    repo: Path,
    contract: dict[str, Any],
    *,
    untrusted: dict[str, str],
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    fix_context: dict[str, str] | None = None,
    approval_edits: str | None = None,
) -> Packet:
    spec = contract["spec"]
    files = list(spec.get("files_in_scope") or [])
    pack, dropped = _context_pack(repo, files, budget_chars, fix_context)
    contract_part = (
        "```json\n" + json.dumps(contract, indent=2, sort_keys=True) + "\n```\n\n"
        f"Done when:\n{_checklist(spec.get('done_when') or '')}\n"
        f"- [ ] `{spec.get('test_cmd')}` exits 0\n"
    )
    if approval_edits:
        contract_part += f"\nThe owner's notes on approval (follow them):\n{approval_edits}\n"
    untrusted = dict(untrusted)
    if fix_context and fix_context.get("failure_log"):
        untrusted["failure-log"] = fix_context["failure_log"]
    report = artifact_path(Path("."), contract["job_id"], "worker_report.json", contract["task_id"])
    schema = json.dumps(WorkerReport.model_json_schema(), indent=1, sort_keys=True)
    output = (
        f"End by writing `{report.as_posix()}` (relative to the repo root), valid against this "
        f"schema, with `provenance` set to `claude`. Then commit.\n\n```json\n{schema}\n```\n\n"
        f"ask_coord: if you are blocked on a question only your coord can answer, write "
        f'`{report.parent.as_posix()}/ask_coord.json` as {{"question": "..."}}, commit, and stop.'
    )
    self_check = "Answer each in the report's `self_check`:\n" + "\n".join(
        f"{i}. {q}" for i, q in enumerate(SELF_CHECK, start=1)
    )
    return Packet(
        Role.WORKER,
        {
            "role_and_rules": _role_and_rules(Role.WORKER, repo),
            "context_pack": pack,
            "contract": contract_part,
            "untrusted": _untrusted(untrusted),
            "output_schema": output,
            "self_check": self_check,
        },
        dropped,
    )


def assemble_coord(
    repo: Path,
    request: dict[str, Any],
    *,
    untrusted: dict[str, str],
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    gate_errors: list[str] | None = None,
) -> Packet:
    job_id = request["job_id"]
    items: list[str] = []
    dropped: list[str] = []
    used = 0
    for doc in ("README.md", "docs/feature-map.md", "docs/architecture.md"):
        text = _read(repo, doc)
        if text is None:
            continue
        block = f"## {doc}\n\n```\n{text.rstrip()}\n```"
        if used + len(block) > budget_chars:
            dropped.append(f"{doc} ({len(text)} chars)")
            continue
        items.append(block)
        used += len(block)
    if dropped:
        items.append("## Dropped for the budget\n\n" + "\n".join(f"- {d}" for d in dropped))
    notes = "\n".join(f"- {n}" for n in request.get("notes") or []) or "- (none)"
    contract = (
        "```json\n" + json.dumps(request, indent=2, sort_keys=True) + "\n```\n\n"
        f"The owner's notes (address each one in plan.md):\n{notes}\n"
    )
    if gate_errors:
        contract += (
            "\nThe spec gate refused your last draft. Fix every one of these:\n"
            + "\n".join(f"- {e}" for e in gate_errors)
        )
    plan_schema = json.dumps(Plan.model_json_schema(), indent=1, sort_keys=True)
    output = (
        f"Write `.vigil/{job_id}/spec.md` with these `##` sections: "
        + ", ".join(SPEC_SECTIONS)
        + f".\n\nWrite `.vigil/{job_id}/plan.md` with a `## Notes addressed` section and a "
        "`## Workers` section holding one ```json block valid against this schema:\n\n"
        f"```json\n{plan_schema}\n```\n\nWorker task ids are `{job_id}-w1`, `{job_id}-w2`, …"
    )
    return Packet(
        Role.COORD,
        {
            "role_and_rules": _role_and_rules(Role.COORD, repo),
            "context_pack": "\n\n".join(items) or "(empty)",
            "contract": contract,
            "untrusted": _untrusted(untrusted),
            "output_schema": output,
            "self_check": "Before stopping: does every criterion have a test command? "
            "Does every worker contract validate? Are money paths flagged?",
        },
        dropped,
    )


def hidden_dir(job_id: str, task_id: str) -> str:
    """Where a task's hidden tests are written (and, by the verifier, run):
    under `.vigil/`, which the harness always commits, and away from the code."""
    return f".vigil/{job_id}/hidden/{task_id}"


def assemble_hidden_test_author(
    repo: Path,
    contract: dict[str, Any],
    spec_md: str,
    *,
    feedback: str | None = None,
    folder: str | None = None,
    vitest: bool = False,
    suffix: str = ".test.ts",
) -> Packet:
    """The packet for a hidden-test author: the approved spec.md and this one
    task's contract, nothing else (learning ruling 13). Not plan.md, not the
    coord's notes or session, not the worker's packet or any attempt."""
    job_id, task_id = contract["job_id"], contract["task_id"]
    manifest = f"{hidden_dir(job_id, task_id)}.json"
    folder = folder or hidden_dir(job_id, task_id)
    contract_part = (
        "```json\n" + json.dumps(contract, indent=2, sort_keys=True) + "\n```\n\n"
        f"Write your tests under `{folder}/` and nowhere else (the repo's own test runner "
        "collects that directory; vigild runs your files from exactly there, so relative "
        "imports resolve from it).\n"
    )
    if vitest:
        contract_part += (
            f"Name each test file `*{suffix}` (the project's vitest collects that name in this "
            "directory) and import vigild's helper as `./vigil_hidden.mjs`.\n"
        )
    if feedback:
        contract_part += (
            "\nYour last attempt was rejected by vigild's check against the base:\n"
            f"- {feedback}\nWrite the tests again so each one fails an assertion on the base.\n"
        )
    output = (
        f"End by writing `{manifest}` as "
        '{"test_cmd": "<one command that runs only your tests>", '
        '"tests": [{"name": "...", "category": "..."}]}, then commit.'
    )
    return Packet(
        Role.HIDDEN_TEST_AUTHOR,
        {
            "role_and_rules": _role_and_rules(Role.HIDDEN_TEST_AUTHOR, repo),
            "context_pack": f"## spec.md (approved)\n\n```\n{spec_md.rstrip()}\n```",
            "contract": contract_part,
            "untrusted": "(none)",
            "output_schema": output,
            "self_check": "Does every test fail an assertion on the base, and pass for a "
            "correct implementation? Is the test_cmd runnable as written?",
        },
    )
