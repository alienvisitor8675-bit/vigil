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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

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


# --- phase 2a, item 5: the deps job ----------------------------------------------------------

MIN_AGE_DAYS = 14
PACKAGE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]{0,213}$")
EXACT = re.compile(r"^[0-9][0-9A-Za-z.+!-]{0,63}$")
UV_LOCK, PNPM_LOCK = "uv.lock", "pnpm-lock.yaml"
LOCKFILE_OF = {"pypi": UV_LOCK, "npm": PNPM_LOCK}


def check_requests(lockfiles: list[str], deps: list[DepRequest]) -> list[str]:
    """The deps job's spec gate: each request an exact version of a plain
    package name, in an ecosystem whose lockfile the repo's dev image installs."""
    errors: list[str] = []
    if not deps:
        errors.append("no dependency requested")
    names = {p.rsplit("/", 1)[-1] for p in lockfiles}
    for d in deps:
        if not PACKAGE.match(d.package):
            errors.append(f"{d.package!r} is not a package name")
        if not EXACT.match(d.version):
            errors.append(f"{d.package}: {d.version!r} is not an exact version")
        if LOCKFILE_OF[d.ecosystem] not in names:
            errors.append(f"{d.package}: the repo has no {LOCKFILE_OF[d.ecosystem]} to update")
    return errors


def uv_exclude_newer(lock_text: str | None) -> str | None:
    """The cutoff a uv.lock was resolved with (`[options] exclude-newer`)."""
    try:
        doc = tomllib.loads(lock_text or "")
    except tomllib.TOMLDecodeError:
        return None
    value = (doc.get("options") or {}).get("exclude-newer")
    return str(value) if value else None


def update_command(deps: list[DepRequest], exclude_newer: str | None = None) -> str:
    """The package manager's lockfile-only update, as one shell line. A uv
    lock's own cutoff is passed back, so the lock keeps its options (the 2a
    done-when's reviewer caught `uv add` dropping it)."""
    import shlex

    parts: list[str] = []
    py = [f"{d.package}=={d.version}" for d in deps if d.ecosystem == "pypi"]
    js = [f"{d.package}@{d.version}" for d in deps if d.ecosystem == "npm"]
    if py:
        cutoff = f"--exclude-newer {shlex.quote(exclude_newer)} " if exclude_newer else ""
        parts.append(f"uv add --no-sync {cutoff}" + " ".join(shlex.quote(x) for x in py))
    if js:
        parts.append("pnpm add -w --lockfile-only " + " ".join(shlex.quote(x) for x in js))
    return " && ".join(parts)


def lock_packages(path: str, text: str | None) -> set[tuple[str, str]]:
    """(name, version) for every registry package a lockfile pulls in."""
    name = path.rsplit("/", 1)[-1]
    out: set[tuple[str, str]] = set()
    if not text:
        return out
    if name == UV_LOCK:
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return out
        for pkg in doc.get("package") or []:
            src = pkg.get("source") or {}
            if "registry" in src and pkg.get("name") and pkg.get("version"):
                out.add((str(pkg["name"]).lower(), str(pkg["version"])))
    elif name == PNPM_LOCK:
        import yaml

        try:
            doc2 = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return out
        pkgs = doc2.get("packages") if isinstance(doc2, dict) else None
        for key in pkgs or {}:
            k = str(key).lstrip("/").split("(", 1)[0]
            pkg2, sep, ver = k.rpartition("@")
            if sep and pkg2 and ver:
                out.add((pkg2, ver))
    return out


def _strip_npm(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in NPM_SECTIONS}


def _strip_pyproject(d: dict[str, Any]) -> dict[str, Any]:
    d = json.loads(json.dumps(d))
    proj = d.get("project") or {}
    proj.pop("dependencies", None)
    proj.pop("optional-dependencies", None)
    d.pop("dependency-groups", None)
    return d


def manifest_errors(path: str, before: str | None, after: str | None) -> list[str]:
    """A manifest may change its dependency fields and nothing else."""
    name = path.rsplit("/", 1)[-1]
    if before == after:
        return []
    if name == UV_LOCK:
        # a uv lock may gain packages; its resolution options (the cutoff) stay
        try:
            same = (tomllib.loads(before or "").get("options") or {}) == (
                tomllib.loads(after or "").get("options") or {}
            )
        except tomllib.TOMLDecodeError:
            return [f"{path} isn't TOML"]
        return [] if same else [f"{path}: its resolution options changed"]
    if is_lockfile(path):
        return []
    if name == "package.json":
        try:
            a, b = json.loads(before or "{}"), json.loads(after or "{}")
        except json.JSONDecodeError:
            return [f"{path} isn't JSON"]
        same = _strip_npm(a) == _strip_npm(b)
        return [] if same else [f"{path}: changed outside its dependency fields"]
    if name == "pyproject.toml":
        try:
            a2, b2 = tomllib.loads(before or ""), tomllib.loads(after or "")
        except tomllib.TOMLDecodeError:
            return [f"{path} isn't TOML"]
        same = _strip_pyproject(a2) == _strip_pyproject(b2)
        return [] if same else [f"{path}: changed outside its dependency fields"]
    return [f"{path}: not a manifest or lockfile"]


