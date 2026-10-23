"""Phase 1 run, finding #1: test ids that change on every run. The base runs
twice; an id in one base run and not the other is unstable, and is matched by
(file, title without its parameters), by count."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import git
from tests.test_verifier import _session, _turn_on, _verify, ws  # noqa: F401
from vigild.verifier import Case, compare_runs, normalise_title
from vigild.workspace import Workspace

EACH_TEST = (
    "import uuid\n\nimport pytest\n\n\n"
    "@pytest.mark.parametrize('key', [str(uuid.uuid4()) for _ in range({n})])\n"
    "def test_each(key):\n    assert len(key) == 36\n"
)


@pytest.fixture
def ws_each(ws: Workspace, fixture_repo: dict[str, Path]) -> Workspace:  # noqa: F811
    """The same repo, with a suite whose test ids carry a fresh uuid every run."""
    seed = fixture_repo["seed"]
    (seed / "tests" / "test_each.py").write_text(EACH_TEST.format(n=3))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "an unstable suite")
    git(seed, "push", "-q", "origin", "main")
    return ws


def test_ids_that_change_every_run_are_matched_by_title_and_count(
    ws_each: Workspace, tmp_path: Path
) -> None:
    base, head = _session(ws_each, tmp_path, _turn_on)
    rec = _verify(ws_each, base, head)
    assert rec["after"]["outcome"] == "pass"
    assert rec["ran"]["ok"], rec["ran"]
    assert rec["ran"]["unstable"] == [
        {"file": "tests.test_each", "title": "test_each", "base": 3, "head": 3}
    ]
    assert rec["ran"]["ids"]["base"] == 5  # 2 old + 3 each
    assert rec["base_repeat"]["tests"] == 5


def test_a_drop_in_the_count_of_an_unstable_title_still_fails(
    ws_each: Workspace, tmp_path: Path
) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_each.py").write_text(EACH_TEST.format(n=2))

    base, head = _session(ws_each, tmp_path, edit)
    rec = _verify(ws_each, base, head)
    assert not rec["ran"]["ok"]
    assert rec["ran"]["unstable"][0]["base"] == 3 and rec["ran"]["unstable"][0]["head"] == 2
    assert rec["ran"]["missing_ids"] == ["tests.test_each::test_each: 3 at base, 2 at head"]


def test_a_stable_test_is_still_matched_by_exact_id(ws_each: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n")

    base, head = _session(ws_each, tmp_path, edit)
    rec = _verify(ws_each, base, head)
    assert not rec["ran"]["ok"]
    assert rec["ran"]["missing_ids"] == ["tests.test_old::test_old_2"]


def test_a_suite_with_nothing_unstable_has_an_empty_list(ws: Workspace, tmp_path: Path) -> None:  # noqa: F811
    base, head = _session(ws, tmp_path, _turn_on)
    rec = _verify(ws, base, head)
    assert rec["ran"]["ok"] and rec["ran"]["unstable"] == []


def test_titles_lose_only_their_parameters() -> None:
    uid = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"
    pem = (
        "-----BEGIN "
        + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END "
        + "PRIVATE KEY-----\n"
    )
    assert normalise_title(f"test_a[{uid}]") == "test_a"
    assert normalise_title("test_a[x[0]-y]") == "test_a"
    assert normalise_title(f"accepts {uid} as an id") == "accepts <> as an id"
    assert normalise_title(f"loads {pem}") == "loads <>"
    assert normalise_title("key a1b2c3d4e5f60718") == "key <>"
    assert normalise_title("token Zm9vYmFyLWJhei1xdXV4LTEyMzQ1Ng==") == "token <>"
    assert normalise_title("rejects a plain short title") == "rejects a plain short title"
    assert normalise_title("works for 12") == "works for 12"  # short numbers are not parameters


def test_compare_runs_with_vitest_shaped_unstable_titles() -> None:
    """vitest names a test by its title under the file; test.each interpolates
    crypto.randomUUID() into it."""

    def run(*uuids: str) -> list[Case]:
        return [Case(f"f.test.ts::loads {u}", "f.test.ts", "pass") for u in uuids] + [
            Case("f.test.ts::stable", "f.test.ts", "pass")
        ]

    a = run("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
    b = run("33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444")
    h = run("55555555-5555-4555-8555-555555555555", "66666666-6666-4666-8666-666666666666")
    assert compare_runs(a, h)["ok"] is False  # one base run: the ids never match
    got = compare_runs(a, h, b)
    assert got["ok"] and got["unstable"][0]["title"] == "loads <>"
    assert not compare_runs(a, h[1:], b)["ok"]  # a drop in count fails


# --- review of PR 17 ------------------------------------------------------------------------


def _c(file: str, name: str) -> Case:
    return Case(f"{file}::{name}", file, "pass")


def test_a_base_repeat_with_no_report_falls_back_to_exact_ids() -> None:
    base = [_c("f", "x 111"), _c("f", "stable")]
    head = [_c("f", "other")]
    assert not compare_runs(base, head, [])["ok"]
    assert "stable" in " ".join(compare_runs(base, head, [])["missing_ids"])
    assert not compare_runs(base, head, None)["ok"]


def test_an_unmasked_random_value_cannot_be_deleted() -> None:
    a = [_c("f", "x 111"), _c("f", "keep")]
    b = [_c("f", "x 222"), _c("f", "keep")]
    got = compare_runs(a, [_c("f", "keep")], b)
    assert not got["ok"], got
    assert compare_runs(a, [_c("f", "keep"), _c("f", "x 333")], b)["ok"]


def test_snake_case_names_with_a_digit_are_not_parameters() -> None:
    one = normalise_title("test_rejects_expired_token_v1")
    two = normalise_title("test_rejects_missing_token_v2")
    assert one != two and "<>" not in one + two
    assert normalise_title("jwt Zm9vYmFyLWJhei1xdXV4LTEyMzQ1Ng") == "jwt <>"


def test_a_verifier_with_a_reportless_repeat_records_it_unavailable(
    ws: Workspace,  # noqa: F811
    tmp_path: Path,
) -> None:
    from tests.harness import LocalTestRunner
    from vigild.verifier import RunResult

    class Flaky(LocalTestRunner):
        n = 0

        def run(self, *a: object, **k: object) -> RunResult:
            self.n += 1
            r = super().run(*a, **k)  # type: ignore[arg-type]
            return RunResult(r.exit, r.output) if self.n == 2 else r

    base, head = _session(ws, tmp_path, _turn_on)
    rec = _verify(ws, base, head, runner=Flaky())
    assert rec["base_repeat"] == "unavailable"
    assert rec["ran"]["unstable"] == [] and rec["ran"]["ok"]
