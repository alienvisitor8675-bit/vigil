"""The pin rule for images (phase 2a, item 0): every Dockerfile's `syntax=`
frontend is pinned by digest, like every base image."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNTAX = re.compile(r"^#\s*syntax\s*=\s*(\S+)\s*$")
PINNED = re.compile(r"^[a-z0-9./_-]+:[0-9][0-9.]*@sha256:[0-9a-f]{64}$")


def unpinned_syntax_lines(text: str) -> list[str]:
    """Each `# syntax=` directive that isn't `image:version@sha256:<digest>`."""
    out = []
    for line in text.splitlines():
        m = SYNTAX.match(line.strip())
        if m and not PINNED.match(m.group(1)):
            out.append(line)
    return out


def test_the_check_refuses_an_unpinned_frontend() -> None:
    assert unpinned_syntax_lines("# syntax=docker/dockerfile:1\nFROM x\n")
    assert unpinned_syntax_lines("# syntax = docker/dockerfile:1.28.0\n")
    assert unpinned_syntax_lines("#syntax=docker/dockerfile@sha256:" + "a" * 64 + "\n")
    ok = "# syntax=docker/dockerfile:1.28.0@sha256:" + "b" * 64 + "\n"
    assert unpinned_syntax_lines(ok) == []


def test_every_dockerfile_pins_its_syntax_frontend() -> None:
    files = sorted(ROOT.glob("images/**/*Dockerfile"))
    assert files
    bad = {str(f.relative_to(ROOT)): unpinned_syntax_lines(f.read_text()) for f in files}
    assert {k: v for k, v in bad.items() if v} == {}
