"""The owner's replan note (owner spec, 2026-10-25): its size cap, the diff the
spec item shows, and the lines of the note that go to the hidden-test author.

None of this involves a model. The note comes only through the admin API, so it
is the owner's text: it is not fenced as untrusted."""

from __future__ import annotations

import difflib
import re

from vigild.jobs.artifacts import MD_PROV

NOTE_MAX_BYTES = 8192

HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
NUMBERED = re.compile(r"^([ \t]*)\d{1,3}[.)][ \t]+(.*)$")
AUTHOR = re.compile(r"\bauthor\b", re.I)
BULLET = re.compile(r"^[ \t]*[-*•◦][ \t]")


def spec_body(text: str) -> str:
    """A stored spec without its provenance comment."""
    return MD_PROV.sub("", text)


def spec_diff(previous: str, amended: str, baseline: str = "previously approved") -> str:
    """A unified diff of the baseline spec (the last approved one, or "current draft,
    never approved") against the amended one ('' when they are the same text),
    ignoring the provenance comments."""
    old = spec_body(previous).splitlines()
    new = spec_body(amended).splitlines()
    lines = difflib.unified_diff(
        old, new, f"spec.md ({baseline})", "spec.md (amended)", lineterm=""
    )
    out = "\n".join(lines)
    return out + "\n" if out else ""


def _section(lines: list[str], i: int) -> int:
    """The index one past the section that starts at line i. A heading runs to the
    next heading of its level or above. A numbered item runs to the next heading or
    numbered item at its indent or above. Its body is its indented and bullet lines
    and, until it has had one, its plain lines (a plain line after bullets is the
    note's own text, not the item's). Blank lines never end it."""
    h = HEADING.match(lines[i])
    j = i + 1
    if h:
        level = len(h.group(1))
        while j < len(lines):
            m = HEADING.match(lines[j])
            if m and len(m.group(1)) <= level:
                break
            j += 1
        return j
    indent = len(NUMBERED.match(lines[i]).group(1).expandtabs())  # type: ignore[union-attr]
    structured = False
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            pass  # a blank line never ends an item: only the next item or heading does
        elif HEADING.match(line):
            break
        else:
            lead = len(line) - len(line.lstrip())
            if NUMBERED.match(line) and lead <= indent:
                break
            if BULLET.match(line) or lead > indent:
                structured = True
            elif structured:
                break
        j += 1
    return j


def author_contract(note: str) -> str:
    """The lines of the note under a heading or a numbered item whose title has the
    word "author" (for example "Hidden-test author contract"; not "authorization"),
    each section from its title line to its end; several are joined by a blank
    line. When no title has it, or the matched sections have no body, the whole note."""
    lines = note.splitlines()
    sections: list[str] = []
    i = 0
    while i < len(lines):
        h = HEADING.match(lines[i])
        n = NUMBERED.match(lines[i])
        title = h.group(2) if h else n.group(2) if n else None
        if title is not None and AUTHOR.search(title):
            j = _section(lines, i)
            if any(x.strip() for x in lines[i + 1 : j]):
                sections.append("\n".join(lines[i:j]).rstrip())
            i = j
        else:
            i += 1
    return "\n\n".join(sections) if sections else note.strip()
