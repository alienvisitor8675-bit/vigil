"""Fail when docs/feature-map.md and the code disagree.

Rules:
- every path named in a `shipped` row's "Core code" or "Tests" column exists;
- every API route declared in the code appears in some row;
- every feature-map row and every `docs/architecture.md` decision row has a
  stable slug in its first `ID` column: unique within its table and
  kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`);
- no sequential `B<n>` decision ID appears in `docs/architecture.md`;
- every slug cited in the docs as ``decision `slug` `` (or ``decisions `a`,
  `b` ``) is the ID of a decision row.

Usage: uv run python scripts/check-feature-map.py
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

ROUTE_DECL = re.compile(
    r"""\.(get|post|put|delete|patch)\(\s*["'](/(?:api|v1)/[^"']*|/api)["']""", re.IGNORECASE
)
ROUTE_IN_MAP = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_{}/.\-]*)")
BARE_ROUTE_IN_MAP = re.compile(r"(?<![\w/])(/api/[A-Za-z0-9_{}/.\-]*)")
BACKTICK = re.compile(r"`([^`]+)`")
PARAM = re.compile(r"\{[^}]*\}")
DEFAULT_CODE_DIRS = ["vigild", "cli"]
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEQUENTIAL_ID = re.compile(r"^\|\s*(B\d+)\s*\|", re.MULTILINE)
# ``decision `a` ``, ``decisions `a`, `b` and `c` ``, ``decisions `a` / `b` ``.
DECISION_REF = re.compile(
    r"\bdecisions?\s+((?:`[a-z0-9]+(?:-[a-z0-9]+)*`(?:,\s+and\s+|,\s+|\s+and\s+|\s*/\s*)?)+)",
    re.IGNORECASE,
)
SLUG_TICKS = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)*)`")


def _norm(route: str) -> str:
    route = PARAM.sub("{}", route.split("?")[0]).rstrip("/.,;")
    return route.rstrip("[") or "/"


def _expand(token: str) -> list[str]:
    """`a/{b,c}.py` -> [`a/b.py`, `a/c.py`]."""
    parts = re.split(r"(\{[^}]*,[^}]*\})", token)
    choices = [p[1:-1].split(",") if p.startswith("{") and "," in p else [p] for p in parts]
    return ["".join(c) for c in itertools.product(*choices)]


def _looks_like_path(token: str) -> bool:
    if " " in token or token.startswith(("/", "http", "-")):
        return False
    return "/" in token or bool(re.search(r"\.[a-z]{1,5}$", token))


def parse_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(set(c) <= {"-", ":"} for c in cells):
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


def parse_tables(text: str) -> list[tuple[list[str], list[dict[str, str]]]]:
    """Every table as (header, rows), in file order."""
    tables: list[tuple[list[str], list[dict[str, str]]]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            tables.append((header, []))
            continue
        if all(set(c) <= {"-", ":"} for c in cells):
            continue
        tables[-1][1].append(dict(zip(header, cells, strict=False)))
    return tables


def check_ids(name: str, text: str, *, need_column: str | None) -> tuple[list[str], set[str]]:
    """Slug rules for one file's tables; returns the errors and the IDs found.

    A table is checked when it has an `ID` first column, or (feature map) when
    it has the `need_column` header, in which case a missing `ID` is an error.
    """
    errors: list[str] = []
    found: set[str] = set()
    for header, rows in parse_tables(text):
        if header[:1] != ["ID"]:
            if need_column and need_column in header:
                errors.append(f"{name}: table with `{need_column}` column has no ID column")
            continue
        seen: set[str] = set()
        for row in rows:
            slug = row.get("ID", "")
            if not SLUG.match(slug):
                errors.append(f"{name}: ID `{slug}` is not a kebab-case slug")
            elif slug in seen:
                errors.append(f"{name}: duplicate ID `{slug}`")
            seen.add(slug)
        found |= seen
    return errors, found


def check_decisions(root: Path, arch_text: str) -> list[str]:
    errors, known = check_ids("docs/architecture.md", arch_text, need_column=None)
    for m in SEQUENTIAL_ID.finditer(arch_text):
        errors.append(f"docs/architecture.md: sequential decision ID `{m.group(1)}`; use a slug")
    for md in sorted((root / "docs").rglob("*.md")):
        for m in DECISION_REF.finditer(md.read_text()):
            for slug in SLUG_TICKS.findall(m.group(1)):
                if slug not in known:
                    rel = md.relative_to(root)
                    errors.append(f"{rel}: decision `{slug}`: no such decision in architecture.md")
    return errors


def map_routes(text: str) -> set[str]:
    found = {f"{m.group(1).upper()} {_norm(m.group(2))}" for m in ROUTE_IN_MAP.finditer(text)}
    # A bare path with no method in front counts for every method.
    found |= {f"* {_norm(m.group(1))}" for m in BARE_ROUTE_IN_MAP.finditer(text)}
    return found


def code_routes(root: Path, code_dirs: list[str]) -> set[str]:
    routes: set[str] = set()
    for d in code_dirs:
        for py in sorted((root / d).rglob("*.py")):
            for m in ROUTE_DECL.finditer(py.read_text()):
                routes.add(f"{m.group(1).upper()} {_norm(m.group(2))}")
    return routes


def check(root: Path, code_dirs: list[str] | None = None) -> list[str]:
    text = (root / "docs" / "feature-map.md").read_text()
    errors, _ = check_ids("docs/feature-map.md", text, need_column="Feature")
    arch = root / "docs" / "architecture.md"
    if arch.exists():
        errors += check_decisions(root, arch.read_text())
    for row in parse_rows(text):
        if not row.get("Status", "").startswith("shipped"):
            continue
        for col in ("Core code", "Tests"):
            for token in BACKTICK.findall(row.get(col, "")):
                if not _looks_like_path(token):
                    continue
                for path in _expand(token):
                    if not (root / path.rstrip("/")).exists():
                        errors.append(f"{row.get('Feature', '?')}: {path} does not exist")
    in_map = map_routes(text)
    for route in sorted(code_routes(root, code_dirs or DEFAULT_CODE_DIRS)):
        method, path = route.split(" ", 1)
        if route not in in_map and f"* {path}" not in in_map:
            errors.append(f"route {method} {path} is in code but in no feature-map row")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check(root)
    for e in errors:
        print(f"feature-map: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
