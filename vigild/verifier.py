"""The verifier: the only source of test results (owner direction, review
rounds 2 and 3).

After a worker exits and its branch is in the vault, vigild runs the task's
`test_cmd` itself, never trusting anything the worker's container wrote:

- **after:** the head commit, checked out fresh from the vault into a
  directory vigild owns, with a new `.git` whose `core.hooksPath` is /dev/null.
- **before** (when `must_fail_first`): the base commit with the worker's new
  and changed test files (paths `TEST_FILE` matches, taken from the head) laid
  over it, so a new test exists but the code it tests doesn't. Deleted test
  files aren't restored; `weakened_tests` rejects those anyway.
- **The base's runner files in every run** (rounds 3 and 4): files that
  decide how tests run (the repo's `runner_config` globs, plus a script
  `test_cmd` runs as its program) are the base's copies in every run, even
  when the approved spec names them. A worker can't make "after" pass by
  rewriting the runner, and acceptance rejects the attempt unless the spec
  names the path.
- **A task whose scope names a runner file** (phase 1 run, finding #2): the
  runner files stay the base's, except the overlapping ones, which are the
  head's; the task's hidden tests run as their own base/head pair and decide.
- **What a result means** (round 3, F3): `judge` maps runner exits that aren't
  test results (pytest 2-5, vitest's "no test files", a signal) to `unknown`.
  pytest and a directly invoked vitest are asked for a JUnit report, written
  to the container's tmpfs and streamed back after the output; when both runs
  have one, must-fail-first means at least one of the worker's new or changed
  tests failed "before" and every one of those passes "after". Without a
  report the exit codes decide and the record says the ids weren't checked.

Each run gets its own checkout and its own container from the repo's dev image
(never the session image): no network, read-only checkout, the agent uid,
output capped. vigild reads the container's output stream itself; nothing is
read back from a path. The checkout is removed after the run.

A report comes from test code the worker wrote, running as the same uid, so it
is evidence of which tests failed, not a defence against a hostile test.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vigild.intake.paths import in_scope
from vigild.redact import redact
from vigild.shellwords import Command, parse
from vigild.workspace import Workspace

VERIFY_SECONDS = 15 * 60
# ParkAgent's full server suite reports 1853 cases in a 376 KB JUnit file
# (measured 2026-10-22); 8 MiB per report is ~20x headroom.
MAX_OUTPUT = 24 << 20  # the output's tail plus the reports, base64
MAX_REPORT = 8 << 20
TAIL = 2000
REPORT_DIR = "/tmp/vigil-report"  # noqa: S108  (the verifier container's own tmpfs)
TEST_FILE = re.compile(
    r"(^|/)(tests?/|__tests__/|test_[^/]*$|[^/]*_test\.[a-z]+$|[^/]*\.(test|spec)\.[a-z]+$)"
)
INTERPRETERS = {"sh", "bash", "dash", "zsh", "python", "python3", "node", "tsx", "deno", "perl"}
PYTEST_NOT_A_RESULT = {2, 3, 4, 5}  # interrupted/collection error, internal, usage, no tests
VITEST_NO_FILES = "No test files found"


@dataclass(frozen=True)
class RunResult:
    exit: int | None
    output: str
    timed_out: bool = False
    truncated: bool = False
    reports: dict[str, bytes] = field(default_factory=dict)


class TestRunner(Protocol):
    def run(
        self, image: str, source: Path, command: str, *, timeout_s: int, max_output: int
    ) -> RunResult: ...


def outcome(code: int | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if code == 0:
        return "pass"
    if code in (125, 126, 127) or (code is not None and code >= 128):
        return "unknown"  # couldn't prepare (125) or run (126/127); killed by a signal
    return "fail"


def runs_pytest(test_cmd: str) -> bool:
    return any(
        "pytest" in (c.program, *(w.rsplit("/", 1)[-1] for w in c.args[:3]))
        for c in parse(test_cmd).commands
    )


def judge(test_cmd: str, code: int | None, timed_out: bool, output: str) -> str:
    """The outcome, with a runner's own non-test exits read as `unknown`."""
    got = outcome(code, timed_out)
    if got != "fail":
        return got
    if runs_pytest(test_cmd) and code in PYTEST_NOT_A_RESULT:
        return "unknown"
    if VITEST_NO_FILES in output:
        return "unknown"
    return got


