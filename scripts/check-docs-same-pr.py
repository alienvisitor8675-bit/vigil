"""Layer 2 of the documentation standard: a PR that touches a flow's files
updates docs/architecture.md in the same PR.

Usage: uv run python scripts/check-docs-same-pr.py [BASE_REF]   (default origin/main)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vigild.intake.paths import matches  # noqa: E402

ARCH = "docs/architecture.md"
# The flows docs/architecture.md draws, and the files that implement them.
FLOWS: dict[str, list[str]] = {
    "job state machine": ["vigild/jobs/fsm.py", "vigild/jobs/model.py"],
    "job tree, inbox and runner": [
        "vigild/jobs/service.py",
        "vigild/jobs/runner.py",
        "vigild/jobs/tree.py",
    ],
    "money path": [
        "vigild/budget.py",
        "vigild/gateway/*",
        "vigild/metering/*",
        "configs/budgets.yaml",
        "configs/prices.yaml",
    ],
    "isolation and deploy": [
        "images/*",
        "compose.yaml",
        "vigild/executors/*",
        "configs/egress.yaml",
        "configs/managed-settings.json",
        "configs/otel-collector.yaml",
    ],
    "GitHub and repo policy": ["vigild/github/*", "vigild/repos.py", "configs/repos.yaml"],
}


def touched_flows(changed: list[str]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for flow, globs in FLOWS.items():
        files = [f for f in changed if any(f == g or matches(f, g) for g in globs)]
        if files:
            hits[flow] = files
    return hits


def check(changed: list[str]) -> list[str]:
    if ARCH in changed:
        return []
    return [
        f"{flow}: {', '.join(files)} changed but {ARCH} did not"
        for flow, files in touched_flows(changed).items()
    ]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    errors = check([f for f in out.stdout.splitlines() if f])
    for e in errors:
        print(f"docs-same-pr: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
