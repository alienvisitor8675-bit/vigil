"""Every binary vigil's code and tests shell out to is in its dev image
(owner, 2026-10-28). The dev image is validated by running the suite inside it,
offline; a tool it lacks fails that validation and blocks every job on the repo
(#57 added rsync; the image had none). This test fails in CI the day a new
binary appears without its apt package, or an explicit reason it isn't needed."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "images" / "dev" / "vigil.Dockerfile"
SCANNED = ("vigild", "cli", "scripts", "tests")
CALLS = {"run", "Popen", "check_output", "check_call", "call"}

# binary -> the Debian package the dev image installs for it
APT = {"git": "git", "rsync": "rsync"}
# in python:3.14.8-slim-trixie itself, or copied in (uv)
BASE = {"bash", "sh", "chmod", "grep", "python", "python3", "uv", "uvx"}
# never run inside the dev image, and why
NOT_IN_DEV_IMAGE = {
    "docker": "host side only: vigild drives Docker; `docker` suites are dev_excluded",
    "node": "looked up with shutil.which; the tests that need it skip without it",
}


def apt_packages() -> set[str]:
    text = DOCKERFILE.read_text()
    m = re.search(r"apt-get install -y --no-install-recommends ([^\\\n]+)", text)
    assert m, "no apt-get install line in the dev Dockerfile"
    return set(m.group(1).split())


def binaries() -> dict[str, set[str]]:
    """Literal first arguments of subprocess calls, and shutil.which lookups."""
    found: dict[str, set[str]] = {}
    for top in SCANNED:
        for p in sorted((ROOT / top).rglob("*.py")):
            for node in ast.walk(ast.parse(p.read_text(), str(p))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                a = node.args[0]
                lit = None
                if name in CALLS and isinstance(a, (ast.List, ast.Tuple)) and a.elts:
                    e = a.elts[0]
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        lit = e.value
                elif name == "which" and isinstance(a, ast.Constant) and isinstance(a.value, str):
                    lit = a.value
                if lit:
                    found.setdefault(lit, set()).add(str(p.relative_to(ROOT)))
    return found


def test_every_binary_the_suite_runs_is_in_the_dev_image() -> None:
    pkgs = apt_packages()
    missing = {}
    for b, where in binaries().items():
        if b in BASE or b in NOT_IN_DEV_IMAGE:
            continue
        if b not in APT or APT[b] not in pkgs:
            missing[b] = sorted(where)
    assert not missing, (
        f"add these to {DOCKERFILE.relative_to(ROOT)}'s apt line (and APT here), or to "
        f"NOT_IN_DEV_IMAGE with a reason: {missing}"
    )


def test_the_scan_sees_the_known_tools() -> None:
    # guards the scanner itself: if it stops finding these, it finds nothing
    found = binaries()
    assert {"git", "rsync"} <= set(found), sorted(found)