def _script(words: list[str]) -> str:
    """The repo file a command runs as its program, if any: the program
    itself when it's a relative path, or an interpreter's script argument.
    Never a test runner's arguments (round 4, item 2)."""
    if not words:
        return ""
    prog, rest = words[0], words[1:]
    base = prog.rsplit("/", 1)[-1]
    if "/" in prog and not prog.startswith("/") and base not in INTERPRETERS:
        return prog
    if base in INTERPRETERS:
        for a in rest:
            if a in ("-c", "-m"):
                return ""  # code or a module, not a file
            if not a.startswith("-"):
                return a
        return ""
    if base == "uv" and rest[:1] == ["run"]:
        args = [a for a in rest[1:] if not a.startswith("-")]
        if args and args[0].endswith((".py", ".sh")):
            return args[0]
        return _script(args)  # `uv run python x.py`; `uv run pytest …` names no script
    if base == "make":
        return "Makefile"
    return ""


def invoked_scripts(test_cmd: str) -> list[str]:
    """Repo files `test_cmd` runs as programs: `./x.sh`, `sh run_tests.sh`,
    `python tools/check.py`, `uv run x.py`, a Makefile. Arguments to a test
    runner (`pytest tests/test_a.py`, `uv run pytest tests/test_a.py`) are
    tests, not runner config."""
    out: list[str] = []
    for c in parse(test_cmd).commands:
        script = _script([c.program, *c.args] if c.program else [])
        if not script:
            first = next((w for w in c.words if w.rsplit("/", 1)[-1] == c.program), "")
            if "/" in first and not first.startswith("/"):
                script = first
        if script and not script.startswith("/"):
            out.append(script)
    return sorted({p.removeprefix("./") for p in out})


def runner_globs(runner_config: Iterable[str], test_cmd: str) -> list[str]:
    """What decides how tests run: the repo's `runner_config` globs and the
    scripts `test_cmd` runs as programs. One list for the verifier's pins and
    the spec gate's overlap."""
    return [*runner_config, *invoked_scripts(test_cmd)]


def runner_overlap(scope: Iterable[str], runner_config: Iterable[str], test_cmd: str) -> list[str]:
    """The literal entries in a task's scope that the verifier pins to the base
    (a glob in scope can't change a runner file: acceptance refuses a changed
    runner file the spec doesn't name by its exact path)."""
    globs = runner_globs(runner_config, test_cmd)
    return sorted({f for f in scope if not set("*?[") & set(f) and in_scope(f, globs)})


@dataclass(frozen=True)
class HiddenSuite:
    """A task's hidden tests: files in a vault commit that is on no pushed
    branch (`sha`), under `prefix`, and the command that runs them."""

    sha: str
    prefix: str
    files: tuple[str, ...]
    test_cmd: str


PACKAGE_MANAGERS = {"pnpm", "npm", "yarn"}
ReadFile = Callable[[str], str | None]


class FileNotAvailable(RuntimeError):
    """The base's files can't be read (no mirror, or it couldn't be fetched)."""


@dataclass(frozen=True)
class ReportPlan:
    """How the verifier will run `test_cmd` so each runner writes a report, and
    why it can't, if it can't. The spec gate and the verifier use this one
    resolver (round 4, item 6): the gate refuses whatever has problems."""

    command: str
    reports: int
    problems: list[str]


