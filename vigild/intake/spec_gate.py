"""The spec gate.

Per task: a task without `test_cmd` and `done_when` does not run; it becomes
a spec-writing job (routing rule 1).

Per job (stage 1 acceptance): spec.md has every section, every acceptance
criterion has a test command, every worker contract in plan.md validates,
worker scopes sit inside the spec's scope (strict globs, no `..` or absolute
paths), dependencies name real workers and form no cycle, parallel workers'
scopes are provably disjoint and their interfaces pinned, every test_cmd can
run in the repo's dev image, and the money paths the spec flags are exactly
the ones the path rules find.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field

from vigild.contract.models import BUILT_VERIFY_LEVELS, Task, WorkerContract
from vigild.intake.paths import globs_overlap, in_scope, matches_any, scope_entry_error
from vigild.jobs.artifacts import SPEC_SECTIONS, PlanDoc, SpecDoc
from vigild.shellwords import Command, parse
from vigild.verifier import ReadFile, plan_reports, runner_overlap


@dataclass(frozen=True)
class GateResult:
    ok: bool
    needs_spec: bool = False
    errors: list[str] = field(default_factory=list)


# Commands that can't be a test on their own: they exit 0 (or a fixed code)
# whatever the code does, or hide the real command from the gate.
NO_OP = {"true", ":", "echo", "printf", "exit", "test", "[", "cat", "head", "tail"}
SHELLS = {"sh", "bash", "dash", "zsh"}
CONTROL = {
    "if", "then", "else", "elif", "fi", "while", "until", "do", "done", "case", "esac",
    "for", "set", "trap", "function", "eval",
}  # fmt: skip


def _trivial(c: Command) -> bool:
    if not c.words:
        return True
    if c.program in SHELLS and "-c" in c.args:
        return True
    return c.program in NO_OP


def bad_test_cmd(cmd: str | None) -> str | None:
    """Why a test_cmd can't be a test, or None (review rounds 2 and 3, N2/F2).

    The verifier runs test_cmd under `bash -o pipefail -o errexit -c`, so a
    failure anywhere in a pipe or a list fails the run. What's left to refuse:
    `&` (the test runs in the background and the line exits 0), `||` (a
    failure is caught: `|| true` and its relatives), and a line whose every
    command can't fail on the code's account: `true`, `:`, `echo`, `printf`,
    `exit <n>`, `test`/`[ … ]`, `cat`/`head`/`tail`, `/bin/true`, `env true`,
    or a `sh -c`/`bash -c` that hides the real command from this check."""
    if not (cmd or "").strip():
        return "test_cmd is empty"
    assert cmd is not None
    line = parse(cmd)
    shown = cmd.strip()
    if "&" in line.ops:
        return f"test_cmd `{shown}` runs something in the background (`&`), so it exits 0"
    if "||" in line.ops:
        return f"test_cmd `{shown}` catches a failure with `||`; a test must fail when it fails"
    for c in line.commands:
        # round 4, item 1: forms that change or hide an exit status
        if c.text.strip().startswith("!") or (c.words and c.words[0] in CONTROL):
            word = "!" if c.text.strip().startswith("!") else c.words[0]
            return f"test_cmd `{shown}` uses `{word}`, which can hide a failing exit"
    if all(_trivial(c) for c in line.commands):
        return f"test_cmd `{shown}` can't fail on the code's account: it's a no-op"
    return None


def test_cmd_problem(cmd: str | None, read: ReadFile | None = None) -> str | None:
    """bad_test_cmd, plus the verifier's own report resolver (round 4, item 6):
    a line it can't attach a test report to is refused here, before any
    spend. Without the base's files (`read`), package scripts are taken on
    trust here and checked again by the verifier."""
    if why := bad_test_cmd(cmd):
        return why
    plan = plan_reports(str(cmd), read)
    problems = (
        plan.problems
        if read is not None
        else [p for p in plan.problems if "can't be read" not in p]
    )
    if problems:
        return f"test_cmd `{str(cmd).strip()}`: " + "; ".join(problems)
    return None


def runner_files_named(
    scope: Sequence[str], runner_config: Sequence[str], test_cmd: str
) -> list[str]:
    """The literal entries in a worker's scope that decide how tests run: the
    runner-file overlap, computed by the verifier's own matcher."""
    return runner_overlap(scope, runner_config, test_cmd)


