"""The spec gate: per task (test_cmd + done_when) and per job (spec.md + plan.md)."""

from __future__ import annotations

import json

import pytest

from vigild.contract.models import Task
from vigild.intake.spec_gate import bad_test_cmd, check_job_spec, check_task
from vigild.jobs.artifacts import parse_plan, parse_spec

MONEY = ["src/billing/**", "src/auth/**", "**/payments/**", "**/consent/**"]


def _task(**spec: object) -> Task:
    base: dict[str, object] = {
        "goal": "g",
        "test_cmd": "pytest -q",
        "done_when": "green",
        "files_in_scope": ["docs/a.md"],
    }
    base.update(spec)
    return Task.model_validate(
        {
            "task_id": "t1",
            "repo": "o/r",
            "base": "main",
            "why": "w",
            "value": 3,
            "risk": 1,
            "spec": base,
        }
    )


def test_task_with_test_and_done_passes() -> None:
    r = check_task(_task())
    assert r.ok and not r.needs_spec


def test_task_without_test_cmd_needs_a_spec() -> None:
    r = check_task(_task(test_cmd=None))
    assert not r.ok and r.needs_spec
    assert "test_cmd" in r.errors[0]


def test_task_without_done_when_needs_a_spec() -> None:
    r = check_task(_task(done_when="  "))
    assert not r.ok and r.needs_spec


SPEC = """<!-- provenance: claude -->
# Spec: docs for the rate limit

## Goal
Document the limit.

## Non-goals
- Changing the limit.

## Acceptance criteria
- AC1: the feature map lists the limit. test: `pytest -q tests/test_feature_map.py`
- AC2: the runbook says how to change it. test: `pytest -q tests/test_runbook.py`

## Files in scope
- `docs/feature-map.md`
- `docs/runbook.md`

## Files out of scope
- `src/**`

## Money paths
none

## Risks
- Stale numbers.

## Interfaces
- None shared: each worker edits only `docs/feature-map.md` or `docs/runbook.md`.

## Rollback
Revert the PR.
"""

PLAN = """<!-- provenance: claude -->
# Plan

## Notes addressed
- "keep it short": adopted, one paragraph each.

## Workers
```json
{"workers": [
  {"task_id": "pa-0001-w1", "goal": "feature map row", "why": "AC1", "value": 3, "risk": 1,
   "size": "XS", "test_cmd": "pytest -q tests/test_feature_map.py",
   "done_when": "AC1 holds", "files_in_scope": ["docs/feature-map.md"], "must_fail_first": true},
  {"task_id": "pa-0001-w2", "goal": "runbook section", "why": "AC2", "value": 3, "risk": 1,
   "size": "XS", "test_cmd": "pytest -q tests/test_runbook.py",
   "done_when": "AC2 holds", "files_in_scope": ["docs/runbook.md"], "must_fail_first": true}
],
 "verify": [
  {"criterion": "AC1", "level": 0, "why_not_higher": "a grep over a doc has no property to state"},
  {"criterion": "AC2", "level": 0, "why_not_higher": "same"}
]}
```
"""


def test_valid_spec_and_plan_pass() -> None:
    assert check_job_spec(parse_spec(SPEC), parse_plan(PLAN), MONEY) == []


def test_criterion_without_a_test_command_fails() -> None:
    spec = SPEC.replace(" test: `pytest -q tests/test_runbook.py`", "")
    errors = check_job_spec(parse_spec(spec), parse_plan(PLAN), MONEY)
    assert any("AC2" in e and "test" in e for e in errors)


def test_missing_section_fails() -> None:
    spec = SPEC.replace("## Rollback\nRevert the PR.\n", "")
    errors = check_job_spec(parse_spec(spec), parse_plan(PLAN), MONEY)
    assert any("Rollback" in e for e in errors)


def test_invalid_worker_contract_fails() -> None:
    plan = PLAN.replace(
        '"value": 3, "risk": 1,\n   "size": "XS", "test_cmd": "pytest -q tests/test_runbook',
        '"value": 9, "risk": 1,\n   "size": "XS", "test_cmd": "pytest -q tests/test_runbook',
    )
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("pa-0001-w2" in e for e in errors)


def test_plan_without_workers_fails() -> None:
    errors = check_job_spec(parse_spec(SPEC), parse_plan("# Plan\n"), MONEY)
    assert any("worker" in e for e in errors)


