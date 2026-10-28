"""Publish a worker's branch: push it and open its draft PR, from the host.

P2 (owner, 2026-10-22): workers never hold a GitHub token. After a worker
session ends, vigild has imported its branch into the vault
(`Workspace.import_result`). Here it checks the branch is `vigil/*` and that
every file changed since the base commit vigild recorded (renames counted as
delete + add) is in the contract's scope or the job's own `.vigil/`
artifacts, then pushes the commit from the vault with a one-hour
installation token and opens a draft PR. A draft or nothing.

Tier 1 (owner, 2026-10-26): `.vigil/<job>/` stays in the vault. What goes out
is the checked tree without `.vigil/` (`Workspace.public_commit`), on the last
commit sent to the branch; the vault keeps the worker's full head. vigild only
fast-forwards: a branch sent before this (no record, `.vigil/` on it) is refused
(`LegacyBranch`) and left to the owner.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import httpx2

from vigild.intake.paths import in_scope

if TYPE_CHECKING:
    from vigild.workspace import Workspace

BRANCH = re.compile(r"^vigil/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)*$")
PROTECTED = {"main", "master"}


class PublishError(RuntimeError):
    pass


class LegacyBranch(PublishError):
    """The PR's branch predates artifact stripping: it is on the remote with
    `.vigil/` and vigild has no record of sending it. Only the owner can fix it."""


class Pulls(Protocol):
    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> str: ...


class RestPulls:
    """GitHub's REST API, authenticated with an installation token."""

    def __init__(
        self, http: httpx2.Client, token: str, api: str = "https://api.github.com"
    ) -> None:
        self.http = http
        self._token = token
        self.api = api.rstrip("/")

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> str:
        r = self.http.post(
            f"{self.api}/repos/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
            timeout=30.0,
        )
        if r.status_code != 201:
            is_json = r.headers.get("content-type", "").startswith("application/json")
            msg = r.json().get("message", "") if is_json else r.text[:200]
            raise PublishError(f"{r.status_code}: {msg}")
        url: str = r.json()["html_url"]
        return url

    def close(self, repo: str, number: int) -> None:
        """Close a PR vigild opened (a deps PR its review failed; owner, 2026-10-23)."""
        r = self.http.patch(
            f"{self.api}/repos/{repo}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"state": "closed"},
            timeout=30.0,
        )
        if r.status_code != 200:
            raise PublishError(f"closing {repo}#{number} failed (HTTP {r.status_code})")

    def state(self, repo: str, number: int) -> str:
        """A PR's state: open, merged or closed (vigild's PR poll, plan 2a item 5)."""
        r = self.http.get(
            f"{self.api}/repos/{repo}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise PublishError(f"reading {repo}#{number} failed (HTTP {r.status_code})")
        doc = r.json()
        if doc.get("merged"):
            return "merged"
        return "closed" if doc.get("state") == "closed" else "open"


def artifact_paths(job_id: str, task_id: str) -> list[str]:
    """The only places under `.vigil/` a worker may write: its own task's
    folder and the task contract (#14 line 992). Its job's other paths, other
    tasks' folders and the playbook paths are out of scope like any file."""
    return [f".vigil/{job_id}/{task_id}/**", ".vigil/task.json"]


def vigild_paths(job_id: str) -> list[str]:
    """What vigild itself commits to a worker's branch before the session (the
    spec, plan and approval). They go in the PR; a worker's edit to one is
    refused by acceptance, which counts changes from after that commit."""
    return [f".vigil/{job_id}/{n}" for n in ("spec.md", "plan.md", "approval.json")]


def out_of_scope(changed: Sequence[str], scope: Sequence[str]) -> list[str]:
    """Changed files outside `scope`, which the caller extends with the task's
    own `artifact_paths`; there is no blanket exemption for `.vigil/`."""
    return [f for f in changed if not in_scope(f, scope)]


def publish(
    ws: Workspace,
    *,
    repo: str,
    head: str,
    base_sha: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    pulls: Pulls,
    scope: Sequence[str],
    token: str | None = None,
    existing: str | None = None,
    push: bool = True,
) -> str:
    """Check, push `head` to `branch`, and open a draft PR into `base`. With
    `existing` (a fix round on a published branch, phase 2a), the same checks
    and the push, and the PR that's already open stands. With `push=False` (a
    bench job's no-op publisher) the same checks run and the commit that would be
    sent is recorded in the vault, but nothing leaves the host."""
    from vigild.workspace import WorkspaceError

    if branch in PROTECTED or branch == base or not BRANCH.match(branch) or ".." in branch:
        raise PublishError(f"refusing to publish branch {branch!r}: only vigil/* branches")
    try:
        changed = [path for _, path in ws.changed(repo, base_sha, head)]
    except WorkspaceError as e:
        raise PublishError(str(e)) from e
    outside = out_of_scope(changed, scope)
    if outside:
        raise PublishError(f"files changed outside the contract's scope: {outside}")
    try:
        # Tier 1: the job's `.vigil/` artifacts stay in the vault; what goes to the
        # target repo is the same tree without them (the base's own `.vigil` kept)
        send = ws.public_commit(repo, head, base_sha, branch)
        if existing and ws.published_sha(repo, branch) is None and send != head:
            raise LegacyBranch(
                f"{branch} predates artifact stripping and carries .vigil/; vigild never "
                "forces a branch: the owner must clean it by hand, or re-plan"
            )
        sent = [path for _, path in ws.changed(repo, base_sha, send)]
        if not sent:
            raise PublishError(f"{branch} changes nothing since {base_sha[:12]}")
        outside = out_of_scope(sent, scope)  # what is sent, too
        if outside:
            raise PublishError(f"files in the commit sent are outside the scope: {outside}")
        if push:
            ws.push(repo, send, branch, token)
        ws.record_published(repo, branch, send)
    except WorkspaceError as e:
        raise PublishError(str(e)) from e
    if existing:
        return existing
    return pulls.create_pull(repo, head=branch, base=base, title=title, body=body, draft=True)
