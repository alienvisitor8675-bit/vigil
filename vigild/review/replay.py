"""Review replay (#47): the pieces of a replay that are not the session itself.

A replay runs one reviewer session on a past job's task head with a chosen
reviewer prompt. The session path is the runner's (`JobRunner.replay_review`), so
this module holds only:

- `resolve_prompt`: a prompt version is a git ref of this repo or the sha256 of
  `prompts/reviewer.md` at some commit of it, read through `vigild.gitsafe`;
- the regression set: `Case` files in `tests/fixtures/review_cases/`, the matcher
  `finding_matches`, and `run_set`, which reports recall and total cost.

Cases hold no code and no diff text (the diff comes from the vault at run time).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigild.gitsafe import GitError, git, git_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "tests" / "fixtures" / "review_cases"
PROMPT_PATH = "prompts/reviewer.md"
SHIPPED = "refs/remotes/origin/main"  # "shipped" means reachable from here, nothing else
HASH = re.compile(r"^[0-9a-f]{64}$")
# A ref name as git writes it, nothing more: no leading `-` (never a git option), no
# `:` (never `rev:path`), no `@{`, whitespace or glob characters.
REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/~^+-]{0,199}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_HISTORY = 500  # commits searched for a content hash
MAX_PROMPT = 64 * 1024


class ReplayError(Exception):
    """Base of what a replay refuses; `status` is the HTTP status the API gives."""

    status = 400


class ReplayInvalid(ReplayError):
    status = 422


class ReplayNotFound(ReplayError):
    status = 404


class ReplayRefused(ReplayError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True)
class ResolvedPrompt:
    text: str
    sha256: str
    ref: str  # what the caller named
    commit: str


def _show(root: Path, commit: str) -> bytes | None:
    try:
        return git_bytes(root, "show", f"{commit}:{PROMPT_PATH}", timeout=30)
    except GitError:
        return None


def resolve_prompt(version: str, root: Path | None = None) -> ResolvedPrompt:
    """The reviewer prompt a version names: a git ref of this repo or the sha256 of
    `prompts/reviewer.md` at some commit, through the one hardened git. Only a
    **shipped** prompt counts: the commit must be reachable from `origin/main`. A
    feature branch, a stash or any other unmerged ref is refused. Anything else is
    refused too."""
    root = root or REPO_ROOT
    version = version.strip()
    if HASH.match(version):
        return _by_hash(root, version)
    if not REF.match(version):
        raise ReplayInvalid(f"{version!r} is not a git ref or a prompt hash")
    try:
        commit = git(root, "rev-parse", "--verify", "--quiet", f"{version}^{{commit}}", timeout=30)
    except GitError as e:
        raise ReplayInvalid(f"{version!r} is not a ref of this repo") from e
    _require_shipped(root, commit, version)
    return _load(root, commit, version)


def _require_shipped(root: Path, commit: str, ref: str) -> None:
    try:
        git(root, "rev-parse", "--verify", "--quiet", f"{SHIPPED}^{{commit}}", timeout=30)
    except GitError as e:
        raise ReplayInvalid("origin/main is not available here, so nothing has shipped") from e
    try:
        git(root, "merge-base", "--is-ancestor", commit, SHIPPED, timeout=30)
    except GitError as e:
        raise ReplayInvalid(f"{ref!r} is not on origin/main (not shipped)") from e


def _load(root: Path, commit: str, ref: str) -> ResolvedPrompt:
    raw = _show(root, commit)
    if raw is None or len(raw) > MAX_PROMPT:
        raise ReplayInvalid(f"{ref!r} has no {PROMPT_PATH}")
    try:
        text = raw.decode()
    except UnicodeDecodeError as e:
        raise ReplayInvalid(f"{PROMPT_PATH} at {ref!r} is not text") from e
    return ResolvedPrompt(text, hashlib.sha256(raw).hexdigest(), ref, commit)


def _by_hash(root: Path, digest: str) -> ResolvedPrompt:
    try:
        log = git(
            root, "log", SHIPPED, f"--max-count={MAX_HISTORY}", "--format=%H", "--", PROMPT_PATH,
            timeout=60,
        )  # fmt: skip
    except GitError as e:
        raise ReplayInvalid("this repo's history can't be read") from e
    for commit in log.split():
        raw = _show(root, commit)
        if raw is not None and hashlib.sha256(raw).hexdigest() == digest:
            return _load(root, commit, digest)
    raise ReplayInvalid(f"no shipped {PROMPT_PATH} has the hash {digest}")


# --- the regression set -----------------------------------------------------------------


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ac: str | None = Field(pattern=r"^AC\d{1,3}$")  # null: a finding that is not an AC
    cls: str = Field(alias="class", min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=200)
    # Where the miss was: pointers only, never code. The matcher needs both.
    file: str = Field(min_length=1, max_length=300)
    line: int = Field(ge=1)


class Case(BaseModel):
    """One past review's expected findings. No code and no diff: the job, the task
    and the head name them, and the vault serves the diff when the case runs."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    job_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    head: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected: list[Expected] = Field(min_length=1, max_length=20)