MFF_REASON_RULE = (
    "must_fail_first is false, so must_fail_first_reason is required: "
    'say why, for example "tests only, code unchanged"'
)


def check_task(task: Task) -> GateResult:
    errors = [
        f"{key} is required before a task can run"
        for key in ("test_cmd", "done_when")
        if not (getattr(task.spec, key) or "").strip()
    ]
    if (task.spec.test_cmd or "").strip() and (why := test_cmd_problem(task.spec.test_cmd)):
        errors.append(why)
    if not task.spec.must_fail_first and not (task.spec.must_fail_first_reason or "").strip():
        errors.append(MFF_REASON_RULE)
    return GateResult(ok=not errors, needs_spec=bool(errors), errors=errors)


TICKED = re.compile(r"`[^`]+`")


def _scope_within(entry: str, spec_scope: Sequence[str]) -> bool:
    """A worker's entry sits inside the spec's scope: a literal path must be in
    scope; a glob must equal a spec entry or be covered by a `**` entry."""
    if entry in spec_scope or (not set("*?[") & set(entry) and in_scope(entry, spec_scope)):
        return True
    return any(s.endswith("/**") and entry.startswith(s[:-2]) for s in spec_scope)


def cycles(workers: list[WorkerContract]) -> list[str]:
    deps = {w.task_id: [d for d in w.depends_on] for w in workers}
    found: list[str] = []
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for d in deps.get(n, []):
            if d not in deps:
                continue
            if state.get(d) == 1:
                found.append(
                    " -> ".join([*path[path.index(d) :], d]) if d in path else f"{n} -> {d}"
                )
            elif state.get(d) is None:
                visit(d, [*path, d])
        state[n] = 2

    for n in deps:
        if state.get(n) is None:
            visit(n, [n])
    return found


def _ordered(workers: list[WorkerContract]) -> set[tuple[str, str]]:
    """Pairs (x, y) where y runs after x, through any chain of depends_on."""
    deps = {w.task_id: set(w.depends_on) for w in workers}
    after: set[tuple[str, str]] = set()
    for w in workers:
        stack, seen = list(deps[w.task_id]), set()
        while stack:
            d = stack.pop()
            if d in seen or d not in deps:
                continue
            seen.add(d)
            after.add((d, w.task_id))
            stack.extend(deps[d])
    return after


def parallel_pairs(workers: list[WorkerContract]) -> list[tuple[WorkerContract, WorkerContract]]:
    order = _ordered(workers)
    return [
        (a, b)
        for i, a in enumerate(workers)
        for b in workers[i + 1 :]
        if (a.task_id, b.task_id) not in order and (b.task_id, a.task_id) not in order
    ]


