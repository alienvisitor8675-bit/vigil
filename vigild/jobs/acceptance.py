"""Stage 3 acceptance (design "Job execution protocol"), checked by vigild on
the host, in the vault, after the worker container exits and before anything
is pushed: must_fail_first observed, test_cmd passes, files changed inside
scope, no test weakened, cost under the limit. Lint and types run in CI.

Test results come only from vigild's verifier (owner direction, review round
2; `vigild/verifier.py`): the worker report's test fields are ignored. It
fails closed: a task with no test_cmd, a missing verifier record, or any
outcome other than `fail` before and `pass` after is a rejection."""

from __future__ import annotations

import re
from typing import Any

from vigild.contract.models import WorkerReport
from vigild.github.publish import artifact_paths, out_of_scope
from vigild.intake.paths import in_scope
from vigild.verifier import TEST_FILE, invoked_scripts
from vigild.workspace import Workspace

ASSERT = re.compile(r"\b(assert|expect|should|XCTAssert)")


def weakened_tests(ws: Workspace, repo: str, base_sha: str, head: str) -> list[str]:
    """Test files deleted (renames count as deletes), or with fewer assertions."""
    out: list[str] = []
    for status, path in ws.changed(repo, base_sha, head):
        if not TEST_FILE.search(path):
            continue
        if status.startswith("D"):
            out.append(f"{path} was deleted")
            continue
        diff = ws.diff(repo, base_sha, head, path)
        removed = sum(1 for d in diff.splitlines() if d.startswith("-") and ASSERT.search(d))
        added = sum(1 for d in diff.splitlines() if d.startswith("+") and ASSERT.search(d))
        if removed > added:
            out.append(f"{path} lost {removed - added} assertion(s)")
    return out


NO_HIDDEN = "runner file task: no hidden tests, so it cannot pass"


def escalates(tests: dict[str, Any] | None) -> bool:
    """A runner-file task whose hidden tests don't exist: it is built and
    reported, never accepted; the owner decides (owner ruling, 2026-10-23)."""
    if not tests or not tests.get("runner_files"):
        return False
    return not (tests.get("hidden") or {}).get("files")


def hidden_errors(tests: dict[str, Any]) -> list[str]:
    """For a task whose scope names a runner file, the hidden tests decide:
    they failed on the base, pass on the head, head ran no fewer. The messages
    carry counts and reasons, never a test's name or output: they reach the
    owner's page and a re-plan's context."""
    if not tests.get("runner_files"):
        return []
    hidden = tests.get("hidden") or {}
    if escalates(tests):
        return [f"{NO_HIDDEN}: {hidden.get('reason') or 'none were written'}"]
    errors: list[str] = []
    if hidden.get("tampered"):
        errors.append("the branch has files in the hidden directory; hidden tests didn't run")
    elif not hidden.get("failed_on_base"):
        errors.append("hidden tests must fail on the base; they didn't")
    if not hidden.get("tampered") and not hidden.get("passed_on_head"):
        errors.append("hidden tests must pass on the head; they didn't")
    ran = hidden.get("ran") or {}
    if ran.get("checked") and not ran.get("ok"):
        errors.append("head ran less than base in the hidden tests' own run")
    return errors