def load_cases(directory: Path | None = None) -> list[Case]:
    """Every `*.yaml`, `*.yml` or `*.json` case in the directory, by file name."""
    folder = directory or CASES_DIR
    cases: list[Case] = []
    for f in sorted(folder.iterdir()):
        if f.suffix not in (".yaml", ".yml", ".json"):
            continue
        try:
            cases.append(Case.model_validate(yaml.safe_load(f.read_text())))  # JSON is YAML
        except (ValidationError, yaml.YAMLError) as e:
            raise ValueError(f"{f.name}: {str(e)[:300]}") from e
    return cases


# What a class of finding sounds like. A finding's claim fits a class when it contains
# any of these (lower case). A class not listed here is matched on the words of its
# own name, so a new case needs no code change for an obvious class.
CLASS_TERMS: dict[str, tuple[str, ...]] = {
    "count-out-of-bound": (
        "bound", "range", "too many", "too few", "more than", "fewer than", "exceed",
        "at most", "at least", "outside", "count", "number of", "allow", "limit", "between",
    ),
    "missing-assertion": ("assert", "check", "verif", "expect", "never test", "not tested"),
    "ac-semantics-mismatch": (
        "whole word", "contain", "mismatch", "spec says", "requires", "stricter", "looser",
        "differs", "instead of", "does not match", "doesn't match",
    ),
    "stale-docs": (
        "comment", "header", "usage", "stale", "outdated", "docs", "documentation",
        "still describes", "only the old",
    ),
    "missing-case": (
        "missing", "no ", "not covered", "uncovered", "untested",
        "lacks", "absent", "without", "does not include", "doesn't include", "never",
    ),
}  # fmt: skip


def _terms(cls: str) -> tuple[str, ...]:
    return CLASS_TERMS.get(cls) or tuple(w for w in re.split(r"[-_\s]+", cls.lower()) if w)


LINE_SLACK = 15  # a finding's line may be this far from the expected line


def _ac_named(claim: str, ac: str) -> bool:
    number = ac[2:].lstrip("0") or "0"
    return bool(re.search(rf"(?<![A-Za-z0-9])AC[\s_-]?0*{re.escape(number)}(?!\d)", claim, re.I))


def _norm(path: str) -> str:
    return path.strip().removeprefix("./").lstrip("/")


def finding_matches(finding: dict[str, Any], expected: Expected) -> bool:
    """A reviewer finding matches an expected one only when both hold:

    (a) it names the AC id (`AC7`, `ac 7` or `AC-7`, as a whole token, so `AC70` and
        `BAC7` do not count); for an `ac: null` case it names the expected file instead
        (its `file` field is that file, or its claim writes the path);
    (b) its `file` is the case's expected file and its `line` is within +/-15 of the
        expected line (a finding with no line does not match).

    The class is not tested here: a generic claim must not match on a word. `class_fits`
    only breaks a tie between two findings that both match."""
    claim = str(finding.get("claim", ""))
    want = _norm(expected.file)
    got = _norm(str(finding.get("file", "")))
    if got != want:
        return False
    line = finding.get("line")
    if (
        not isinstance(line, int)
        or isinstance(line, bool)
        or abs(line - expected.line) > LINE_SLACK
    ):
        return False
    if expected.ac is not None:
        return _ac_named(claim, expected.ac)
    return got == want or want.lower() in claim.lower()


def class_fits(finding: dict[str, Any], expected: Expected) -> bool:
    """Whether the claim reads like the expected class; a tiebreak, never a match."""
    low = str(finding.get("claim", "")).lower()
    return any(t in low for t in _terms(expected.cls))


AC_TOKEN = re.compile(r"\bAC\s*\d{1,3}\b", re.IGNORECASE)


def _assign(findings: list[dict[str, Any]], expected: list[Expected]) -> list[int | None]:
    """One-to-one: AC cases claim findings first, then `ac: null` cases, and a null
    case never takes a finding that names an AC id (owner, #49 round 2). Returns, per
    expected finding in its listed order, the index of the finding it took."""
    used: set[int] = set()
    hits: list[int | None] = [None] * len(expected)
    order = [i for i, e in enumerate(expected) if e.ac is not None] + [
        i for i, e in enumerate(expected) if e.ac is None
    ]
    for k in order:
        exp = expected[k]
        cands = [
            i for i, f in enumerate(findings)
            if i not in used and finding_matches(f, exp)
            and (exp.ac is not None or not AC_TOKEN.search(str(f.get("claim") or "")))
        ]  # fmt: skip
        hit = next((i for i in cands if class_fits(findings[i], exp)), cands[0] if cands else None)
        if hit is not None:
            used.add(hit)
        hits[k] = hit
    return hits


