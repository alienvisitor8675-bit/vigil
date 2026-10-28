"""The review regression set (#47): cases in `tests/fixtures/review_cases/` (no code, no
diff text; the diff comes from the vault at run time), a matcher, and a runner that
reports recall (expected findings found / expected findings) and total cost."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import auth
from tests.jobs.test_coord_loop import CLEAN_REVIEW, World
from tests.jobs.test_coord_loop import world as world
from tests.review.test_replay import FINDING, _ready, _spender
from vigild.review import replay as rp
from vigild.review.replay import Case, Expected, finding_matches, load_cases, run_set

CASES = Path(__file__).resolve().parents[1] / "fixtures" / "review_cases"
FIRST, FIX = "c902b32b3f9f252f325f1c8d1738cd031f019790", "5e57b155fdef46d266fd768492fd6094f3c1da89"


def _finding(
    claim: str, *, file: str = "src/x.test.ts", line: int | None = 10, severity: str = "medium"
) -> dict[str, Any]:
    return {"severity": severity, "file": file, "line": line, "claim": claim, "fix": "x"}


# --- the fixtures ---------------------------------------------------------------------


PA2 = "9f313ae2b3f115393567fa941c305a6a741478de"


def test_the_cases_are_the_pa_0003_and_pa_0002_misses() -> None:
    cases = {c.id: c for c in load_cases(CASES)}
    p3 = [c for c in cases.values() if c.job_id == "pa-0003"]
    assert {c.head for c in p3} == {FIRST, FIX} and {c.task_id for c in p3} == {"pa-0003-w1"}
    by_head = {c.head: sorted((e.ac, e.cls) for e in c.expected) for c in p3}
    assert by_head[FIRST] == [("AC1", "missing-case"), ("AC7", "count-out-of-bound")]
    assert by_head[FIX] == [("AC1", "missing-case"), ("AC7", "missing-assertion")]
    (p2,) = [c for c in cases.values() if c.job_id == "pa-0002"]
    assert (p2.task_id, p2.head) == ("pa-0002-w3", PA2)
    assert sorted((e.ac or "", e.cls) for e in p2.expected) == [
        ("", "stale-docs"), ("AC2", "ac-semantics-mismatch"),
    ]  # fmt: skip
    assert sum(len(c.expected) for c in cases.values()) == 6
    assert any("classifyPlace" in e.description for c in p3 for e in c.expected)


def test_recall_is_reported_three_ways() -> None:
    cases = load_cases(CASES)

    # a reviewer that finds only the AC-named misses of pa-0003's first pass and AC2
    def replay(c: Case) -> dict[str, Any]:
        f = {FIRST: [_finding("AC7: 5 cases against a bound of 2-4", file="server/test/placeClassification.test.ts", line=291)],
             PA2: [_finding("AC2: the check requires a whole word but the spec says contain", file="scripts/check-feature-map.mjs", line=86)]}  # fmt: skip
        return {"status": "done", "cost_usd": 0.1, "findings": f.get(c.head, [])}

    r = run_set(replay, cases)["recalls"]
    assert r["pa-0003"] == {"expected": 4, "found": 1, "recall": 0.25}
    assert r["ac_only"] == {"expected": 5, "found": 2, "recall": 0.4}
    assert r["all"] == {"expected": 6, "found": 2, "recall": pytest.approx(0.3333, abs=1e-3)}


def test_a_non_ac_finding_matches_on_the_expected_file_and_line() -> None:
    e = Expected(ac=None, cls="stale-docs", description="header comment is stale",
                 file="scripts/check.sh", line=1)  # fmt: skip
    assert finding_matches(
        _finding("the usage comment is stale", file="scripts/check.sh", line=3), e
    )
    assert finding_matches(_finding("anything", file="./scripts/check.sh", line=16), e)
    assert not finding_matches(_finding("the usage comment is stale", file="scripts/other.sh"), e)
    assert not finding_matches(
        _finding("the usage comment is stale", file="scripts/check.sh", line=40), e
    )


def test_cases_hold_no_code_and_no_diff_text() -> None:
    files = sorted(CASES.iterdir())
    assert files and all(f.suffix in (".yaml", ".yml", ".json") for f in files)
    for f in files:
        raw = f.read_text()
        data = yaml.safe_load(raw)
        assert set(data) == {"id", "job_id", "task_id", "head", "expected"}, f.name
        assert re.fullmatch(r"[0-9a-f]{40}", data["head"])
        for e in data["expected"]:
            assert set(e) == {"ac", "class", "description", "file", "line"}, f.name
            assert "\n" not in e["file"] and isinstance(e["line"], int)
            assert e["ac"] is None or re.fullmatch(r"AC\d+", e["ac"])
            assert len(e["description"]) <= 200  # a short description, not a quote
        assert "diff --git" not in raw and "@@" not in raw and "```" not in raw
        assert len(raw) < 2000


def test_a_case_with_anything_else_in_it_is_refused(tmp_path: Path) -> None:
    ok = {"id": "c", "job_id": "j-1", "task_id": "j-1-w1", "head": "a" * 40,
          "expected": [{"ac": "AC1", "class": "missing-case", "description": "d",
                        "file": "a.ts", "line": 1}]}  # fmt: skip
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(ok))
    assert [c.id for c in load_cases(tmp_path)] == ["c"]
    for bad in ({**ok, "diff": "+ x"}, {**ok, "head": "main"}, {**ok, "expected": []}):
        (tmp_path / "a.yaml").write_text(yaml.safe_dump(bad))
        with pytest.raises(ValueError):
            load_cases(tmp_path)


# --- the matcher ----------------------------------------------------------------------


E_BOUND = Expected(
    ac="AC7",
    cls="count-out-of-bound",
    description="5 cases against 2-4",
    file="src/x.test.ts",
    line=10,
)
E_ASSERT = Expected(
    ac="AC7",
    cls="missing-assertion",
    description="no located/source assert",
    file="src/x.test.ts",
    line=10,
)
E_CASE = Expected(
    ac="AC1",
    cls="missing-case",
    description="no empty-understood row",
    file="src/x.test.ts",
    line=10,
)


def test_a_finding_matches_when_it_names_the_ac_on_the_right_file_and_line() -> None:
    assert finding_matches(_finding("AC7 allows 2-4 classifyPlace cases but there are 5"), E_BOUND)
    assert finding_matches(_finding("ac 7: the case count exceeds the bound", line=25), E_BOUND)
    assert finding_matches(_finding("AC7: two cases never assert located", line=10), E_ASSERT)
    assert finding_matches(_finding("AC1 is not covered: no row has empty understood"), E_CASE)
    # the class is not needed for a match
    assert finding_matches(_finding("AC7: the name is unclear"), E_BOUND)


def test_a_generic_finding_on_another_file_does_not_match() -> None:
    assert not finding_matches(_finding("missing check", file="src/other.ts"), E_ASSERT)
    assert not finding_matches(_finding("count is off", file="src/other.ts"), E_BOUND)
    # even naming the AC, the wrong file or a far line is no match
    assert not finding_matches(_finding("AC7: bound exceeded", file="src/other.ts"), E_BOUND)
    assert not finding_matches(_finding("AC7: bound exceeded", line=26), E_BOUND)
    assert not finding_matches(_finding("AC7: bound exceeded", line=None), E_BOUND)
    # the right file and line without the AC, or with another, is no match
    assert not finding_matches(_finding("the case count exceeds the bound"), E_BOUND)
    assert not finding_matches(_finding("AC8 allows 2-4 cases but there are 5"), E_BOUND)
    assert not finding_matches(_finding("AC17 allows 2-4 cases but there are 5"), E_BOUND)


def test_the_ac_id_must_be_a_whole_token() -> None:
    assert finding_matches(_finding("(AC7) bound exceeded"), E_BOUND)
    assert not finding_matches(_finding("AC70 bound exceeded"), E_BOUND)
    assert not finding_matches(_finding("BAC7 bound exceeded"), E_BOUND)


def test_the_class_only_breaks_a_tie() -> None:
    cases = [_case("c", "a" * 40, E_ASSERT)]
    plain = _finding("AC7: something else is wrong here")
    fits = _finding("AC7: the located assertion is never checked", line=12)
    out = {"status": "done", "cost_usd": 0.0, "findings": [plain, fits]}
    run = run_set(lambda c: out, cases)
    assert run["found"] == 1
    out2 = {**out, "findings": [plain]}
    assert run_set(lambda c: out2, cases)["found"] == 1  # a lone match without the class counts


# --- the runner -----------------------------------------------------------------------


def _case(cid: str, head: str, *expected: Expected) -> Case:
    return Case(id=cid, job_id="j-1", task_id="j-1-w1", head=head, expected=list(expected))


def test_recall_is_expected_findings_found_over_expected_findings() -> None:
    cases = [_case("first", "a" * 40, E_BOUND, E_CASE), _case("fix", "b" * 40, E_ASSERT, E_CASE)]
    answers = {
        "a" * 40: {"findings": [_finding("AC7: 5 cases against a bound of 2-4"),
                                _finding("AC3: unrelated")], "cost_usd": 0.20},
        "b" * 40: {"findings": [_finding("AC7: located assertion missing"),
                                _finding("AC1: no charging lot row is covered")],
                   "cost_usd": 0.15},
    }  # fmt: skip
    report = run_set(lambda c: {"status": "done", "id": f"rp-{c.id}", **answers[c.head]}, cases)
    assert (report["expected"], report["found"]) == (4, 3)
    assert report["recall"] == pytest.approx(0.75)
    assert report["cost_usd"] == pytest.approx(0.35)
    first, fix = report["cases"]
    assert [e["found"] for e in first["expected"]] == [True, False]
    assert [e["found"] for e in fix["expected"]] == [True, True]
    assert first["replay_id"] == "rp-first"


def test_one_finding_counts_for_one_expected_finding_only() -> None:
    cases = [_case("c", "a" * 40, E_BOUND, Expected(ac="AC7", cls="count-out-of-bound",
                                                    description="again", file="src/x.test.ts",
                                                    line=10))]  # fmt: skip
    one = {"status": "done", "cost_usd": 0.1, "findings": [_finding("AC7: bound exceeded")]}
    assert run_set(lambda c: one, cases)["found"] == 1
    two = {**one, "findings": [_finding("AC7: bound exceeded"), _finding("AC7: too many cases")]}
    assert run_set(lambda c: two, cases)["found"] == 2


def test_a_failed_case_is_not_scored_and_makes_the_run_incomplete() -> None:
    """Owner, #49: any status but done (refused, failed, error) counts in neither side
    of recall, is listed under not_scored with its status, and the run is incomplete.
    Its cost still counts."""
    ok = {"status": "done", "findings": [], "cost_usd": 0.05}
    bad = {"status": "failed", "findings": [], "cost_usd": 0.12, "error": "no valid review.json"}
    cases = [_case("ok", "a" * 40, E_BOUND), _case("bad", "b" * 40, E_BOUND)]
    report = run_set(lambda c: ok if c.id == "ok" else bad, cases)
    assert report["incomplete"] is True and report["recall"] is None
    assert report["not_scored"] == [{"case": "bad", "status": "failed"}]
    assert report["expected"] == 1  # only the done case is scored
    assert report["cost_usd"] == pytest.approx(0.17)
    assert report["cases"][1]["error"] == "no valid review.json"


def test_a_case_the_vault_cannot_serve_is_an_error_not_a_crash() -> None:
    def replay(c: Case) -> dict[str, Any]:
        raise rp.ReplayInvalid("no such head in the vault")

    report = run_set(replay, [_case("c", "a" * 40, E_BOUND)])
    assert report["not_scored"] == [{"case": "c", "status": "error"}]
    assert report["incomplete"] is True and "no such head" in report["cases"][0]["error"]


def test_a_refusal_stops_the_set() -> None:
    def replay(c: Case) -> dict[str, Any]:
        raise rp.ReplayRefused(409, "a breaker is open")

    with pytest.raises(rp.ReplayRefused):
        run_set(replay, [_case("c", "a" * 40, E_BOUND)])


# --- the route, end to end ------------------------------------------------------------


def test_the_set_route_replays_each_case_and_reports_recall_and_cost(
    world: World,
    prompts: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world
    job, task = _ready(w)
    head = w.runner._task_head(task)
    assert head
    case = {"id": "fixture-1", "job_id": job, "task_id": task, "head": head,
            "expected": [{"ac": "AC1", "class": "missing-case", "description": "no row",
                          "file": "src/x.test.ts", "line": 10},
                         {"ac": "AC2", "class": "missing-assertion", "description": "no assert",
                          "file": "src/x.test.ts", "line": 10}]}  # fmt: skip
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "one.yaml").write_text(yaml.safe_dump(case))
    monkeypatch.setattr(rp, "CASES_DIR", cases)
    spend = _spender(w)
    w.standin.review = lambda spec: {  # one match: AC1 is missing; nothing about AC2
        **spend(spec),
        "findings": [_finding("AC1: no row covers this criterion")],
    }
    day = w.budget.spent_today()
    body = {"prompt_version": "new"}
    assert w.client.post("/api/review/replay-set", json=body).status_code == 401
    assert w.client.post("/api/review/replay-set", headers=auth(w.tokens.read),
                         json=body).status_code == 403  # fmt: skip
    bad = w.client.post("/api/review/replay-set", headers=auth(w.tokens.admin),
                        json={**body, "visibility": "public"})  # fmt: skip
    assert bad.status_code == 422
    r = w.client.post("/api/review/replay-set", headers=auth(w.tokens.admin), json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["expected"], out["found"], out["recall"]) == (2, 1, 0.5)
    assert out["cost_usd"] == pytest.approx(0.30) and out["prompt_version"] == "new"
    assert out["prompt_sha256"] == prompts["new"]
    assert out["cases"][0]["replay_id"].startswith("rp-")
    # the cost is in the experiment line only
    assert w.budget.spent_today() == pytest.approx(day)
    assert w.budget.spent_experiment_month() == pytest.approx(0.30)
    assert json.loads(w.store.q("SELECT findings FROM replays")[0]["findings"])[0]["claim"]
    # a head the task never published is a case error, not a replay
    case["head"] = "c" * 40
    (cases / "one.yaml").write_text(yaml.safe_dump(case))
    out2 = w.client.post("/api/review/replay-set", headers=auth(w.tokens.admin), json=body).json()
    assert out2["found"] == 0 and out2["cases"][0]["error"]
    assert int(w.store.q("SELECT COUNT(*) FROM replays")[0][0]) == 1


# --- the prompt versions --------------------------------------------------------------


def test_a_prompt_version_is_a_ref_or_a_content_hash_of_this_repos_reviewer_prompt(
    prompts: dict[str, str],
) -> None:
    old = rp.resolve_prompt("old")
    assert old.text == "# Role: reviewer\nOLD-PROMPT-MARKER\n" and old.sha256 == prompts["old"]
    assert rp.resolve_prompt(prompts["new"]).text.endswith("NEW-PROMPT-MARKER\n")
    assert rp.resolve_prompt("main~2").sha256 == prompts["old"]  # any ref git resolves
    assert rp.resolve_prompt(prompts["prod"]).sha256 == prompts["prod"]
    for bad in ("", "no-such-ref", "0" * 64, "-p", "--output=x", "old;ls", "HEAD:README.md",
                "old:prompts/coord.md", "a" * 41):  # fmt: skip
        with pytest.raises(rp.ReplayInvalid):
            rp.resolve_prompt(bad)


def test_only_a_prompt_that_shipped_on_main_is_a_version(prompts: dict[str, str]) -> None:
    """A version is a commit reachable from origin/main: a feature branch, a stash or
    any other ref is refused, by ref name and by content hash."""
    from tests.review.conftest import _git

    root = rp.REPO_ROOT
    _git(root, "checkout", "-q", "-b", "feat/x")
    (root / "prompts" / "reviewer.md").write_text("# Role: reviewer\nBRANCH-ONLY-MARKER\n")
    _git(root, "commit", "-qam", "branch only")
    branch_sha = hashlib.sha256(b"# Role: reviewer\nBRANCH-ONLY-MARKER\n").hexdigest()
    _git(root, "tag", "branch-tag")
    (root / "prompts" / "reviewer.md").write_text("# Role: reviewer\nSTASHED-MARKER\n")
    _git(root, "stash", "-q")  # a stash entry holds a prompt nobody shipped
    for ref in ("feat/x", "branch-tag", "HEAD", "stash@{0}"):
        with pytest.raises(rp.ReplayInvalid):
            rp.resolve_prompt(ref)
    with pytest.raises(rp.ReplayInvalid):
        rp.resolve_prompt(branch_sha)  # nor by content hash: `--all` is out
    assert rp.resolve_prompt("old").sha256 == prompts["old"]  # a tag on main is fine
    assert rp.resolve_prompt(prompts["new"]).sha256 == prompts["new"]


def test_a_repo_without_origin_main_refuses_every_version(prompts: dict[str, str]) -> None:
    from tests.review.conftest import _git

    _git(rp.REPO_ROOT, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(rp.ReplayInvalid):
        rp.resolve_prompt("old")


def test_the_resolver_reads_through_gitsafe_only(
    prompts: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    real = subprocess.run

    def spy(*a: Any, **k: Any) -> Any:
        calls.append(tuple(a[0]))
        return real(*a, **k)

    monkeypatch.setattr("vigild.gitsafe.subprocess.run", spy)
    rp.resolve_prompt("old")
    assert calls and all("core.hooksPath=/dev/null" in c for c in calls)
    assert all(c[0] == "git" for c in calls)


def test_a_session_replay_uses_no_unlisted_reviewer_prompt(
    world: World,
    prompts: dict[str, str],
) -> None:
    """The shipped prompt is untouched by a replay of an older one."""
    w = world
    job, task = _ready(w)
    w.standin.review = lambda spec: CLEAN_REVIEW
    r = w.client.post("/api/review/replay", headers=auth(w.tokens.admin),
                      json={"job_id": job, "task_id": task, "prompt_version": "old"})  # fmt: skip
    assert r.status_code == 200
    assert FINDING and "OLD-PROMPT-MARKER" in w.standin.specs[-1].packet
    from vigild.packets.assemble import PROMPTS_DIR

    assert "OLD-PROMPT-MARKER" not in (PROMPTS_DIR / "reviewer.md").read_text()


def _mk_case(cid: str, *expected: dict[str, Any]) -> Any:
    from vigild.review.replay import Case

    return Case.model_validate(
        {"id": cid, "job_id": "j", "task_id": "t", "head": "a" * 40, "expected": list(expected)}
    )


def test_a_gate_refused_replay_makes_the_run_incomplete() -> None:
    """Owner, #49 round 2: a case refused at the gate is listed apart, counts in neither
    side of recall, and the run reports incomplete instead of a recall figure."""
    from vigild.review.replay import run_set

    ok = _mk_case("ok", {"ac": "AC1", "class": "missing-case", "description": "d",
                      "file": "a.ts", "line": 10})  # fmt: skip
    gated = _mk_case("gated", {"ac": "AC2", "class": "missing-case", "description": "d",
                            "file": "a.ts", "line": 50})  # fmt: skip

    def replay(case: Any) -> dict[str, Any]:
        if case.id == "gated":
            return {"status": "refused", "findings": [], "cost_usd": 0.0,
                    "error": "gate_refused: backend breaker not closed"}  # fmt: skip
        return {"status": "done", "findings": [_finding("AC1: missing", file="a.ts", line=11)],
                "cost_usd": 0.1}  # fmt: skip

    report = run_set(replay, [ok, gated])
    assert report["incomplete"] is True and report["recall"] is None
    assert report["not_scored"] == [{"case": "gated", "status": "refused"}]
    assert report["expected"] == 1 and report["found"] == 1  # the refused case is out


def test_matching_is_one_to_one_ac_cases_first_and_null_never_takes_an_ac_finding() -> None:
    from vigild.review.replay import run_set

    # the null case is listed first, but the AC case must get the only AC finding
    case = _mk_case(
        "c",
        {"ac": None, "class": "stale-docs", "description": "d", "file": "a.ts", "line": 10},
        {"ac": "AC3", "class": "missing-case", "description": "d", "file": "a.ts", "line": 12},
    )

    def replay(_: Any) -> dict[str, Any]:
        return {"status": "done", "cost_usd": 0.0,
                "findings": [_finding("AC3: a case is missing", file="a.ts", line=11)]}  # fmt: skip

    report = run_set(replay, [case])
    rows = report["cases"][0]["expected"]
    assert [r["found"] for r in rows] == [False, True]
