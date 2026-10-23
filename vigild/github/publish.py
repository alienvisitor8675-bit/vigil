"""Publish a worker's branch: push it and open its draft PR, from the host.

P2 (owner, 2026-10-22): workers never hold a GitHub token. After a worker
session ends, vigild has imported its branch into the vault
(`Workspace.import_result`). Here it checks the branch is `vigil/*` and that
every file changed since the base commit vigild recorded (renames counted as
delete + add) is in the contract's scope or the job's own `.vigil/`
artifacts, then pushes the commit from the vault with a one-hour
installation token and opens a draft PR. A draft or nothing.
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


def out_of_scope(changed: Sequence[str], scope: Sequence[str]) -> list[str]:
    """Changed files that are neither in scope nor vigil's own artifacts."""
    return [f for f in changed if not f.startswith(".vigil/") and not in_scope(f, scope)]


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
) -> str:
    """Check, push `head` to `branch`, and open a draft PR into `base`."""
    from vigild.workspace import WorkspaceError

    if branch in PROTECTED or branch == base or not BRANCH.match(branch) or ".." in branch:
        raise PublishError(f"refusing to publish branch {branch!r}: only vigil/* branches")
    try:
        changed = [path for _, path in ws.changed(repo, base_sha, head)]
    except WorkspaceError as e:
        raise PublishError(str(e)) from e
    if not changed:
        raise PublishError(f"{branch} changes nothing since {base_sha[:12]}")
    outside = out_of_scope(changed, scope)
    if outside:
        raise PublishError(f"files changed outside the contract's scope: {outside}")
    try:
        ws.push(repo, head, branch, token)
    except WorkspaceError as e:
        raise PublishError(str(e)) from e
    return pulls.create_pull(repo, head=branch, base=base, title=title, body=body, draft=True)