def run_set(replay: Callable[[Case], dict[str, Any]], cases: list[Case]) -> dict[str, Any]:
    """Replay every case with `replay` and report recall (expected findings found /
    expected findings) and the total cost. Matching is one-to-one (`_assign`). A case
    whose replay didn't finish `done` (refused at a breaker that wasn't closed, failed,
    or an error such as a head the vault can't serve) is listed under `not_scored`
    with its status, counts in neither side
    of recall, and makes the run `incomplete`: no recall figure is reported then
    (owner, #49). Its cost still counts. A refusal at the start (an open breaker, a
    full experiment line, the private-repo rule) stops the whole set."""
    report: list[dict[str, Any]] = []
    not_scored: list[dict[str, str]] = []
    expected_n = found_n = 0
    tally: dict[str, list[int]] = {}  # group -> [expected, found]

    def count(group: str, expected: int, found: int) -> None:
        t = tally.setdefault(group, [0, 0])
        t[0] += expected
        t[1] += found

    cost = 0.0
    for case in cases:
        entry: dict[str, Any] = {"case": case.id, "head": case.head}
        try:
            out = replay(case)
        except ReplayRefused:
            raise
        except ReplayError as e:
            out = {"status": "error", "findings": [], "cost_usd": 0.0, "error": str(e)}
        cost += float(out.get("cost_usd") or 0.0)
        entry.update(
            replay_id=out.get("id"),
            status=out.get("status"),
            verdict=out.get("verdict"),
            cost_usd=round(float(out.get("cost_usd") or 0.0), 6),
        )
        if out.get("error"):
            entry["error"] = str(out["error"])
        if out.get("status") != "done":  # refused, failed or error: not scored
            not_scored.append({"case": case.id, "status": str(out.get("status"))})
            report.append(entry)
            continue
        findings = list(out.get("findings") or [])
        hits = _assign(findings, list(case.expected))
        rows = [
            {"ac": e.ac, "class": e.cls, "found": h is not None}
            for e, h in zip(case.expected, hits, strict=True)
        ]
        found = sum(1 for r in rows if r["found"])
        entry.update(findings=len(findings), expected=rows)
        report.append(entry)
        expected_n += len(rows)
        found_n += found
        count(case.job_id, len(rows), found)
        ac_rows = [(e, r) for e, r in zip(case.expected, rows, strict=True) if e.ac is not None]
        count("ac_only", len(ac_rows), sum(1 for _, r in ac_rows if r["found"]))
    count("all", expected_n, found_n)
    incomplete = bool(not_scored)
    return {
        "incomplete": incomplete,
        "not_scored": not_scored,
        "recalls": {
            g: {
                "expected": e,
                "found": f,
                "recall": None if incomplete else (round(f / e, 4) if e else 0.0),
            }
            for g, (e, f) in tally.items()
        },
        "expected": expected_n,
        "found": found_n,
        "recall": None if incomplete else (round(found_n / expected_n, 4) if expected_n else 0.0),
        "cost_usd": round(cost, 6),
        "cases": report,
    }


def render_report(report: dict[str, Any]) -> str:
    """A set's report as the CLI prints it."""
    lines = [
        f"prompt {report['prompt_version']} ({str(report['prompt_sha256'])[:12]})",
        (
            "INCOMPLETE: not scored: "
            + ", ".join(f"{n['case']} ({n['status']})" for n in report["not_scored"])
            + "; no recall figure"
            if report.get("incomplete")
            else f"recall {report['recall']:.2f} ({report['found']} of {report['expected']} "
            "expected findings found)"
        )
        + f", cost ${report['cost_usd']:.4f} on the experiment line",
    ]
    for group, r in report.get("recalls", {}).items():
        shown = "n/a" if r["recall"] is None else f"{r['recall']:.2f}"
        lines.append(f"  recall {group}: {shown} ({r['found']} of {r['expected']})")
    for c in report["cases"]:
        marks = " ".join(
            f"{e['ac'] or e['class']}:{'found' if e['found'] else 'MISSED'}"
            for e in c.get("expected", [])
        ) or str(c.get("status"))
        tail = f"  error: {c['error']}" if c.get("error") else ""
        lines.append(
            f"  {c['case']}  {marks}  ${c['cost_usd']:.4f}  {c.get('replay_id') or '-'}{tail}"
        )
    return "\n".join(lines)