def check_job_spec(
    spec: SpecDoc,
    plan: PlanDoc,
    money_globs: Sequence[str],
    *,
    missing_tools: Callable[[str], list[str]] | None = None,
    read: ReadFile | None = None,
    existing_task_ids: Collection[str] = (),
) -> list[str]:
    errors: list[str] = []
    for entry in spec.files_in_scope:
        if err := scope_entry_error(entry):
            errors.append(f"spec.md files in scope: {err}")
    for sec in SPEC_SECTIONS:
        if sec not in spec.sections:
            errors.append(f"spec.md is missing the '## {sec}' section")
    if not spec.criteria:
        errors.append("spec.md has no acceptance criteria")
    for c in spec.criteria:
        if not c.test_cmd:
            errors.append(f"spec.md {c.ident} has no test command (`test: \\`cmd\\``)")
        elif why := test_cmd_problem(c.test_cmd, read):
            errors.append(f"spec.md {c.ident}: {why}")

    errors.extend(plan.errors)
    if not plan.workers and not plan.errors:
        errors.append("plan.md has no worker contracts")
    ids = [w.task_id for w in plan.workers]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"plan.md repeats worker {dup}")
    # phase 1 run finding #4: a task id belongs to one task for the life of the job,
    # whatever its state; a re-plan names its workers anew (and may depend on a PR
    # that already stands).
    taken = set(existing_task_ids)
    for reused in sorted({i for i in ids if i in taken}):
        errors.append(
            f"worker {reused} already exists in this job (merged, failed or superseded); "
            "name every worker in a re-plan with a new task id, never an old one"
        )
    for w in plan.workers:
        if why := test_cmd_problem(w.test_cmd, read):
            errors.append(f"worker {w.task_id}: {why}")
        if not w.must_fail_first and not (w.must_fail_first_reason or "").strip():
            errors.append(f"worker {w.task_id}: {MFF_REASON_RULE}")
        for dep in w.depends_on:
            if dep not in ids and dep not in taken:
                errors.append(f"worker {w.task_id} depends on unknown worker {dep}")
        for f in w.files_in_scope + w.out_of_scope:
            if err := scope_entry_error(f):
                errors.append(f"worker {w.task_id}: {err}")
        for f in w.files_in_scope:
            if not scope_entry_error(f) and not _scope_within(f, spec.files_in_scope):
                errors.append(f"worker {w.task_id} scope {f} is outside spec.md's files in scope")
        if missing_tools is not None:
            lacking = missing_tools(w.test_cmd)
            if lacking:
                errors.append(
                    f"worker {w.task_id} test_cmd needs {lacking}, which the repo's dev image "
                    "doesn't have; use a check the image can run"
                )
    for loop in cycles(plan.workers):
        errors.append(f"plan.md has a dependency cycle: {loop}")
    for c in spec.criteria:
        if c.test_cmd and missing_tools is not None and missing_tools(c.test_cmd):
            errors.append(
                f"spec.md {c.ident} test needs {missing_tools(c.test_cmd)}, which the repo's "
                "dev image doesn't have"
            )

    # Single writer by default; parallel writers only with disjoint files and
    # interfaces pinned in spec.md (owner decisions 2026-10-22).
    pairs = parallel_pairs(plan.workers)
    for a, b in pairs:
        shared = sorted(
            {
                f"{f} / {g}"
                for f in a.files_in_scope
                for g in b.files_in_scope
                if globs_overlap(f, g)
            }
        )
        if shared:
            errors.append(
                f"workers {a.task_id} and {b.task_id} run in parallel but their files are not "
                f"disjoint: {shared}; order them with depends_on or split the files"
            )
    if pairs and not any(TICKED.search(i) for i in spec.interfaces):
        errors.append(
            "parallel workers need their shared interfaces pinned in '## Interfaces': "
            "name the files, signatures or formats in backticks"
        )

    # The verification ladder: every criterion's level, and why not higher at 0.
    levels = {v.criterion: v for v in plan.verify}
    for c in spec.criteria:
        if c.ident not in levels and not plan.errors:
            errors.append(
                f"plan.md has no verify entry for {c.ident} (verify names "
                f"{sorted(levels) or 'nothing'}; use the spec's ids: AC1, AC2, …)"
            )
    for v in plan.verify:
        if v.level not in BUILT_VERIFY_LEVELS:
            errors.append(f"{v.criterion}: verify level {v.level} isn't built yet (phase 1: 0)")

    in_scope = set(spec.files_in_scope) | {f for w in plan.workers for f in w.files_in_scope}
    found = {f for f in in_scope if matches_any(f, money_globs)}
    flagged = set(spec.money_paths)
    for f in sorted(found - flagged):
        errors.append(f"{f} is a money/auth/consent path but spec.md does not flag it")
    for f in sorted(flagged - found):
        errors.append(f"spec.md flags {f} as a money path, but the path rules don't match it")
    return errors
