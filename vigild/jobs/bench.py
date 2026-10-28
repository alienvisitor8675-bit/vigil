"""Bench mode (bench slice 1a; owner, 2026-10-27, issue #8).

A bench job (`source: bench`) measures vigil on a fixed task. It lives on a virtual
`bench/<name>` repo, never touches GitHub, skips the owner's spec approval (a
guarded FSM edge), draws money from the experiment line, and writes only
`eval-only` rows. This module holds the pieces that don't belong to the runner:

- `BenchPublisher`: the publisher a bench job gets. It runs the real scope checks
  and records the commit it would have sent in the vault, but pushes nothing and
  opens no PR; each call records "not published (bench)".
- The failure tags a run ends with: `spec`, `environment`, `model`, `harness`,
  and `budget` (the experiment line was full).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vigild.github.publish import publish
from vigild.workspace import Workspace

NOT_PUBLISHED = "not published (bench)"
TAGS = ("spec", "environment", "model", "harness", "budget")
URL_PREFIX = "bench:not-published/"


class _NoPulls:
    """Opens nothing; the 'PR' is a marker URL so the job's stage checks (which look
    for a published head, as for any PR) still hold."""

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> str:
        return f"{URL_PREFIX}{repo}/{head}"


class BenchPublisher:
    """The no-op publisher. `records` is what it was asked to do and why it didn't."""

    reason = NOT_PUBLISHED

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.records: list[dict[str, Any]] = []

    def publish(
        self,
        *,
        repo: str,
        head: str,
        base_sha: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        scope: Sequence[str],
        existing: str | None = None,
        job_id: str | None = None,
    ) -> str:
        url = publish(
            self.workspace,
            repo=repo,
            head=head,
            base_sha=base_sha,
            branch=branch,
            base=base,
            title=title,
            body=body,
            pulls=_NoPulls(),
            scope=scope,
            token=None,
            existing=existing,
            push=False,
        )
        self.records.append(
            {"op": "publish", "repo": repo, "branch": branch, "head": head, "reason": self.reason}
        )
        return url

    def close(self, repo: str, url: str, job_id: str | None = None) -> None:
        self.records.append({"op": "close", "repo": repo, "reason": self.reason})
