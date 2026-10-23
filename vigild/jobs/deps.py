"""Dependency changes (owner direction, round 3).

A worker can't add a dependency: its dev image is built from the base's
lockfiles and the verifier runs the base's runner files. When it needs one, it
says so in worker_report.json (`needs_deps`), or vigild reads it from a
manifest it changed. The task then blocks (`needs_deps`) instead of being
verified, and its job blocks with the requested dependencies and the worker's
manifest and lockfile diff (fenced as untrusted) in the inbox.

Phase 2 adds the deps job itself: no model, the package manager's
lockfile-only update in a build container with registry egress and nothing
else, the 14-day rule on every new package, the dev image rebuilt from the new
lockfile, the full suite offline, and its own PR (shape `deps`, always the
owner's review). When it merges, the blocked task resumes on the new image.
Until then the owner resumes it from the inbox; the FSM refuses while the deps
job it is `blocked_on` hasn't merged.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from vigild.contract.models import DepRequest

NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def _npm(text: str) -> dict[str, str]:
    try:
        doc = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for sec in NPM_SECTIONS:
        deps = doc.get(sec) if isinstance(doc, dict) else None
        if isinstance(deps, dict):
            out.update({str(k): str(v) for k, v in deps.items()})
    return out


def _pypi(text: str) -> dict[str, str]:
    try:
        doc: dict[str, Any] = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError:
        return {}
    project = doc.get("project") or {}
    reqs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        reqs += list(group)
    for group in (doc.get("dependency-groups") or {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    out: dict[str, str] = {}
    for r in reqs:
        m = PEP508_NAME.match(str(r))
        if m:
            out[m.group(1).lower()] = m.group(3).split(";")[0].strip()
    return out


def is_lockfile(path: str) -> bool:
    """A lockfile proper (not a manifest, a workspace file or a version pin)."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith((".lock", "-lock.yaml", "-lock.json")) or name in ("yarn.lock",)


def infer_deps(path: str, base_text: str | None, head_text: str | None) -> list[DepRequest]:
    """New or changed dependencies in one manifest, base against head."""
    name = path.rsplit("/", 1)[-1]
    if name == "package.json":
        eco, before, after = "npm", _npm(base_text or ""), _npm(head_text or "")
    elif name == "pyproject.toml":
        eco, before, after = "pypi", _pypi(base_text or ""), _pypi(head_text or "")
    else:
        return []
    return [
        DepRequest(ecosystem=eco, package=pkg, version=ver or "*", why="read from the diff")  # type: ignore[arg-type]
        for pkg, ver in sorted(after.items())
        if before.get(pkg) != ver
    ]
