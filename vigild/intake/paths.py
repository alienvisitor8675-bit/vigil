"""Two glob semantics, each where its error is the safe one.

**Detection** (`matches`, `matches_any`): money/auth/consent paths. Guard's
and route's semantics, so one money glob means the same thing everywhere:
a glob without `/` matches the basename anywhere, a glob with `/` matches the
path's tail, case-insensitive. Over-matching flags more, which is safe.

**Scope** (`in_scope`, `globs_overlap`): what a worker may change. Strict,
because over-matching here grants more (review round 1, item 14):
anchored at the repo root, case-sensitive, `*` and `?` stay inside one path
segment, `**` matches zero or more whole segments. Scope entries may not be
absolute or contain `..`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

WILD = set("*?[")


def matches(path: str, glob: str) -> bool:
    p = path.strip().removeprefix("./").lower()
    g = glob.strip().lower()
    if "/" not in g:
        return fnmatchcase(p.rsplit("/", 1)[-1], g)
    candidates = [g]
    while candidates[-1].startswith("**/"):
        candidates.append(candidates[-1][3:])  # a leading **/ may match nothing
    return any(fnmatchcase(p, c) or fnmatchcase(p, "*/" + c) for c in candidates)


def matches_any(path: str, globs: Iterable[str]) -> bool:
    return any(matches(path, g) for g in globs)


# --- scope ---------------------------------------------------------------------------


def scope_entry_error(entry: str) -> str | None:
    if not entry or entry != entry.strip():
        return "empty or padded path"
    if entry.startswith(("/", "~")) or "\\" in entry or ":" in entry.split("/")[0]:
        return f"{entry!r} must be a relative path inside the repo"
    if any(seg in ("..", ".", "") for seg in entry.split("/")):
        return f"{entry!r} must be a relative path inside the repo (no '..', '.' or empty parts)"
    return None


def _match_segs(p: list[str], g: list[str]) -> bool:
    if not g:
        return not p
    if g[0] == "**":
        return any(_match_segs(p[i:], g[1:]) for i in range(len(p) + 1))
    return bool(p) and fnmatchcase(p[0], g[0]) and _match_segs(p[1:], g[1:])


def _match_scope(path: str, glob: str) -> bool:
    return path == glob or _match_segs(path.split("/"), glob.split("/"))


def in_scope(path: str, scope: Sequence[str]) -> bool:
    return any(_match_scope(path, s) for s in scope)


def _literal_prefix(glob: str) -> list[str]:
    out: list[str] = []
    for seg in glob.split("/"):
        if WILD & set(seg):
            break
        out.append(seg)
    return out


def globs_overlap(a: str, b: str) -> bool:
    """Whether two scope entries can name the same file. When unsure, True:
    the caller treats an overlap as a conflict, so doubt fails closed."""
    a_lit, b_lit = not (WILD & set(a)), not (WILD & set(b))
    if a_lit and b_lit:
        return a == b
    if a_lit:
        return _match_scope(a, b)
    if b_lit:
        return _match_scope(b, a)
    pa, pb = _literal_prefix(a), _literal_prefix(b)
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]
