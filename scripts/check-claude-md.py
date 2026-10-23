"""Layer 1 of the documentation standard: CLAUDE.md is at most 200 lines,
and AGENTS.md is a symlink to it, so both harnesses read one file.

Usage: uv run python scripts/check-claude-md.py
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_LINES = 200


def check(root: Path) -> list[str]:
    errors: list[str] = []
    claude = root / "CLAUDE.md"
    if not claude.is_file():
        return ["CLAUDE.md is missing"]
    n = len(claude.read_text().splitlines())
    if n > MAX_LINES:
        errors.append(f"CLAUDE.md is {n} lines; the limit is {MAX_LINES}")
    agents = root / "AGENTS.md"
    if not agents.is_symlink() or agents.readlink() != Path("CLAUDE.md"):
        errors.append("AGENTS.md must be a symlink to CLAUDE.md")
    return errors


def main() -> int:
    errors = check(Path(__file__).resolve().parents[1])
    for e in errors:
        print(f"claude-md: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