class Registry(Protocol):
    def published(self, ecosystem: str, name: str, version: str) -> datetime | None: ...


class HttpRegistry:
    """Publish times from the registries themselves (PyPI's JSON API, the npm
    registry's packument), read by vigild on the host."""

    def __init__(self, http: Any) -> None:
        self.http = http

    def published(self, ecosystem: str, name: str, version: str) -> datetime | None:
        if ecosystem == "pypi":
            r = self.http.get(f"https://pypi.org/pypi/{name}/{version}/json", timeout=20.0)
            if r.status_code != 200:
                return None
            times = [u.get("upload_time_iso_8601") for u in r.json().get("urls") or []]
            stamps = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times if t]
            return min(stamps) if stamps else None
        r = self.http.get(f"https://registry.npmjs.org/{name}", timeout=20.0)
        if r.status_code != 200:
            return None
        t = (r.json().get("time") or {}).get(version)
        return datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None


def too_new(added: set[tuple[str, str, str]], registry: Registry, now: datetime) -> list[str]:
    """Every (ecosystem, name, version) the lockfile newly pulls in must be at
    least 14 days old by its registry's publish time; the ones that aren't,
    or whose time can't be read, as a list."""
    out = []
    for eco, name, ver in sorted(added):
        try:
            when = registry.published(eco, name, ver)
        except Exception as e:  # the registry is down: no proof of age
            out.append(f"{name}=={ver}: publish time unreadable ({type(e).__name__})")
            continue
        if when is None:
            out.append(f"{name}=={ver}: no publish time")
        elif now - when < timedelta(days=MIN_AGE_DAYS):
            out.append(f"{name}=={ver}: published {when.date()}, under {MIN_AGE_DAYS} days ago")
    return out


@dataclass
class DepsResult:
    files: dict[str, bytes] = field(default_factory=dict)  # changed manifests and lockfiles
    errors: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)  # name==version newly in a lockfile
    output: str = ""


Container = Callable[[str, Path, str], tuple[int, str]]  # (image, work dir, command)


class DepsExecutor:
    """No model (B39): the repo's manifests and lockfiles from the base, the
    package manager's lockfile-only update in a build container with registry
    egress and nothing else, then the checks. The work dir has no `.git` and
    nothing of the repo but those files; vigild reads them back as regular
    files only and commits them itself, in its own clone."""

    def __init__(
        self,
        workspace: Any,
        container: Container,
        registry: Registry,
        clock: Callable[[], datetime],
        root: Path,
    ) -> None:
        self.workspace = workspace
        self.container = container
        self.registry = registry
        self.clock = clock
        self.root = root

    def run(
        self, repo: str, base_sha: str, lockfiles: list[str], deps: list[DepRequest], image: str
    ) -> DepsResult:
        import secrets
        import shutil

        from vigild.safefile import UnsafeFile, safe_read_bytes

        work = self.root / f"deps-{secrets.token_hex(6)}"
        work.mkdir(parents=True)
        work.chmod(0o777)  # the build container's uid writes here
        try:
            self.workspace.export_files(repo, base_sha, lockfiles, work)
            before = {p: (work / p).read_bytes() for p in lockfiles if (work / p).is_file()}
            for p in work.rglob("*"):
                p.chmod(0o777 if p.is_dir() else 0o666)
            cutoff = next(
                (
                    uv_exclude_newer(before[p].decode(errors="replace"))
                    for p in before
                    if p.rsplit("/", 1)[-1] == UV_LOCK
                ),
                None,
            )
            code, output = self.container(image, work, update_command(deps, cutoff))
            res = DepsResult(output=output[-4000:])
            if code != 0:
                res.errors.append(f"the package manager failed (exit {code})")
                return res
            others = sorted(
                str(p.relative_to(work))
                for p in work.rglob("*")
                if p.is_file() and str(p.relative_to(work)) not in lockfiles
            )
            if others:
                res.errors.append(f"files other than manifests and lockfiles: {others[:20]}")
            for path in lockfiles:
                try:
                    data = safe_read_bytes(work, work / path, 64 << 20)
                except FileNotFoundError:
                    continue
                except (OSError, UnsafeFile) as e:
                    res.errors.append(f"{path}: {e}")
                    continue
                if before.get(path) != data:
                    res.files[path] = data
            if not res.files:
                res.errors.append("the update changed nothing")
            for path, data in res.files.items():
                old = before.get(path, b"").decode(errors="replace")
                res.errors += manifest_errors(path, old or None, data.decode(errors="replace"))
            added: set[tuple[str, str, str]] = set()
            for eco, lock in LOCKFILE_OF.items():
                for path in [p for p in lockfiles if p.rsplit("/", 1)[-1] == lock]:
                    if path in res.files:
                        new = lock_packages(path, res.files[path].decode(errors="replace"))
                        old_set = lock_packages(
                            path, before.get(path, b"").decode(errors="replace")
                        )
                        added |= {(eco, n, v) for n, v in new - old_set}
            res.added = sorted(f"{n}=={v}" for _, n, v in added)
            res.errors += too_new(added, self.registry, self.clock())
            return res
        finally:
            shutil.rmtree(work, ignore_errors=True)
