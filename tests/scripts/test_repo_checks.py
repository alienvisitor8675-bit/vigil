"""The repo checks CI runs: the feature map and CLAUDE.md."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fm = _load("check-feature-map.py")
cm = _load("check-claude-md.py")

MAP = """
| ID | Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|---|
| a | A | 1 | x | `GET /api/things/{id}` | `pkg/a.py`, `pkg/{b,c}.py` | — | `tests/test_a.py` | shipped |
| b | B | 2 | y | `POST /api/later` | `pkg/missing.py` | — | — | planned (phase 2) |
"""


def _tree(tmp: Path, files: list[str], source: str = "") -> Path:
    (tmp / "docs").mkdir()
    (tmp / "docs" / "feature-map.md").write_text(MAP)
    for f in files:
        p = tmp / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source if f.endswith("api.py") else "")
    return tmp


def test_shipped_row_paths_must_exist(tmp_path: Path) -> None:
    root = _tree(tmp_path, ["pkg/a.py", "pkg/b.py", "tests/test_a.py"])
    errors = fm.check(root, code_dirs=["pkg"])
    assert any("pkg/c.py" in e for e in errors)
    # A planned row may name paths that don't exist yet.
    assert not any("pkg/missing.py" in e for e in errors)


def test_clean_tree_passes(tmp_path: Path) -> None:
    src = '@router.get("/api/things/{thing_id}")\ndef f(): ...\n'
    root = _tree(
        tmp_path, ["pkg/a.py", "pkg/b.py", "pkg/c.py", "tests/test_a.py", "pkg/api.py"], src
    )
    assert fm.check(root, code_dirs=["pkg"]) == []


def test_route_in_code_but_in_no_row_fails(tmp_path: Path) -> None:
    src = '@router.post("/api/secret-door")\ndef f(): ...\n'
    root = _tree(
        tmp_path, ["pkg/a.py", "pkg/b.py", "pkg/c.py", "tests/test_a.py", "pkg/api.py"], src
    )
    errors = fm.check(root, code_dirs=["pkg"])
    assert errors == ["route POST /api/secret-door is in code but in no feature-map row"]


def test_real_repo_feature_map_is_clean() -> None:
    assert fm.check(ROOT) == []


def test_claude_md_limits(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("x\n" * 201)
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    assert any("201 lines" in e for e in cm.check(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("x\n" * 10)
    assert cm.check(tmp_path) == []
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "AGENTS.md").write_text("copy")
    assert any("symlink" in e for e in cm.check(tmp_path))


def test_real_repo_claude_md_is_clean() -> None:
    assert cm.check(ROOT) == []


ds = _load("check-docs-same-pr.py")


def test_flow_change_without_architecture_fails() -> None:
    errors = ds.check(["vigild/jobs/fsm.py", "tests/jobs/test_fsm.py"])
    assert errors and "job state machine" in errors[0]


def test_flow_change_with_architecture_passes() -> None:
    assert ds.check(["vigild/gateway/service.py", "docs/architecture.md"]) == []


def test_non_flow_change_passes() -> None:
    assert ds.check(["vigild/decider/questions.py", "README.md"]) == []


def test_nested_flow_files_match() -> None:
    assert ds.touched_flows(["images/claude-executor/Dockerfile"]) == {
        "isolation and deploy": ["images/claude-executor/Dockerfile"]
    }


def test_a_planned_page_row_with_no_route_or_code_yet_passes(tmp_path: Path) -> None:
    """/watch (round 2): a phase-3 row names a page, a stream topic and code
    that don't exist yet. Planned, that's fine; shipped, the paths must exist."""
    row = (
        "| watch-page | Watch page | 3 | x | `/watch`; `GET /api/stream?topics=code,tower` | `vigild/web/`, "
        "`vigild/api/stream.py` | — | `tests/web/test_watch.py` | {status} |\n"
    )
    root = _tree(tmp_path, ["pkg/a.py", "pkg/b.py", "pkg/c.py", "tests/test_a.py"])
    fmap = root / "docs" / "feature-map.md"
    fmap.write_text(MAP + row.format(status="planned (phase 3)"))
    assert fm.check(root, code_dirs=["pkg"]) == []
    fmap.write_text(MAP + row.format(status="shipped"))
    errors = fm.check(root, code_dirs=["pkg"])
    assert any("vigild/api/stream.py" in e for e in errors)
