"""Glob semantics shared with guard and route."""

from __future__ import annotations

import pytest

from vigild.intake.paths import matches, matches_any


@pytest.mark.parametrize(
    "path,glob,expected",
    [
        ("src/billing/charge.py", "src/billing/**", True),
        ("server/src/billing/x/y.ts", "src/billing/**", True),  # tail match
        ("src/billingx/a.py", "src/billing/**", False),
        ("a/payments/b.py", "**/payments/**", True),
        ("payments/b.py", "**/payments/**", True),  # leading **/ may be empty
        ("deep/dir/.env", ".env", True),  # no slash: basename anywhere
        ("deep/dir/.ENV", ".env", True),  # case-insensitive
        ("deep/dir/x.env.local", "*.env.*", True),
        ("docs/a.md", "*.py", False),
        ("fly.toml", "fly.toml", True),
    ],
)
def test_glob_semantics(path: str, glob: str, expected: bool) -> None:
    assert matches(path, glob) is expected


def test_matches_any() -> None:
    assert matches_any("src/auth/login.py", ["src/billing/**", "src/auth/**"])
    assert not matches_any("docs/x.md", ["src/billing/**"])


# --- scope globs are strict (review round 1, item 14) -----------------------------------

from vigild.intake.paths import globs_overlap, in_scope, scope_entry_error  # noqa: E402


@pytest.mark.parametrize(
    "path,glob,expected",
    [
        ("docs/a.md", "docs/a.md", True),
        ("docs/a.md", "docs/*.md", True),
        ("docs/sub/a.md", "docs/*.md", False),  # * stays inside a segment
        ("docs/sub/a.md", "docs/**/*.md", True),  # ** crosses segments
        ("docs/a.md", "docs/**/*.md", True),  # ** may be empty
        ("src/docs/a.md", "docs/*.md", False),  # anchored: no tail matching
        ("a.md", "*.md", True),
        ("deep/a.md", "*.md", False),  # no basename-anywhere for scope
        ("Docs/A.md", "docs/a.md", False),  # case-sensitive
        ("src/billing/x.py", "src/**", True),
    ],
)
def test_scope_matching_is_strict(path: str, glob: str, expected: bool) -> None:
    assert in_scope(path, [glob]) is expected


@pytest.mark.parametrize(
    "entry", ["../etc/passwd", "/etc/passwd", "docs/../../x", "a\\b", "", "~/x"]
)
def test_bad_scope_entries_are_rejected(entry: str) -> None:
    assert scope_entry_error(entry)


def test_good_scope_entries_pass() -> None:
    assert scope_entry_error("docs/**/*.md") is None


@pytest.mark.parametrize(
    "a,b,overlap",
    [
        ("docs/a.md", "docs/a.md", True),
        ("docs/a.md", "docs/b.md", False),
        ("src/*.py", "src/a*", True),  # glob vs glob: not provably disjoint
        ("src/*.py", "docs/*.md", False),  # distinct literal prefixes
        ("src/**", "src/a/b.py", True),
        ("src/a.py", "src/*.py", True),
        ("**/x.py", "docs/*.py", True),  # no literal prefix: assume overlap
        ("**/x.py", "docs/a.md", False),  # a literal path is checked exactly
    ],
)
def test_glob_overlap(a: str, b: str, overlap: bool) -> None:
    assert globs_overlap(a, b) is overlap
