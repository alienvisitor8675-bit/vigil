"""Fail when docs/feature-map.md and the code disagree.

Two rules:
- every path named in a `shipped` row's "Core code" or "Tests" column exists;
- every API route declared in the code appears in some row.

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
    errors: list[str] = []
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