def test_unflagged_money_path_fails() -> None:
    plan = PLAN.replace(
        '"files_in_scope": ["docs/runbook.md"]', '"files_in_scope": ["src/billing/x.py"]'
    )
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("src/billing/x.py" in e and "money" in e for e in errors)


def test_flagged_money_path_that_matches_passes() -> None:
    plan = PLAN.replace(
        '"files_in_scope": ["docs/runbook.md"]', '"files_in_scope": ["src/billing/x.py"]'
    )
    spec = SPEC.replace("## Money paths\nnone", "## Money paths\n- `src/billing/x.py`").replace(
        "- `docs/runbook.md`\n", "- `docs/runbook.md`\n- `src/billing/x.py`\n"
    )
    assert check_job_spec(parse_spec(spec), parse_plan(plan), MONEY) == []


def test_worker_scope_outside_spec_scope_fails() -> None:
    plan = PLAN.replace('"files_in_scope": ["docs/runbook.md"]', '"files_in_scope": ["README.md"]')
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("README.md" in e for e in errors)


def test_dependency_on_unknown_worker_fails() -> None:
    plan = PLAN.replace(
        '"must_fail_first": true}\n]', '"must_fail_first": true, "depends_on": ["nope"]}\n]'
    )
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("nope" in e for e in errors)


# --- owner decisions 2026-10-22: parallel writers and the verification ladder ---


def test_parallel_workers_with_overlapping_files_fail() -> None:
    plan = PLAN.replace(
        '"files_in_scope": ["docs/runbook.md"]', '"files_in_scope": ["docs/feature-map.md"]'
    )
    spec = SPEC.replace("- `docs/runbook.md`\n", "", 1)
    errors = check_job_spec(parse_spec(spec), parse_plan(plan), MONEY)
    assert any("disjoint" in e and "pa-0001-w1" in e and "pa-0001-w2" in e for e in errors)


def test_overlapping_workers_are_fine_when_ordered() -> None:
    plan = PLAN.replace(
        '"files_in_scope": ["docs/runbook.md"]', '"files_in_scope": ["docs/feature-map.md"]'
    )
    plan = plan.replace(
        '"must_fail_first": true}\n]', '"must_fail_first": true, "depends_on": ["pa-0001-w1"]}\n]'
    )
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert not any("disjoint" in e for e in errors)


def test_parallel_workers_need_pinned_interfaces() -> None:
    spec = SPEC.replace(
        "## Interfaces\n- None shared: each worker edits only `docs/feature-map.md` or `docs/runbook.md`.\n\n",
        "",
    )
    errors = check_job_spec(parse_spec(spec), parse_plan(PLAN), MONEY)
    assert any("Interfaces" in e for e in errors)


def test_a_single_writer_needs_no_interfaces_section() -> None:
    spec = SPEC.replace(
        "## Interfaces\n- None shared: each worker edits only `docs/feature-map.md` or `docs/runbook.md`.\n\n",
        "",
    )
    plan = PLAN.replace(
        '"must_fail_first": true}\n]', '"must_fail_first": true, "depends_on": ["pa-0001-w1"]}\n]'
    )
    assert check_job_spec(parse_spec(spec), parse_plan(plan), MONEY) == []


def test_every_criterion_needs_a_verify_entry() -> None:
    plan = PLAN.replace(',\n  {"criterion": "AC2", "level": 0, "why_not_higher": "same"}', "")
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("AC2" in e and "verify" in e for e in errors)


def test_level_zero_must_say_why_not_higher() -> None:
    plan = PLAN.replace('"why_not_higher": "same"', '"why_not_higher": ""')
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("why_not_higher" in e for e in errors)


def test_levels_not_built_yet_are_refused() -> None:
    plan = PLAN.replace(
        '{"criterion": "AC2", "level": 0, "why_not_higher": "same"}',
        '{"criterion": "AC2", "level": 1}',
    )
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("level 1" in e for e in errors)


def test_task_spec_has_a_verify_field_at_level_zero() -> None:
    t = _task()
    assert t.spec.verify.level == 0


# --- review round 1 -----------------------------------------------------------------------


def _with_scope(entry: str) -> str:
    return PLAN.replace('"files_in_scope": ["docs/runbook.md"]', f'"files_in_scope": ["{entry}"]')


