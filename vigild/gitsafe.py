"""The one way vigild runs git on the host.

Every call ignores the system and global config (so ambient credential
helpers and `url.*.insteadOf` rules never apply), disables hooks, fsmonitor,
external diff and textconv, and checks objects on transfer. A token, when
one is needed, reaches only this git process as an extra HTTP header in its
environment: never argv, never a file.

vigild runs git only in repos it owns (mirrors, its vault) or in a task clone
before the session starts. After a session, the worker's result arrives as a
bundle (`Workspace.import_result`); host git never runs in a `.git` a worker
could have written (review round 1, blocker 1).
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

HARDENING = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.sshCommand=false",
    "-c", "credential.helper=",
    "-c", "diff.external=",
    "-c", "transfer.fsckObjects=true",
    "-c", "protocol.allow=never",
    "-c", "protocol.file.allow=always",
    "-c", "protocol.https.allow=always",
    # No auto-maintenance or auto-gc in any repo vigild owns (round 5): a
    # fetch's maintenance holds objects/maintenance.lock while a clone copies
    # objects/, and the file vanishes mid-copy.
    "-c", "maintenance.auto=false",
    "-c", "gc.auto=0",
)  # fmt: skip


class GitError(RuntimeError):
    pass


def git_env(token: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "SSH_AUTH_SOCK"}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1",  # a path is a path, never a pattern (round 3, F8)
            "GIT_ASKPASS": "/usr/bin/false",
        }
    )
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
    return env


def git(
    cwd: Path,
    *args: str,
    token: str | None = None,
    timeout: int = 600,
    raw: bool = False,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    out = subprocess.run(
        ["git", *HARDENING, *args],
        cwd=cwd,
        env={**git_env(token), **(env or {})},
        capture_output=True,
        text=not raw,
        timeout=timeout,
        check=False,
        input=stdin,
    )
    if out.returncode != 0:
        err = out.stderr if isinstance(out.stderr, str) else out.stderr.decode(errors="replace")
        raise GitError(f"git {args[0]} failed: {err.strip()[:300]}")
    stdout = out.stdout if isinstance(out.stdout, str) else out.stdout.decode(errors="replace")
    return stdout.strip() if not raw else stdout


def git_bytes(cwd: Path, *args: str, timeout: int = 600) -> bytes:
    """git's stdout as bytes (blob contents), under the same hardening."""
    out = subprocess.run(
        ["git", *HARDENING, *args],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if out.returncode != 0:
        err = out.stderr.decode(errors="replace").strip()[:300]
        raise GitError(f"git {args[0]} failed: {err}")
    return bytes(out.stdout)