def _flags(n: int) -> str:
    return f" --reporter=default --reporter=junit --outputFile.junit={REPORT_DIR}/vitest-{n}.xml"


def _runs_pytest(c: Command) -> bool:
    return "pytest" in (c.program, *(a.rsplit("/", 1)[-1] for a in c.args[:3]))


def _runs_vitest(c: Command) -> bool:
    return c.program == "vitest" or ("vitest" in c.args and "run" in c.args)


def plan_reports(test_cmd: str, read: ReadFile | None) -> ReportPlan:
    """Resolve each command: pytest reports through PYTEST_ADDOPTS; a direct
    `vitest run` gets the reporter flags; `pnpm [-C dir] [run] <script> [args]`
    whose script (from the base's package.json, relative to any `cd`) ends in
    vitest runs as `pnpm exec bash -c '<script> <args> <flags>'`, since pnpm
    takes `--reporter` as its own. Anything the verifier can't attach a report
    to is a problem, never a silent drop."""
    problems: list[str] = []
    out, pos, n, reporting = [], 0, 0, 0
    cwd = ""
    for c in parse(test_cmd).commands:
        if c.program == "cd" and c.args:
            cwd = "" if c.args[0] in (".", "/work") else c.args[0].strip("/")
            continue
        if _runs_pytest(c):
            reporting += 1
            continue
        if _runs_vitest(c):
            n += 1
            reporting += 1
            end = c.start + len(c.text.rstrip())
            out.append(test_cmd[pos:end] + _flags(n))
            pos = end
            continue
        if c.program in ("npm", "yarn", "npx") and not _runs_vitest(c):
            problems.append(
                f"`{c.text.strip()}`: {c.program} scripts aren't supported; "
                "use pnpm or call the runner directly"
            )
            continue
        if c.program != "pnpm":
            continue
        args = list(c.args)
        if any(a.startswith("--filter") or a == "-F" or a in ("-r", "--recursive") for a in args):
            problems.append(f"`{c.text.strip()}`: pnpm --filter/-r isn't supported; use -C <dir>")
            continue
        where = cwd
        for flag in ("-C", "--dir"):
            if flag in args and args.index(flag) + 1 < len(args):
                k = args.index(flag)
                where = "/".join(x for x in (cwd, args[k + 1].strip("/")) if x)
                del args[k : k + 2]
        if args[:1] == ["run"]:
            args = args[1:]
        if not args or args[0] in ("exec", "dlx", "install", "add"):
            continue
        name, extra = args[0], args[1:]
        if read is None:
            problems.append(f"`{c.text.strip()}`: the package script can't be read to check it")
            continue
        manifest = f"{where}/package.json" if where else "package.json"
        try:
            raw = read(manifest)
        except FileNotAvailable as e:
            problems.append(f"`{c.text.strip()}`: {e}, so its package script can't be checked")
            continue
        if raw is None:
            problems.append(f"`{c.text.strip()}`: there's no {manifest} on the base")
            continue
        try:
            script = str((json.loads(raw or "{}").get("scripts") or {}).get(name) or "")
        except json.JSONDecodeError, AttributeError:
            script = ""
        last = parse(script).commands[-1] if script.strip() else None
        if last is None or not (_runs_vitest(last) or _runs_pytest(last)):
            problems.append(
                f"`{c.text.strip()}`: the package script `{name}` runs no test runner "
                "vigil can read (vitest or pytest, last in the script)"
            )
            continue
        reporting += 1
        if _runs_pytest(last):
            continue  # PYTEST_ADDOPTS reaches it through pnpm
        n += 1
        body = " ".join([script, *(shlex.quote(a) for a in extra)]) + _flags(n)
        lead = c.text[: len(c.text) - len(c.text.lstrip())]
        dir_opt = f"-C {shlex.quote(where)} " if where and where != cwd else ""
        out.append(
            test_cmd[pos : c.start]
            + lead
            + f"pnpm {dir_opt}exec bash -o pipefail -o errexit -c {shlex.quote(body)}"
        )
        pos = c.start + len(c.text.rstrip())
    out.append(test_cmd[pos:])
    if not reporting and not problems:
        problems.append(
            "runs no test runner vigil can read (pytest, vitest, or a pnpm script that runs one)"
        )
    return ReportPlan("".join(out), reporting, problems)


