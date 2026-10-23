"""Run the CLI from the repo: uv run python cli/vigil <command>"""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
for p in (str(_root), str(_root / "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

from vigil.main import app  # noqa: E402

app()