def test_scope_entries_with_dotdot_or_absolute_paths_fail() -> None:  # item 14
    for bad in ("../etc/passwd", "/etc/passwd"):
        spec = SPEC.replace("- `docs/runbook.md`\n", f"- `docs/runbook.md`\n- `{bad}`\n")
        errors = check_job_spec(parse_spec(spec), parse_plan(_with_scope(bad)), MONEY)
        assert any(bad in e and "relative" in e for e in errors), errors


def test_worker_scope_must_sit_inside_spec_scope_strictly() -> None:  # item 14
    spec = SPEC.replace("- `docs/runbook.md`\n", "- `docs/*.md`\n")
    errors = check_job_spec(parse_spec(spec), parse_plan(_with_scope("docs/sub/x.md")), MONEY)
    assert any("docs/sub/x.md" in e for e in errors)


def test_parallel_globs_that_may_overlap_fail() -> None:  # item 15
    plan = PLAN.replace(
        '"files_in_scope": ["docs/feature-map.md"]', '"files_in_scope": ["docs/*.md"]'
    )
    plan = plan.replace('"files_in_scope": ["docs/runbook.md"]', '"files_in_scope": ["docs/r*"]')
    spec = SPEC.replace("- `docs/feature-map.md`\n- `docs/runbook.md`\n", "- `docs/**`\n")
    errors = check_job_spec(parse_spec(spec), parse_plan(plan), MONEY)
    assert any("disjoint" in e for e in errors)


@pytest.mark.parametrize(
    "deps",
    [
        (
            '"must_fail_first": true}\n]',
            '"must_fail_first": true, "depends_on": ["pa-0001-w2"]}\n]',
        ),
        (
            '"files_in_scope": ["docs/feature-map.md"], "must_fail_first": true}',
            '"files_in_scope": ["docs/feature-map.md"], "must_fail_first": true, "depends_on": ["pa-0001-w1"]}',
        ),
    ],
)
def test_dependency_cycles_fail(deps: tuple[str, str]) -> None:  # item 17
    plan = PLAN.replace(*deps)
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("cycle" in e for e in errors), errors


def test_a_two_worker_cycle_fails() -> None:  # item 17
    plan = PLAN.replace(
        '"files_in_scope": ["docs/feature-map.md"], "must_fail_first": true}',
        '"files_in_scope": ["docs/feature-map.md"], "must_fail_first": true, "depends_on": ["pa-0001-w2"]}',
    ).replace(
        '"must_fail_first": true}\n]', '"must_fail_first": true, "depends_on": ["pa-0001-w1"]}\n]'
    )
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("cycle" in e for e in errors)


def test_interfaces_must_name_something() -> None:  # nit
    assert check_job_spec(parse_spec(SPEC), parse_plan(PLAN), MONEY) == []
    vague = SPEC.replace(
        "- None shared: each worker edits only `docs/feature-map.md` or `docs/runbook.md`.",
        "- stuff",
    )
    errors = check_job_spec(parse_spec(vague), parse_plan(PLAN), MONEY)
    assert any("Interfaces" in e for e in errors)


def test_a_test_cmd_the_dev_image_cannot_run_fails() -> None:  # blocker 4
    plan = PLAN.replace(
        'pytest -q tests/test_runbook.py",\n   "done_when"', 'pnpm test",\n   "done_when"'
    )
    errors = check_job_spec(
        parse_spec(SPEC),
        parse_plan(plan),
        MONEY,
        missing_tools=lambda cmd: ["pnpm"] if "pnpm" in cmd else [],
    )
    assert any("pnpm" in e and "dev image" in e for e in errors), errors


# --- review round 2, N2: a test_cmd that can't fail is refused before approval ------------

NO_OPS = [
    "",
    "   ",
    "true",
    ":",
    "exit 0",
    "exit",
    "echo ok",
    "printf done",
    "true && echo ok",
    "true; :",
    "(true)",
    "{ true; }",
    "pytest -q || true",
    "make test || echo failed",
    "TERM=dumb true",
    # review round 3, F2: the reviewer's always-0 commands
    "pytest &",
    "true & pytest",
    "sh -c true",
    'bash -c "exit 0"',
    "bash -c 'pytest -q'",
    "test 1",
    "[ 1 ]",
    "test -f docs/a.md",
    "/bin/true",
    "env true",
    "exit 00",
    "exit 1",
    "cat /dev/null",
    "head -c 0 /dev/zero",
    "tail -n 1 README.md",
    "pytest -q || :",
    "test 1 && echo ok",
]