def can_report(test_cmd: str, read: ReadFile | None = None) -> bool:
    return not plan_reports(test_cmd, read).problems


def with_reports(test_cmd: str, read: ReadFile | None = None) -> str:
    return plan_reports(test_cmd, read).command


@dataclass(frozen=True)
class Case:
    id: str
    file: str
    status: str  # pass | failure | error | skipped | xfail


def parse_junit(data: bytes) -> list[Case]:
    if len(data) > MAX_REPORT or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return []
    try:
        root = ET.fromstring(data)  # noqa: S314 (no DTDs or entities, size-capped)
    except ET.ParseError:
        return []
    cases: list[Case] = []
    for tc in root.iter("testcase"):
        classname = tc.get("classname", "")
        status = "pass"
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                xfail = "xfail" in (el.get("type", "") + el.get("message", "")).lower()
                status = "xfail" if tag == "skipped" and xfail else tag
                break
        cases.append(
            Case(f"{classname}::{tc.get('name', '')}", tc.get("file") or classname, status)
        )
    return cases


def belongs(case: Case, files: Iterable[str]) -> bool:
    """Is a report's test case in one of these repo files? pytest names a
    module (`tests.test_a`); vitest a path relative to its project dir."""
    parts = case.file.split(".")
    # pytest: a module, or module.Class(.Nested): try each prefix as a .py path
    guesses = {case.file} | {"/".join(parts[:k]) + ".py" for k in range(1, len(parts) + 1)}
    for f in files:
        for g in guesses:
            if g and (f == g or f.endswith("/" + g)):
                return True
    return False


# What a test.each title interpolates that changes on every run, and nothing else:
# a PEM block, a uuid, a long hex run, a long base64-looking token (mixed case with
# a digit, or with `+`/`/` and a digit, or ending in `=` with a digit; never a plain
# `[a-z0-9_]` name), a number of 10 or more digits (a timestamp). A pytest id's
# `[...]` is cut off whole. A random value that is none of these is not masked; the
# two base runs then show it under different titles, and `compare_runs` requires
# head to have as many leftover new ids in that file (fails closed on a deletion).
_B64 = r"[A-Za-z0-9+/_-]"
_PARAMS = re.compile(
    r"-----BEGIN [A-Z ]+-----.*?(?:-----END [A-Z ]+-----|\Z)"
    r"|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-fA-F]{16,}\b"
    rf"|(?<![A-Za-z0-9+/_=-])(?={_B64}*[A-Z])(?={_B64}*[a-z])(?={_B64}*\d){_B64}{{24,}}={{0,2}}"
    rf"|(?<![A-Za-z0-9+/_=-])(?={_B64}*[+/])(?={_B64}*\d){_B64}{{24,}}={{0,2}}"
    rf"|(?<![A-Za-z0-9+/_=-])(?={_B64}*\d){_B64}{{24,}}={{1,2}}"
    r"|\b\d{10,}\b",
    re.S,
)


def normalise_title(name: str) -> str:
    """A test's title without its parameters, so the same test matches itself
    across runs whose generated values differ."""
    if name.endswith("]") and "[" in name:
        name = name[: name.index("[")]
    return " ".join(_PARAMS.sub("<>", name).split())


