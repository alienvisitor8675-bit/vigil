"""Stable slug IDs: the feature-map and architecture rules of check-feature-map.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_feature_map_slugs", ROOT / "scripts" / "check-feature-map.py"
)
assert _spec and _spec.loader
fm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fm)

FEATURES = """
| ID | Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|---|
{rows}
"""
ARCH = """
| ID | Choice | Picked | Why |
|---|---|---|---|
{rows}
"""


def _feature(slug: str, name: str = "x") -> str:
    return f"| {slug} | {name} | 1 | y | — | — | — | — | planned (phase 2) |"


def _tree(
    tmp: Path,
    features: list[str] | None = None,
    arch: list[str] | None = None,
    other: str = "",
) -> Path:
    (tmp / "docs").mkdir()
    rows = features if features is not None else [_feature("alpha")]
    (tmp / "docs" / "feature-map.md").write_text(FEATURES.format(rows="\n".join(rows)))
    arows = arch if arch is not None else ["| sqlite-wal | DB | SQLite | one writer |"]
    (tmp / "docs" / "architecture.md").write_text(ARCH.format(rows="\n".join(arows)))
    (tmp / "docs" / "other.md").write_text(other)
    return tmp


def test_clean_slug_tree_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, other="Per decision `sqlite-wal`, one writer.")
    assert fm.check(root, code_dirs=[]) == []


def test_duplicate_feature_id_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, features=[_feature("alpha"), _feature("alpha", "y")])
    errors = fm.check(root, code_dirs=[])
    assert any("duplicate ID `alpha`" in e for e in errors)


def test_duplicate_architecture_id_fails(tmp_path: Path) -> None:
    row = "| sqlite-wal | DB | SQLite | one writer |"
    root = _tree(tmp_path, arch=[row, row])
    assert any("duplicate ID `sqlite-wal`" in e for e in fm.check(root, code_dirs=[]))


def test_same_slug_in_two_tables_is_fine(tmp_path: Path) -> None:
    root = _tree(tmp_path, features=[_feature("sqlite-wal")])
    assert fm.check(root, code_dirs=[]) == []


def test_bad_slug_fails(tmp_path: Path) -> None:
    for i, bad in enumerate(("Alpha", "alpha_beta", "-alpha", "alpha--beta", "alpha beta")):
        sub = tmp_path / str(i)
        sub.mkdir()
        root = _tree(sub, features=[_feature(bad)])
        errors = fm.check(root, code_dirs=[])
        assert any("is not a kebab-case slug" in e for e in errors), bad


def test_bad_architecture_slug_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, arch=["| SQLite_WAL | DB | SQLite | one writer |"])
    assert any("is not a kebab-case slug" in e for e in fm.check(root, code_dirs=[]))


def test_missing_id_column_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "docs" / "feature-map.md").write_text("| Feature | Phase |\n|---|---|\n| x | 1 |\n")
    assert any("no ID column" in e for e in fm.check(root, code_dirs=[]))


def test_new_sequential_decision_id_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path, arch=["| sqlite-wal | DB | SQLite | one writer |", "| B56 | New | x | y |"]
    )
    errors = fm.check(root, code_dirs=[])
    assert any("sequential decision ID `B56`" in e for e in errors)


def test_dangling_decision_reference_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, other="See decision `no-such-thing` for why.")
    errors = fm.check(root, code_dirs=[])
    assert any("`no-such-thing`" in e and "no such decision" in e for e in errors)


def test_dangling_reference_in_a_list_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, other="Decisions `sqlite-wal`, `gone-away` and `sqlite-wal`.")
    errors = fm.check(root, code_dirs=[])
    assert any("`gone-away`" in e for e in errors)
    assert not any("`sqlite-wal`" in e for e in errors)


def test_dangling_reference_in_feature_map_fails(tmp_path: Path) -> None:
    row = "| alpha | x | 1 | see decision `gone` | — | — | — | — | planned (phase 2) |"
    root = _tree(tmp_path, features=[row])
    assert any("`gone`" in e for e in fm.check(root, code_dirs=[]))


def test_real_repo_has_no_sequential_decision_ids() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text()
    assert "| decision `vigild-native` |" not in text
