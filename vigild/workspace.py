"""Mirrors, per-task clones, and the vault (B3, B25).

- **Mirror:** a bare mirror per repo under <data>/mirrors, fetched with a
  read-scoped App token when one is configured, never with ambient host
  credentials, and updated by one fetch at a time per repo.
- **Clone:** each session gets `git clone --no-hardlinks` of the mirror under
  <data>/clones/<task_id> (copied objects, so a worker can't corrupt the
  mirror through a shared inode). vigild records the base commit it cloned
  at, outside the clone.
- **Vault:** a bare repo per repo under <data>/vault that only vigild writes.
  After a session the worker's result comes back as a git bundle written by
  the harness. vigild copies it out with `safe_read_bytes`, fetches only the
  session's branch into `refs/vigil/<task_id>`, checks the base it recorded
  is an ancestor, and does every diff, artifact read and push from the vault.
  Host git never runs in the worker's clone after the session starts.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vigild.gitsafe import GitError, git, git_bytes
from vigild.repos import is_bench_repo
from vigild.safefile import UnsafeFile, safe_read_bytes

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BRANCH = re.compile(r"^vigil/[A-Za-z0-9._/-]+$")
BRANCH_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
RO_DIRS = (".github", ".claude", ".opencode", ".git/hooks")
RO_FILES = (".git/config",)
VIGIL_DIR = ".vigil"
BOT_NAME = "vigil-worker[bot]"
BOT_EMAIL = "vigil-worker[bot]@users.noreply.github.com"
MAX_BUNDLE = 512 << 20
MAX_BLOB = 1 << 20
TokenFor = Callable[[str], AbstractContextManager[str | None]]


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    read_only: bool


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _no_token(repo: str) -> AbstractContextManager[str | None]:
    return nullcontext(None)


class Workspace:
    def __init__(
        self,
        data_dir: Path,
        remote_url: Callable[[str], str] = github_url,
        token_for: TokenFor = _no_token,
    ) -> None:
        self.data_dir = data_dir
        self.remote_url = remote_url
        self.token_for = token_for
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def _repo_lock(self, repo: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault(repo, threading.Lock())
        with lock:
            yield

    def _git(self, cwd: Path, *args: str, token: str | None = None) -> str:
        try:
            return git(cwd, *args, token=token)
        except GitError as e:
            raise WorkspaceError(str(e)) from e

    # --- mirrors and clones ----------------------------------------------------------

    def _mirror_path(self, repo: str) -> Path:
        owner, name = repo.split("/")
        return self.data_dir / "mirrors" / owner / f"{name}.git"

    def mirror(self, repo: str) -> Path:
        with self._repo_lock(repo):
            return self._update_mirror(repo)

    def _update_mirror(self, repo: str) -> Path:
        """Fetch (or make) the mirror; the caller holds the repo's lock."""
        path = self._mirror_path(repo)
        if is_bench_repo(repo):
            # a bench repo is not on GitHub: its mirror is imported from the task image
            # (bench slice 1b) and never fetched, so no token is asked for and no
            # remote is contacted
            if not path.exists():
                raise WorkspaceError(f"bench repo {repo} has not been imported; nothing to fetch")
            return path
        with self.token_for(repo) as token:
            if path.exists():
                self._git(path, "remote", "update", "--prune", token=token)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._git(
                    path.parent, "clone", "--mirror", "--quiet", self.remote_url(repo), str(path),
                    token=token,
                )  # fmt: skip
        return path

    def clone(
        self,
        repo: str,
        task_id: str,
        *,
        branch: str,
        base: str,
        start: str | None = None,
        record_as: str | None = None,
    ) -> Path:
        """A fresh clone for one session. `start` (a commit in the vault) puts
        the branch there instead of at the base's tip, for a fix round, a
        resumed worker or a reviewer (phase 2a); the base recorded is still
        the base's, unless `record_as` names the base commit to record."""
        if not ID.match(task_id):
            raise WorkspaceError(f"bad task id {task_id!r}")
        if not BRANCH.match(branch) or ".." in branch:
            raise WorkspaceError(f"branch {branch!r} must be vigil/*")
        dest = self.data_dir / "clones" / task_id
        # one lock for the fetch and the copy: no fetch writes the mirror while
        # a clone copies its objects (round 5)
        with self._repo_lock(repo):
            mirror = self._update_mirror(repo)
            base_sha = self._git(mirror, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                dest.parent, "clone", "--no-hardlinks", "--quiet", "--branch", base,
                str(mirror), str(dest),
            )  # fmt: skip
        self._git(dest, "checkout", "--quiet", "--detach", base_sha)
        if start is not None:
            if not SHA.match(start):
                raise WorkspaceError("bad start sha")
            vault = self.vault(repo)
            with self._repo_lock(f"vault:{repo}"):
                self._git(
                    dest, "-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "--quiet",
                    "--no-tags", str(vault), start,
                )  # fmt: skip
            if record_as is not None:
                if not SHA.match(record_as):
                    raise WorkspaceError("bad base sha")
                base_sha = record_as  # the task's own base, not the branch's tip today
            self._git(dest, "merge-base", "--is-ancestor", base_sha, start)
            self._git(dest, "checkout", "--quiet", "-b", branch, start)
        else:
            self._git(dest, "checkout", "--quiet", "-b", branch)
        self._git(dest, "remote", "remove", "origin")
        self._git(dest, "config", "user.name", BOT_NAME)
        self._git(dest, "config", "user.email", BOT_EMAIL)
        exclude = dest / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n/.vigil/task.json\n")
        rec = self.data_dir / "clones" / f"{task_id}.base"
        rec.write_text(json.dumps({"repo": repo, "base": base, "base_sha": base_sha}))
        return dest

    def base_sha(self, task_id: str) -> str:
        """The base commit vigild cloned at, read from vigild's own record."""
        rec = self.data_dir / "clones" / f"{task_id}.base"
        sha = str(json.loads(rec.read_text())["base_sha"])
        if not SHA.match(sha):
            raise WorkspaceError(f"bad base record for {task_id}")
        return sha

    def commit_before_session(self, clone: Path, paths: list[str], message: str) -> str:
        """vigild's own commit in a clone, before any session has touched it;
        returns its sha, the point a worker's own changes are counted from."""
        self._git(clone, "add", "--", *paths)
        self._git(clone, "commit", "-q", "--no-verify", "-m", message)
        return self._git(clone, "rev-parse", "HEAD")

    def write_task(self, clone: Path, contract: dict[str, Any]) -> Path:
        path = clone / ".vigil" / "task.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
        return path

    def overlays(self, clone: Path) -> list[Mount]:
        """Read-only mounts over /work (B18). Missing dirs are created empty so
        the agent can't create them either."""
        mounts: list[Mount] = []
        for d in RO_DIRS:
            p = clone / d
            p.mkdir(parents=True, exist_ok=True)
            mounts.append(Mount(str(p), f"/work/{d}", read_only=True))
        for f in RO_FILES:
            mounts.append(Mount(str(clone / f), f"/work/{f}", read_only=True))
        return mounts

    def remove(self, clone: Path) -> None:
        root = (self.data_dir / "clones").resolve()
        if clone.resolve().parent != root:
            raise WorkspaceError(f"{clone} is not a task clone")
        shutil.rmtree(clone)

    # --- the vault ---------------------------------------------------------------------

    def vault(self, repo: str) -> Path:
        owner, name = repo.split("/")
        path = self.data_dir / "vault" / owner / f"{name}.git"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._git(path.parent, "init", "--quiet", "--bare", str(path))
        return path

    def import_result(
        self,
        repo: str,
        task_id: str,
        out_dir: Path,
        branch: str,
        base_sha: str,
        *,
        head: str,
        checkpoint: bool = False,
    ) -> str:
        """Take the session's branch from the bundle the harness wrote; return its head.

        `head` is the commit the harness named on its result line (PID 1's
        stdout, out of the agent's reach). The bundle sits in a directory the
        agent uid can write, so a tip that isn't `head` is refused (review round 2, N1).
        `checkpoint` (phase 2a, item 3) takes a running worker's checkpoint
        instead: `checkpoint.bundle`'s `refs/vigil/checkpoint`, into
        `refs/vigil/<task>-ckpt`, bound the same way to the commit named on
        its checkpoint line.
        """
        if not SHA.match(base_sha):
            raise WorkspaceError("bad base sha")
        if not SHA.match(head or ""):
            raise WorkspaceError("the harness named no head commit")
        try:
            name = "checkpoint.bundle" if checkpoint else "result.bundle"
            data = safe_read_bytes(out_dir, out_dir / name, MAX_BUNDLE)
        except (OSError, UnsafeFile) as e:
            raise WorkspaceError(f"no usable result bundle: {e}") from e
        vault = self.vault(repo)
        with self._repo_lock(f"vault:{repo}"):
            mirror = self._mirror_path(repo)
            self._git(
                vault, "fetch", "--quiet", "--no-tags", str(mirror), "+refs/heads/*:refs/mirror/*"
            )
            tmp = self.data_dir / "vault" / "incoming" / f"{task_id}-{secrets.token_hex(4)}.bundle"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            src = "refs/vigil/checkpoint" if checkpoint else f"refs/heads/{branch}"
            ref = f"refs/vigil/{task_id}-ckpt" if checkpoint else f"refs/vigil/{task_id}"
            try:
                self._git(vault, "bundle", "verify", "--quiet", str(tmp))
                self._git(vault, "fetch", "--quiet", "--no-tags", str(tmp), f"+{src}:{ref}")
            finally:
                tmp.unlink(missing_ok=True)
            tip = self._git(vault, "rev-parse", "--verify", f"{ref}^{{commit}}")
            if tip != head:
                self._git(vault, "update-ref", "-d", ref)
                raise WorkspaceError(f"the bundle's {branch} is not the head the harness named")
            try:
                self._git(vault, "merge-base", "--is-ancestor", base_sha, head)
            except WorkspaceError as e:
                raise WorkspaceError(
                    f"{branch} does not descend from the base vigild recorded"
                ) from e
        return head

    def verify_checkout(
        self,
        repo: str,
        name: str,
        sha: str,
        *,
        take: list[tuple[str, list[str]]] | None = None,
        put: list[tuple[str, str]] | None = None,
        source: str = "vault",
    ) -> Path:
        """A fresh checkout of `sha` for the verifier, under
        <data>/verify/<name>/src: a new .git with hooks off, nothing from the
        worker's clone. `take` lays files over it: for each (commit, paths),
        each path as it is at that commit, or removed if it isn't there.
        `put` writes vigild's own files (path, text) last.
        `source` is the vault (a worker's result) or the mirror (a base commit,
        for validating a dev image)."""
        take = take or []
        if not ID.match(name) or not SHA.match(sha):
            raise WorkspaceError(f"bad verify checkout {name!r} {sha!r}")
        if any(not SHA.match(c) for c, _ in take):
            raise WorkspaceError("bad overlay sha")
        if source not in ("vault", "mirror"):
            raise WorkspaceError(f"bad source {source!r}")
        origin = self.vault(repo) if source == "vault" else self._mirror_path(repo)
        # a new directory every time: Docker Desktop's file sharing can refuse a
        # bind mount of a path that was just removed and made again
        root = self.data_dir / "verify" / f"{name}-{secrets.token_hex(4)}"
        src = root / "src"
        src.parent.mkdir(parents=True)
        self._git(src.parent, "init", "--quiet", str(src))
        self._git(src, "config", "core.hooksPath", "/dev/null")
        self._git(src, "config", "core.fsmonitor", "false")
        wants = sorted({sha, *(c for c, _ in take)})
        with self._repo_lock(f"vault:{repo}" if source == "vault" else repo):
            self._git(
                src, "-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "--quiet",
                "--no-tags", str(origin), *wants,
            )  # fmt: skip
        self._git(src, "checkout", "--quiet", "--detach", sha)
        for commit, paths in take:
            if not paths:
                continue
            listed = self._git(src, "ls-tree", "-r", "-z", "--name-only", commit, "--", *paths)
            there = set(filter(None, listed.split("\0")))
            present = [p for p in paths if p in there]
            absent = [p for p in paths if p not in there]
            if present:
                self._git(src, "checkout", "--quiet", commit, "--", *present)
            if absent:
                self._git(src, "rm", "-q", "-f", "--ignore-unmatch", "--", *absent)
        for rel, text in put or []:
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        return src

    def keep_hidden_output(self, name: str, text: str) -> Path:
        """The full output of a hidden-test run, kept on the host (mode 600) in
        <data>/hidden-runs/: it can carry the tests' names and source lines, so
        it never goes into the DB, an artifact, the API or a PR."""
        if not ID.match(name):
            raise WorkspaceError(f"bad hidden run name {name!r}")
        d = self.data_dir / "hidden-runs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.txt"
        path.write_text(text)
        path.chmod(0o600)
        return path

    def export_tree(self, repo: str, sha: str, dest: Path) -> Path:
        """The tree at `sha`, from a freshly fetched mirror, unpacked at `dest`
        (claude-mods for the executor layer's build context)."""
        import io
        import tarfile

        if not SHA.match(sha):
            raise WorkspaceError("bad sha")
        if dest.exists():
            return dest
        mirror = self.mirror(repo)
        try:
            data = git_bytes(mirror, "archive", "--format=tar", sha)
        except GitError as e:
            raise WorkspaceError(str(e)) from e
        tmp = dest.with_name(dest.name + ".partial")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(tmp, filter="data")
        tmp.rename(dest)
        return dest

    def mirror_file(self, repo: str, branch: str, path: str) -> str | None:
        """A regular file at the tip of `branch` in the mirror as last fetched,
        or None (no mirror yet, no such file, not a regular file)."""
        mirror = self._mirror_path(repo)
        if not mirror.exists() or not BRANCH_NAME.match(branch):
            return None
        try:
            sha = self._git(mirror, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
            entries = self._tree_entries(repo, sha, [path])
            if path not in entries:
                return None
            return git_bytes(mirror, "cat-file", "blob", entries[path][1]).decode(errors="replace")
        except WorkspaceError, GitError:
            return None

    def mirror_is_dir(self, repo: str, branch: str, path: str) -> bool:
        """`path` is a directory at the tip of `branch` in the mirror as last
        fetched (`ls-tree` reads git objects only; False if unknown)."""
        mirror = self._mirror_path(repo)
        if not mirror.exists() or not BRANCH_NAME.match(branch):
            return False
        try:
            sha = self._git(mirror, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
            out = self._git(mirror, "ls-tree", "-z", sha, "--", path)
        except WorkspaceError, GitError:
            return False
        return any(e.partition("\t")[0].split()[1:2] == ["tree"] for e in out.split("\0") if e)

    def mirror_file_at(self, repo: str, sha: str, path: str) -> str | None:
        """A regular file at commit `sha` in the mirror, or None (no mirror, no such file)."""
        mirror = self._mirror_path(repo)
        if not mirror.exists() or not SHA.match(sha):
            return None
        try:
            entries = self._tree_entries(repo, sha, [path])
            if path not in entries:
                return None
            return git_bytes(mirror, "cat-file", "blob", entries[path][1]).decode(errors="replace")
        except WorkspaceError, GitError:
            return None

    def mirror_is_dir_at(self, repo: str, sha: str, path: str) -> bool:
        """`path` is a directory at commit `sha` in the mirror (False if unknown)."""
        mirror = self._mirror_path(repo)
        if not mirror.exists() or not SHA.match(sha):
            return False
        try:
            out = self._git(mirror, "ls-tree", "-z", sha, "--", path)
        except WorkspaceError, GitError:
            return False
        return any(e.partition("\t")[0].split()[1:2] == ["tree"] for e in out.split("\0") if e)

    def tree_paths(self, repo: str, sha: str) -> set[str]:
        """Every file path at `sha` in the vault."""
        if not SHA.match(sha):
            raise WorkspaceError("bad sha")
        out = self._git(self.vault(repo), "ls-tree", "-r", "-z", "--name-only", sha)
        return set(filter(None, out.split("\0")))

    def head_sha(self, repo: str, branch: str) -> str:
        """The tip of `branch` in a freshly fetched mirror."""
        mirror = self.mirror(repo)
        return self._git(mirror, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")

    def _origin(self, repo: str, source: str) -> Path:
        if source not in ("vault", "mirror"):
            raise WorkspaceError(f"bad source {source!r}")
        return self.vault(repo) if source == "vault" else self._mirror_path(repo)

    def _tree_entries(
        self, repo: str, sha: str, paths: list[str], source: str = "mirror"
    ) -> dict[str, tuple[str, str]]:
        if not SHA.match(sha):
            raise WorkspaceError("bad sha")
        out = self._git(self._origin(repo, source), "ls-tree", "-z", sha, "--", *paths)
        entries: dict[str, tuple[str, str]] = {}
        for entry in filter(None, out.split("\0")):
            meta, _, name = entry.partition("\t")
            mode, kind, blob = meta.split()
            if kind == "blob" and mode in ("100644", "100755"):
                entries[name] = (mode, blob)
        return entries

    def blob_ids(
        self, repo: str, sha: str, paths: list[str], source: str = "mirror"
    ) -> dict[str, str | None]:
        """Each path's blob id at `sha` (in the mirror, or the vault for a deps
        job's head); None if absent or not a regular file."""
        entries = self._tree_entries(repo, sha, paths, source)
        return {p: (entries[p][1] if p in entries else None) for p in paths}

    def export_files(
        self, repo: str, sha: str, paths: list[str], dest: Path, source: str = "mirror"
    ) -> None:
        """Write the regular files among `paths` at `sha` under `dest` (a dev
        image's build context, or a deps job's work dir: lockfiles and
        manifests only, never the source)."""
        mirror = self._origin(repo, source)
        for path, (mode, blob) in self._tree_entries(repo, sha, paths, source).items():
            if path.startswith("/") or ".." in Path(path).parts:
                raise WorkspaceError(f"bad path {path!r}")
            size = int(self._git(mirror, "cat-file", "-s", blob))
            if size > MAX_BUNDLE:
                raise WorkspaceError(f"{path} is {size} bytes")
            try:
                data = git_bytes(mirror, "cat-file", "blob", blob)
            except GitError as e:
                raise WorkspaceError(str(e)) from e
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if mode == "100755" else 0o644)

    def changed(self, repo: str, base_sha: str, head: str) -> list[tuple[str, str]]:
        """(status, path) for every file changed; renames show as delete + add."""
        out = self._git(
            self.vault(repo), "diff", "--no-renames", "--no-ext-diff", "--no-textconv",
            "--name-status", "-z", base_sha, head,
        )  # fmt: skip
        parts = out.split("\0")
        return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]

    def is_ancestor(self, repo: str, ancestor: str, head: str) -> bool:
        """Is `ancestor` in `head`'s history, in the vault?"""
        if not (SHA.match(ancestor) and SHA.match(head)):
            return False
        try:
            self._git(self.vault(repo), "merge-base", "--is-ancestor", ancestor, head)
        except WorkspaceError:
            return False
        return True

    def diff(self, repo: str, base_sha: str, head: str, path: str) -> str:
        return self._git(
            self.vault(repo), "diff", "--no-renames", "--no-ext-diff", "--no-textconv",
            base_sha, head, "--", path,
        )  # fmt: skip

    def commit_files(self, clone: Path, task_id: str, files: dict[str, bytes], message: str) -> str:
        """vigild's own commit in a clone no session has touched (a deps job's
        manifests and lockfiles, plan 2a item 5), taken into the vault as
        `refs/vigil/<task>`. Returns the head."""
        for path in files:
            if path.startswith("/") or ".." in Path(path).parts:
                raise WorkspaceError(f"bad path {path!r}")
        for path, data in files.items():
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.commit_before_session(clone, sorted(files), message)
        branch = self._git(clone, "symbolic-ref", "--short", "HEAD")
        head = self._git(clone, "rev-parse", "HEAD")
        repo = json.loads((self.data_dir / "clones" / f"{task_id}.base").read_text())["repo"]
        vault = self.vault(repo)
        with self._repo_lock(f"vault:{repo}"):
            self._git(
                vault, "fetch", "--quiet", "--no-tags", str(self._mirror_path(repo)),
                "+refs/heads/*:refs/mirror/*",
            )  # fmt: skip
            self._git(
                vault, "fetch", "--quiet", "--no-tags", str(clone),
                f"+refs/heads/{branch}:refs/vigil/{task_id}",
            )  # fmt: skip
        return head

    def overlay(
        self, clone: Path, repo: str, base_sha: str, head: str, *, skip: set[str]
    ) -> list[str]:
        """Lay a worker's changes from the vault (base to head) over a fresh
        clone, uncommitted: regular files written, deletions made; paths in
        `skip` (manifests and lockfiles) and under `.vigil/` left as the clone
        has them. A
        committed symlink reads as nothing and isn't laid. Returns the paths."""
        laid = []
        for status, path in self.changed(repo, base_sha, head):
            if path in skip or path.startswith(("/", ".vigil/")) or ".." in Path(path).parts:
                continue  # vigild's artifacts and the old report aren't the work
            target = clone / path
            if status == "D":
                target.unlink(missing_ok=True)
                laid.append(path)
                continue
            text = self.read_blob(repo, head, path)
            if text is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            laid.append(path)
        return laid

    def full_diff(self, repo: str, base_sha: str, head: str) -> str:
        """The whole change, base to head, from the vault (what a reviewer reads)."""
        if not SHA.match(base_sha) or not SHA.match(head):
            raise WorkspaceError("bad sha")
        return self._git(
            self.vault(repo), "diff", "--no-renames", "--no-ext-diff", "--no-textconv",
            base_sha, head,
        )  # fmt: skip

    def read_blob(self, repo: str, head: str, path: str, max_bytes: int = MAX_BLOB) -> str | None:
        """A regular file's content at `head`; None for a symlink, a submodule,
        a missing path or one over the cap."""
        vault = self.vault(repo)
        entry = self._git(vault, "ls-tree", "-z", "--long", head, "--", path).rstrip("\0")
        if not entry:
            return None
        meta, _, name = entry.partition("\t")
        mode, kind, sha, size = meta.split()
        if name != path or kind != "blob" or mode not in ("100644", "100755"):
            return None
        if int(size) > max_bytes:
            return None
        try:
            return git(vault, "cat-file", "blob", sha, raw=True)
        except GitError as e:
            raise WorkspaceError(str(e)) from e

    def push(self, repo: str, head: str, branch: str, token: str | None) -> None:
        if is_bench_repo(repo):
            raise WorkspaceError(f"{repo} is a bench repo: nothing is pushed")
        self._git(
            self.vault(repo), "push", "--quiet", "--no-verify", self.remote_url(repo),
            f"{head}:refs/heads/{branch}", token=token,
        )  # fmt: skip

    # --- what reaches the target repo (Tier 1: `.vigil/` stays in the vault) ---------------

    def _vigil_entries(self, repo: str, sha: str) -> list[str]:
        """`sha`'s top-level `.vigil` tree entries (any case, any kind), as ls-tree
        prints them."""
        if not SHA.match(sha):
            raise WorkspaceError("bad sha")
        out = self._git(self.vault(repo), "ls-tree", "-z", sha)
        return sorted(e for e in out.split("\0") if e and e.partition("\t")[2].lower() == VIGIL_DIR)

    def has_vigil(self, repo: str, sha: str, base_sha: str) -> bool:
        """Does `sha`'s top-level `.vigil` differ from the base's? A repo's own
        tracked `.vigil` is not the job's, so it does not count."""
        return self._vigil_entries(repo, sha) != self._vigil_entries(repo, base_sha)

    def _pub_ref(self, branch: str) -> str:
        return "refs/vigil/pub/" + hashlib.sha256(branch.encode()).hexdigest()[:24]

    def published_sha(self, repo: str, branch: str) -> str | None:
        """The commit vigild last sent to `branch`, from its own record."""
        try:
            return self._git(
                self.vault(repo), "rev-parse", "--verify", "--quiet",
                f"{self._pub_ref(branch)}^{{commit}}",
            )  # fmt: skip
        except WorkspaceError:
            return None

    def record_published(self, repo: str, branch: str, sha: str) -> None:
        if not SHA.match(sha):
            raise WorkspaceError("bad sha")
        self._git(self.vault(repo), "update-ref", self._pub_ref(branch), sha)

    def public_commit(self, repo: str, head: str, base_sha: str, branch: str) -> str:
        """The commit to send for the vault's `head`: `head` itself when its
        top-level `.vigil` is the base's and nothing went to `branch` before;
        otherwise a commit of the same tree with that entry replaced by the base's
        (removed if the base has none), on the last commit sent to `branch` (or the
        base), so the branch only moves forward and never carries the job's
        artifacts. It keeps the head commit's author, committer, dates and message,
        so the same inputs give the same commit. The vault keeps `head` whole."""
        if not (SHA.match(head) and SHA.match(base_sha)):
            raise WorkspaceError("bad sha")
        vault = self.vault(repo)
        prev = self.published_sha(repo, branch)
        if prev is None and not self.has_vigil(repo, head, base_sha):
            return head
        entries = [
            e
            for e in self._git(vault, "ls-tree", "-z", head).split("\0")
            if e and e.partition("\t")[2].lower() != VIGIL_DIR
        ] + self._vigil_entries(repo, base_sha)
        try:
            tree = git(vault, "mktree", "-z", "--missing", stdin="\0".join(entries) + "\0")
        except GitError as e:
            raise WorkspaceError(str(e)) from e
        parent = prev or base_sha
        if self._git(vault, "rev-parse", f"{parent}^{{tree}}") == tree:
            return parent
        meta = self._git(
            vault,
            "log",
            "-1",
            "--date=raw",
            "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd",
            head,
        ).split("\0")
        if len(meta) != 6:
            raise WorkspaceError("unreadable head commit")
        env = dict(zip(
            ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
             "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"),
            meta, strict=True,
        ))  # fmt: skip
        try:
            message = git(vault, "log", "-1", "--format=%B", head, raw=True)
            return git(vault, "commit-tree", tree, "-p", parent, stdin=message, env=env)
        except GitError as e:
            raise WorkspaceError(str(e)) from e