def _title_counts(cases: Iterable[Case]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for c in cases:
        name = c.id.split("::", 1)[1] if "::" in c.id else c.id
        key = (c.file, normalise_title(name))
        out[key] = out.get(key, 0) + 1
    return out


def compare_runs(
    base: list[Case], head: list[Case], base2: list[Case] | None = None
) -> dict[str, Any]:
    """What ran at head against what ran at base, with the same command and the
    same runner config (owner direction, round 3): head may add tests; it may
    never run fewer ids or files, or skip or xfail more.

    `base2` is a second run of the base. A test id in one base run and not the
    other is *unstable* (its title carries a value generated per run). Stable ids
    are matched exactly. Unstable ones are matched by (file, title without its
    parameters) and by count: head needs at least as many of each as both base
    runs had (the lower of the two counts), counting only head ids that are not
    stable base ids. A drop in count fails."""
    if not base2:  # no second base run, or one with no report: exact ids only
        base2 = None
    unstable = ({c.id for c in base} ^ {c.id for c in base2}) if base2 is not None else set()
    ids_b = {c.id for c in base} - unstable
    ids_h = {c.id for c in head}
    files_b, files_h = {c.file for c in base}, {c.file for c in head}

    def count(cases: list[Case], status: str) -> int:
        return sum(1 for c in cases if c.status == status)

    missing = sorted(ids_b - ids_h)
    rows: list[dict[str, Any]] = []
    if unstable and base2 is not None:
        in_a = _title_counts(c for c in base if c.id in unstable)
        in_b = _title_counts(c for c in base2 if c.id in unstable)
        at_head = _title_counts(c for c in head if c.id not in ids_b)
        used: dict[str, int] = {}  # head ids spent on matched titles, per file
        loose_a: dict[str, int] = {}  # unstable ids whose title didn't match across runs
        loose_b: dict[str, int] = {}
        for key in sorted(set(in_a) | set(in_b)):
            na, nb = in_a.get(key, 0), in_b.get(key, 0)
            need = min(na, nb)
            loose_a[key[0]] = loose_a.get(key[0], 0) + na - need
            loose_b[key[0]] = loose_b.get(key[0], 0) + nb - need
            if not need:
                continue
            got = at_head.get(key, 0)
            used[key[0]] = used.get(key[0], 0) + min(got, need)
            rows.append({"file": key[0], "title": key[1], "base": need, "head": got})
            if got < need:
                missing.append(f"{key[0]}::{key[1]}: {need} at base, {got} at head")
        spare: dict[str, int] = {}
        for (f, _), n in at_head.items():
            spare[f] = spare.get(f, 0) + n
        for f in sorted(set(loose_a) | set(loose_b)):
            need = min(loose_a.get(f, 0), loose_b.get(f, 0))
            left = spare.get(f, 0) - used.get(f, 0)
            if need:
                rows.append({"file": f, "title": "<unmatched>", "base": need, "head": left})
            if left < need:
                missing.append(f"{f}: {need} unmatched unstable ids at base, {left} new at head")
    out: dict[str, Any] = {
        "checked": True,
        "ids": {"base": len({c.id for c in base}), "head": len(ids_h)},
        "missing_ids": missing[:50],
        "missing_files": sorted(files_b - files_h)[:50],
        "skipped": {"base": count(base, "skipped"), "head": count(head, "skipped")},
        "xfail": {"base": count(base, "xfail"), "head": count(head, "xfail")},
        "unstable": rows[:50],
    }
    out["ok"] = not (
        out["missing_ids"]
        or out["missing_files"]
        or out["skipped"]["head"] > out["skipped"]["base"]
        or out["xfail"]["head"] > out["xfail"]["base"]
    )
    return out


class Verifier:
    def __init__(
        self, workspace: Workspace, runner: TestRunner, timeout_s: int = VERIFY_SECONDS
    ) -> None:
        self.ws = workspace
        self.runner = runner
        self.timeout_s = timeout_s

    def verify(
        self,
        *,
        repo: str,
        image: str,
        task_id: str,
        base_sha: str,
        head: str,
        test_cmd: str,
        must_fail_first: bool,
        runner_config: Iterable[str] = (),
        runner_overlap: Sequence[str] = (),
        hidden: HiddenSuite | None = None,
    ) -> dict[str, Any]:
        """`test_cmd` is the approved spec's, never the worker's tree's.

        `runner_overlap` is the runner files the task's scope names (owner
        ruling, 2026-10-23): those, and only those, are the head's in every
        run; `hidden` is the task's hidden tests, run on their own."""
        base_files = self.ws.tree_paths(repo, base_sha)
        head_files = self.ws.tree_paths(repo, head)
        cfg = list(runner_config)
        overlap = sorted(set(runner_overlap))
        pinned = self._pinned(base_files, head_files, cfg, test_cmd, overlap)
        changed = self.ws.changed(repo, base_sha, head)
        changed_paths = {path for _, path in changed}
        tests = sorted(
            path
            for status, path in changed
            if status[:1] in ("A", "M") and TEST_FILE.search(path) and path not in pinned
        )
        record: dict[str, Any] = {
            "test_cmd": test_cmd,
            "runner_config": {"pinned_to_base": [p for p in pinned if p in changed_paths]},
            "runner_files": overlap,
            "runner_file_changed": bool(set(overlap) & changed_paths),
        }
        command = with_reports(test_cmd, lambda path: self.ws.read_blob(repo, base_sha, path))
        pin = (base_sha, pinned)
        base = self._run(repo, image, f"{task_id}-base", base_sha, test_cmd, command, [pin])
        record["base"] = base
        base2: dict[str, Any] | None = None
        if base["_report"]:
            # A second base run finds ids that change per run (phase 1 run, finding
            # #1). It costs one more run the length of the base's, and only when the
            # base reported.
            base2 = self._run(repo, image, f"{task_id}-base2", base_sha, test_cmd, command, [pin])
            if base2["_report"] and base2["_cases"]:
                record["base_repeat"] = {
                    k: base2[k] for k in ("sha", "exit", "outcome", "tests") if k in base2
                }
            else:
                record["base_repeat"] = "unavailable"  # exact ids only
        before: dict[str, Any] | None = None
        if must_fail_first:
            before = self._run(
                repo, image, f"{task_id}-before", base_sha, test_cmd, command,
                [(head, tests), pin],
            )  # fmt: skip
            before["applied"] = tests
            record["before"] = before
        after = self._run(repo, image, f"{task_id}-after", head, test_cmd, command, [pin])
        if not after["_report"] and after["outcome"] == "pass":
            # owner direction, round 3: "tests ran" can't be confirmed without a report
            after["outcome"] = "unknown"
            after["note"] = "no test report: that any test ran can't be confirmed"
        record["after"] = after
        added = sorted(path for status, path in changed if status[:1] == "A")
        if not after["_report"]:
            record["ran"] = {"checked": False, "note": "no report from the head run"}
        elif base["_report"]:
            record["ran"] = {
                **compare_runs(base["_cases"], after["_cases"], base2["_cases"] if base2 else None),
                "against": "base",
            }
        elif before is not None and before["_report"]:
            # round 4, item 4: base ran no runner (e.g. a check before it failed);
            # compare with "before" minus the tests the worker added
            kept = [c for c in before["_cases"] if not belongs(c, added)]
            record["ran"] = {**compare_runs(kept, after["_cases"]), "against": "before"}
        else:
            record["ran"] = {
                "checked": False,
                "unknown": True,
                "note": "neither base nor before produced a report: what ran can't be compared",
            }
        if before is not None:
            record["ids"] = self._ids(before, after, tests)
        if hidden is not None:
            record["hidden"] = self._hidden_pair(
                repo, image, task_id, base_sha, head, hidden, cfg, overlap,
                base_files, head_files,
            )  # fmt: skip
        record["provenance"] = "human"  # observed by vigild, not reported by the model
        record["source"] = "verifier"
        for run in (base, base2, before, after):
            if run is not None:
                run.pop("_cases", None)
                run.pop("_report", None)
        return record

    @staticmethod
    def _pinned(
        base_files: set[str],
        head_files: set[str],
        runner_config: list[str],
        test_cmd: str,
        overlap: Sequence[str],
    ) -> list[str]:
        """The runner files that are the base's in every run (owner direction,
        round 3): the repo's runner_config, and any script `test_cmd` runs that
        the base has. Not the overlap, which is the head's."""
        pinned = {p for p in base_files | head_files if in_scope(p, runner_config)} | {
            p for p in invoked_scripts(test_cmd) if p in base_files
        }
        return sorted(pinned - set(overlap))

    def _hidden_run(
        self,
        repo: str,
        image: str,
        name: str,
        sha: str,
        base_sha: str,
        hidden: HiddenSuite,
        pinned: list[str],
    ) -> dict[str, Any]:
        command = with_reports(
            hidden.test_cmd, lambda path: self.ws.read_blob(repo, base_sha, path)
        )
        take = [(base_sha, pinned), (hidden.sha, list(hidden.files))]
        run = self._run(repo, image, name, sha, hidden.test_cmd, command, take)
        if not run["_report"] and run["outcome"] == "pass":
            run["outcome"] = "unknown"
            run["note"] = "no test report: that any hidden test ran can't be confirmed"
        return run

    @staticmethod
    def base_failure(run: dict[str, Any]) -> dict[str, Any]:
        """How a hidden run failed on the base. Every hidden test must fail an
        assertion: a JUnit `<failure>`. An import, collection or setup error
        (`<error>`), a pass, a skip, a timeout or a missing report is the wrong
        way, and doesn't count (owner, 2026-10-23)."""
        cases = run["_cases"] if run["_report"] else []
        n = {k: sum(1 for c in cases if c.status == k) for k in ("failure", "error", "pass")}
        wrong = len(cases) - n["failure"]
        out: dict[str, Any] = {
            "outcome": run["outcome"], "tests": len(cases), "failing": n["failure"],
            "errors": n["error"], "passing": n["pass"],
        }  # fmt: skip
        out["ok"] = run["outcome"] == "fail" and bool(cases) and wrong == 0
        if not out["ok"]:
            if run["outcome"] == "pass" or (n["pass"] and not n["error"]):
                out["why"] = "the hidden tests pass on the base; they must fail there"
            elif n["error"]:
                out["why"] = (
                    f"{n['error']} hidden test(s) errored (import, collection or setup) on the "
                    "base instead of failing an assertion"
                )
            elif run["outcome"] == "timeout":
                out["why"] = "the hidden tests timed out on the base"
            else:
                out["why"] = (
                    f"the hidden tests must each fail an assertion on the base, with a report; "
                    f"the run was {run['outcome']} with {len(cases)} test(s) in its report"
                )
        return out

    def check_hidden_on_base(
        self,
        *,
        repo: str,
        image: str,
        task_id: str,
        base_sha: str,
        hidden: HiddenSuite,
        runner_config: Iterable[str] = (),
        overlap: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Spec time: hidden tests must fail on the base, in their own run,
        each with an assertion failure."""
        base_files = self.ws.tree_paths(repo, base_sha)
        pinned = self._pinned(base_files, set(), list(runner_config), hidden.test_cmd, overlap)
        pinned = [p for p in pinned if p not in hidden.files]
        run = self._hidden_run(repo, image, f"{task_id}-hspec", base_sha, base_sha, hidden, pinned)
        return self.base_failure(run)

    def _hidden_pair(
        self,
        repo: str,
        image: str,
        task_id: str,
        base_sha: str,
        head: str,
        hidden: HiddenSuite,
        runner_config: list[str],
        overlap: Sequence[str],
        base_files: set[str],
        head_files: set[str],
    ) -> dict[str, Any]:
        """The hidden tests as their own run, base against head (learning
        ruling 14). They decide a runner-file task: they must fail on the base
        and pass on the head, with a report, and head may run no fewer."""
        out: dict[str, Any] = {"test_cmd": hidden.test_cmd, "files": len(hidden.files)}
        planted = sorted(p for p in head_files if p.startswith(hidden.prefix.rstrip("/") + "/"))
        if planted:
            # a worker can't put files where the hidden tests run (a conftest.py there)
            out.update(ok=False, tampered=planted[:20], failed_on_base=False, passed_on_head=False)
            return out
        pinned = [
            p
            for p in self._pinned(base_files, head_files, runner_config, hidden.test_cmd, overlap)
            if p not in hidden.files
        ]
        b = self._hidden_run(repo, image, f"{task_id}-hbase", base_sha, base_sha, hidden, pinned)
        h = self._hidden_run(repo, image, f"{task_id}-hhead", head, base_sha, hidden, pinned)
        failed_on_base = self.base_failure(b)["ok"]
        passed_on_head = h["outcome"] == "pass" and h["_report"] and h["tests"] > 0
        ran: dict[str, Any] = {"checked": False}
        if b["_report"] and h["_report"]:
            ran = {**compare_runs(b["_cases"], h["_cases"]), "against": "base"}
        out.update(
            base=b,
            head=h,
            failed_on_base=failed_on_base,
            base_failure={k: v for k, v in self.base_failure(b).items() if k != "ok"},
            passed_on_head=bool(passed_on_head),
            ran=ran,
            ok=bool(failed_on_base and passed_on_head and (not ran.get("checked") or ran["ok"])),
        )
        for run in (b, h):
            run.pop("_cases", None)
            run.pop("_report", None)
        return out

    @staticmethod
    def _ids(before: dict[str, Any], after: dict[str, Any], tests: list[str]) -> dict[str, Any]:
        b, a = before.get("_cases"), after.get("_cases")
        if not before.get("_report") or not after.get("_report"):
            return {"checked": False, "note": "test ids not checked: the runner gave no report"}
        failing = sorted(
            c.id for c in b or [] if c.status in ("failure", "error") and belongs(c, tests)
        )
        passing = {c.id for c in a or [] if c.status == "pass"}
        return {
            "checked": True,
            "failing_before": failing,
            "not_passing_after": sorted(i for i in failing if i not in passing),
        }

    def _run(
        self,
        repo: str,
        image: str,
        name: str,
        sha: str,
        test_cmd: str,
        command: str,
        take: list[tuple[str, list[str]]],
    ) -> dict[str, Any]:
        src = self.ws.verify_checkout(repo, name, sha, take=take)
        try:
            r = self.runner.run(
                image, src, command, timeout_s=self.timeout_s, max_output=MAX_OUTPUT
            )
        finally:
            shutil.rmtree(src.parent, ignore_errors=True)
        cases = [c for data in r.reports.values() for c in parse_junit(data)]
        got = judge(test_cmd, r.exit, r.timed_out, r.output)
        out: dict[str, Any] = {
            "sha": sha,
            "exit": r.exit,
            "outcome": got,
            "tail": redact(r.output[-TAIL:]),
            "tests": len(cases),
            "_cases": cases,
            "_report": bool(r.reports),
        }
        if r.reports and not cases and got == "pass":
            out["outcome"] = "unknown"  # exit 0 with no test run is not a pass (round 3)
            out["note"] = "the report lists no tests"
        failing = [c.id for c in cases if c.status in ("failure", "error")]
        if failing and got == "pass":
            # round 4, item 1: `! pytest`, `if pytest; …`, `set +e`, a trap or
            # `&& …; echo` can exit 0 while tests fail; the report decides
            out["outcome"] = "fail"
            out["note"] = f"exit 0, but the report has {len(failing)} failing test(s)"
        if r.truncated:
            out["truncated"] = True
        return out