def check_tests(spec: dict[str, Any], tests: dict[str, Any] | None) -> list[str]:
    if not str(spec.get("test_cmd") or "").strip():
        return ["the contract has no test_cmd, so nothing can be accepted"]
    if not tests or tests.get("source") != "verifier":
        return [
            "the verifier's record is missing: test_cmd was not observed, so nothing is accepted"
        ]
    errors: list[str] = []
    before = (tests.get("before") or {}).get("outcome")
    after = (tests.get("after") or {}).get("outcome")
    if spec.get("must_fail_first", True) and before != "fail":
        errors.append(f"must_fail_first: the verifier saw the test {before or 'not run'} before")
    if after != "pass":
        errors.append(f"test_cmd must pass; the verifier saw it {after or 'not run'} after")
    ids = tests.get("ids") or {}
    if spec.get("must_fail_first", True) and ids.get("checked"):
        # round 3, F3: the failure "before" must be the worker's own test failing
        if not ids.get("failing_before"):
            errors.append(
                "must_fail_first: none of the worker's new or changed tests failed before; "
                "the failure was elsewhere in test_cmd"
            )
        if ids.get("not_passing_after"):
            still = ids["not_passing_after"]
            errors.append(f"the worker's tests that failed before don't pass after: {still}")
    ran = tests.get("ran") or {}
    if ran.get("unknown"):
        errors.append(f"what ran can't be compared: {ran.get('note')}")
    if ran.get("checked") and not ran.get("ok"):
        # owner direction, round 3: head may add tests, never run fewer or skip more
        errors.append(
            "head ran less than base: "
            f"missing ids {ran.get('missing_ids')}, missing files {ran.get('missing_files')}, "
            f"skipped {ran.get('skipped')}, xfail {ran.get('xfail')}"
        )
    return [*errors, *hidden_errors(tests)]


def runner_config_changes(
    ws: Workspace,
    repo: str,
    base_sha: str,
    head: str,
    contract: dict[str, Any],
    runner_config: list[str] | tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """(refused, allowed): changed files that decide how tests run (round 3,
    F1). Allowed only when the spec names the exact path, not a glob."""
    spec = contract["spec"]
    globs = [*runner_config, *invoked_scripts(str(spec.get("test_cmd") or ""))]
    named = set(spec.get("files_in_scope") or [])
    changed = sorted({path for _, path in ws.changed(repo, base_sha, head)})
    hits = [p for p in changed if in_scope(p, globs)]
    return [p for p in hits if p not in named], [p for p in hits if p in named]


def check_worker(
    ws: Workspace,
    repo: str,
    base_sha: str,
    head: str,
    contract: dict[str, Any],
    report: WorkerReport,
    tests: dict[str, Any] | None,
    *,
    cost: float,
    runner_config: list[str] | tuple[str, ...] = (),
    session_base: str | None = None,
) -> list[str]:
    spec = contract["spec"]
    errors: list[str] = []
    vendored = sorted(
        p for _, p in ws.changed(repo, base_sha, head) if "node_modules" in p.split("/")
    )
    if vendored:
        # round 4, item 5: installed packages are the image's, never the worker's
        errors.append(f"node_modules is never committed: {vendored[:20]}")
    refused, _ = runner_config_changes(ws, repo, base_sha, head, contract, runner_config)
    if refused:
        errors.append(
            f"changes how tests run: {refused}; the approved spec must name each of these "
            "by its exact path in files in scope"
        )
    if report.task_id != contract["task_id"]:
        errors.append(f"report task_id {report.task_id} is not {contract['task_id']}")
    errors += check_tests(spec, tests)
    if session_base and not ws.is_ancestor(repo, session_base, head):
        # the branch was rewritten (reset, amend, rebase): what vigild committed is gone
        errors.append(
            "vigild's pre-session commit is missing from the branch (history was rewritten); "
            "the files can't be checked against scope"
        )
        return errors
    changed = [path for _, path in ws.changed(repo, session_base or base_sha, head)]
    own = artifact_paths(str(contract.get("job_id") or ""), str(contract.get("task_id") or ""))
    outside = out_of_scope(changed, [*(spec.get("files_in_scope") or []), *own])
    if outside:
        errors.append(f"files changed outside scope: {outside}")
    for w in weakened_tests(ws, repo, base_sha, head):
        errors.append(f"test weakened: {w}")
    limit = float((contract.get("limits") or {}).get("max_cost_usd") or 0)
    if limit and cost > limit + 1e-9:
        errors.append(f"cost ${cost:.4f} is over the limit ${limit:.2f}")
    return errors
