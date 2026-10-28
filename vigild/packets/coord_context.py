"""What the coord packet reads out of the repo's docs (ADR-0005 part 1).

Pure functions over text: a feature-map index (one line per row, no
descriptions), the full rows whose files meet the job's paths, and the
`docs/architecture.md` sections that name those paths. The caller reads the
files and applies the per-layer budgets.
"""

from __future__ import annotations

import fnmatch
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field

BACKTICK = re.compile(r"`([^`]+)`")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
# a path-looking token inside a request or a note: has a slash or a file extension
PATH_TOKEN = re.compile(
    r"[A-Za-z0-9_.@~-]+(?:/[A-Za-z0-9_.@~-]+)+/?|[A-Za-z0-9_-]+\.[A-Za-z]{1,5}\b"
)
INDEX_FILES_SHOWN = 1
SHORT_LINE = 400
SENTENCE_MAX = 300


@dataclass
class Row:
    name: str
    status: str
    code: list[str]
    tests: list[str]
    line: str
    desc: str = ""
    files: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.files = self.code + self.tests

    @property
    def first_sentence(self) -> str:
        """The first sentence of the row's description (at most SENTENCE_MAX characters)."""
        text = self.desc.replace("\\|", "|").replace("**", "")
        m = re.search(r"[.!?](?:\s|$)", text)
        first = text[: m.end()].strip() if m else text.strip()
        return first[:SENTENCE_MAX]

    @property
    def short(self) -> str:
        """The row cut down to its status, its files and its first sentence."""
        return (
            f"{self.name} | status: {self.status} | files: {', '.join(self.files)} | "
            f"{self.first_sentence}"
        )


def _expand(token: str) -> list[str]:
    """`a/{b,c}.py` -> [`a/b.py`, `a/c.py`]."""
    parts = re.split(r"(\{[^}]*,[^}]*\})", token)
    choices = [p[1:-1].split(",") if p.startswith("{") and "," in p else [p] for p in parts]
    return ["".join(c) for c in itertools.product(*choices)]


def _paths(cell: str) -> list[str]:
    out: list[str] = []
    for tok in BACKTICK.findall(cell):
        if " " in tok or tok.startswith(("/", "http", "-")):
            continue
        if "/" in tok or re.search(r"\.[a-z]{1,5}$", tok):
            out.extend(_expand(tok.split("::")[0].split(" (")[0]))
    return out


def feature_rows(text: str) -> list[Row]:
    """Every row of every table in the feature map, in order. A cell's `\\|`
    is not a separator."""
    rows: list[Row] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in CELL_SPLIT.split(line.strip().strip("|"))]
        if header is None:
            header = cells
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        col = {h: i for i, h in enumerate(header)}
        code_i, tests_i = col.get("Core code"), col.get("Tests")
        code = _paths(cells[code_i]) if code_i is not None and code_i < len(cells) else []
        tests = _paths(cells[tests_i]) if tests_i is not None and tests_i < len(cells) else []
        desc_i = col.get("What it does")
        desc = cells[desc_i] if desc_i is not None and desc_i < len(cells) else ""
        rows.append(Row(cells[0], cells[-1], code, tests, line, desc))
    return rows


def status_word(status: str) -> str:
    m = re.match(r"[A-Za-z]+", status.strip())
    return m.group(0).lower() if m else "?"


def feature_index(rows: list[Row]) -> str:
    """One line per row: the feature, its status and its files. No descriptions."""
    lines = []
    for r in rows:
        shown = r.code[:INDEX_FILES_SHOWN]
        more = len(r.code) - len(shown)
        files = ", ".join(shown) + (f" +{more}" if more > 0 else "")
        lines.append(f"- {r.name} [{status_word(r.status)}]" + (f": {files}" if files else ""))
    return "\n".join(lines)


def job_paths(
    text: str, exists: Callable[[str], bool], extra: list[str] | None = None
) -> list[str]:
    """The repo paths a text names that exist in the clone, then `extra` (the
    part-5 pre-fill adds its own), each once, in order."""
    found: list[str] = []
    for tok in [*(m.group(0).rstrip("/.") for m in PATH_TOKEN.finditer(text)), *(extra or [])]:
        if tok and tok not in found and exists(tok):
            found.append(tok)
    return found


def _meets(path: str, token: str) -> bool:
    if path == token:
        return True
    if token.endswith("/"):
        return path.startswith(token)
    return "*" in token and fnmatch.fnmatchcase(path, token)


def rows_for(rows: list[Row], paths: list[str]) -> list[Row]:
    """Rows whose files meet a job path. The row that leads with the path (its
    first core file) comes first, then rows with it in their core code, then
    rows that only test it; each group in map order."""
    hit = [r for r in rows if any(_meets(p, f) for p in paths for f in r.files)]

    def rank(r: Row) -> int:
        if r.code and any(_meets(p, r.code[0]) for p in paths):
            return 0
        return 1 if any(_meets(p, f) for p in paths for f in r.code) else 2

    return sorted(hit, key=rank)


def sections(doc: str) -> list[tuple[str, str]]:
    """(heading, whole section) for each `#`..`###` heading, outside code fences."""
    out: list[tuple[str, list[str]]] = []
    in_code = False
    for line in doc.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and re.match(r"#{1,3} ", line):
            out.append((line, [line]))
        elif out:
            out[-1][1].append(line)
    return [(h, "\n".join(body).rstrip()) for h, body in out]


def sections_naming(doc: str, paths: list[str]) -> list[tuple[str, str]]:
    """The architecture sections that name a job path, in document order."""
    return [(h, s) for h, s in sections(doc) if any(p in s for p in paths)]


def lines_naming(section: str, paths: list[str]) -> str:
    """The heading and the lines of a section that name a path: its short form."""
    lines = section.splitlines()
    keep = [
        ln[:SHORT_LINE] + ("…" if len(ln) > SHORT_LINE else "")
        for ln in lines[1:]
        if any(p in ln for p in paths)
    ]
    return "\n".join([lines[0], *keep]) if keep else ""