@pytest.mark.parametrize("cmd", NO_OPS)
def test_a_no_op_test_cmd_is_refused(cmd: str) -> None:
    assert bad_test_cmd(cmd), cmd
    r = check_task(_task(test_cmd=cmd))
    assert not r.ok and any("test_cmd" in e for e in r.errors)
    plan = PLAN.replace("\"test_cmd\": \"pytest -q tests/test_runbook.py\"",
                        f'"test_cmd": {json.dumps(cmd)}')  # fmt: skip
    spec = SPEC.replace("test: `pytest -q tests/test_runbook.py`", f"test: `{cmd}`")
    errors = check_job_spec(parse_spec(spec), parse_plan(plan), MONEY)
    assert any("pa-0001-w2" in e and "test_cmd" in e for e in errors), errors
    if cmd.strip():
        assert any("AC2" in e for e in errors), errors


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q tests/test_feature_map.py",
        "pytest -q tests/test_a.py",
        "true && pytest -q",
        "echo start; pytest -q",
        "sh scripts/check.sh && echo ok",
        # under `bash -o pipefail -o errexit` these are honest (round 3, F2)
        "pnpm test 2>&1 | tail -n 50",
        "pytest | cat",
        "grep -q x docs/a.md; exit 0",
        "pnpm -C server test &> /tmp/log && echo done",
    ],
)
def test_a_real_test_cmd_passes(cmd: str) -> None:
    assert bad_test_cmd(cmd) is None, cmd


def test_the_refusals_say_why() -> None:
    assert "background" in str(bad_test_cmd("pytest &"))
    assert "||" in str(bad_test_cmd("pytest || true"))
    assert "can't fail" in str(bad_test_cmd("/bin/true"))


def test_the_worker_contract_refuses_a_blank_test_cmd() -> None:
    from pydantic import ValidationError

    from vigild.contract.models import WorkerContract

    base = {"task_id": "pa-0001-w1", "goal": "g", "why": "w", "value": 3, "risk": 1,
            "size": "XS", "done_when": "d", "files_in_scope": ["docs/a.md"]}  # fmt: skip
    for bad in ("", "  \n"):
        with pytest.raises(ValidationError):
            WorkerContract.model_validate({**base, "test_cmd": bad})


# --- review round 3: the verifier needs a test report; say so at the gate -----------------


@pytest.mark.parametrize("cmd", ["grep -q x docs/a.md", "sh scripts/check.sh", "make check"])
def test_a_test_cmd_no_runner_reports_on_is_refused(cmd: str) -> None:
    r = check_task(_task(test_cmd=cmd))
    assert not r.ok and any("test runner" in e for e in r.errors), r.errors
    plan = PLAN.replace('"test_cmd": "pytest -q tests/test_runbook.py"', f'"test_cmd": "{cmd}"')
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("pa-0001-w2" in e and ("test runner" in e or "test_cmd" in e) for e in errors), (
        errors
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q tests",
        "uv run pytest -q",
        "python -m pytest tests/test_a.py",
        "pnpm -C server test",
        "npx vitest run test/a.test.ts",
        "pnpm -C server exec vitest run",
        "cd server && pnpm test 2>&1 | tail -n 50",
    ],
)
def test_a_reporting_runner_is_accepted(cmd: str) -> None:
    # without the base's files the gate takes a pnpm script on trust; with
    # them it checks the script (tests/test_verifier.py, the resolver)
    from vigild.intake import spec_gate

    assert spec_gate.test_cmd_problem(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "! pytest -q",
        "if pytest -q; then :; fi",
        "set +e; pytest -q",
        'trap "exit 0" EXIT; pytest -q',
        "while pytest -q; do :; done",
    ],
)
def test_control_flow_that_hides_an_exit_is_refused(cmd: str) -> None:  # round 4, item 1
    assert bad_test_cmd(cmd), cmd


# --- phase 1 run finding #4: a plan never reuses a task id ---------------------------------


def test_a_plan_that_reuses_an_existing_task_id_is_refused() -> None:
    errors = check_job_spec(
        parse_spec(SPEC), parse_plan(PLAN), MONEY, existing_task_ids={"pa-0001-w1"}
    )
    assert any("pa-0001-w1" in e and "already exists" in e for e in errors)
    assert not any("pa-0001-w2" in e and "already exists" in e for e in errors)


def test_a_superseded_or_failed_tasks_id_is_reused_no_more_than_a_merged_ones() -> None:
    # the gate sees ids only: every existing task, whatever its state, blocks its id
    errors = check_job_spec(
        parse_spec(SPEC),
        parse_plan(PLAN),
        MONEY,
        existing_task_ids=["pa-0001-w1", "pa-0001-w2"],
    )
    assert sum("already exists" in e for e in errors) == 2


def test_a_plan_that_repeats_a_worker_id_is_refused() -> None:
    plan = PLAN.replace('"task_id": "pa-0001-w2"', '"task_id": "pa-0001-w1"')
    errors = check_job_spec(parse_spec(SPEC), parse_plan(plan), MONEY)
    assert any("repeats worker pa-0001-w1" in e for e in errors)


def test_fresh_ids_pass_beside_existing_ones() -> None:
    plan = PLAN.replace("pa-0001-w", "pa-0001-w1")  # w11, w12
    errors = check_job_spec(
        parse_spec(SPEC), parse_plan(plan), MONEY, existing_task_ids={"pa-0001-w1", "pa-0001-w2"}
    )
    assert errors == []


def test_a_new_worker_may_depend_on_an_existing_task() -> None:
    plan = PLAN.replace("pa-0001-w2", "pa-0001-w3").replace(
        '"task_id": "pa-0001-w1"', '"task_id": "pa-0001-w4", "depends_on": ["pa-0001-w2"]'
    )
    errors = check_job_spec(
        parse_spec(SPEC), parse_plan(plan), MONEY, existing_task_ids={"pa-0001-w1", "pa-0001-w2"}
    )
    assert errors == []


# --- #31 item 2: must_fail_first off states why ---------------------------------------------


def _off(reason: str | None) -> str:
    extra = "" if reason is None else f', "must_fail_first_reason": {json.dumps(reason)}'
    return PLAN.replace(
        '"files_in_scope": ["docs/runbook.md"], "must_fail_first": true}',
        f'"files_in_scope": ["docs/runbook.md"], "must_fail_first": false{extra}}}',
    )


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_must_fail_first_off_needs_a_reason(reason: str | None) -> None:
    errors = check_job_spec(parse_spec(SPEC), parse_plan(_off(reason)), MONEY)
    assert any(
        "pa-0001-w2" in e and "must_fail_first" in e and "must_fail_first_reason" in e
        for e in errors
    ), errors


def test_must_fail_first_off_with_a_reason_passes() -> None:
    plan = parse_plan(_off("tests only, code unchanged"))
    assert check_job_spec(parse_spec(SPEC), plan, MONEY) == []


def test_a_task_with_it_off_and_no_reason_needs_a_spec() -> None:
    res = check_task(_task(must_fail_first=False))
    assert not res.ok and any("must_fail_first_reason" in e for e in res.errors)
    assert check_task(_task(must_fail_first=False, must_fail_first_reason="tests only")).ok


BARE_DIR = "use dir/** (a bare directory matches only itself)"


def _is_dir(path: str) -> bool:
    return path in {"tests/gateway", "tests"}


def _scoped(entry: str) -> tuple[str, str]:
    spec = SPEC.replace("- `docs/runbook.md`\n", f"- `docs/runbook.md`\n- `{entry}`\n")
    return spec, _with_scope(entry)


def test_a_bare_existing_directory_in_a_worker_scope_is_refused() -> None:
    spec, plan = _scoped("tests/gateway")
    errors = check_job_spec(parse_spec(spec), parse_plan(plan), MONEY, is_dir=_is_dir)
    assert any(f"tests/gateway: {BARE_DIR}" in e and "pa-0001-w2" in e for e in errors), errors
    assert any(f"tests/gateway: {BARE_DIR}" in e and "spec.md" in e for e in errors), errors


@pytest.mark.parametrize(
    "entry", ["tests/gateway/**", "tests/gateway/x.py", "tests/new.py", "tests/*.py"]
)
def test_a_dir_glob_file_new_path_or_glob_passes_the_bare_dir_rule(entry: str) -> None:
    spec, plan = _scoped(entry)
    errors = check_job_spec(parse_spec(spec), parse_plan(plan), MONEY, is_dir=_is_dir)
    assert not any(BARE_DIR in e for e in errors), errors
