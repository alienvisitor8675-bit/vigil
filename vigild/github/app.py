"""The vigil-worker GitHub App (B19): JWT, installation tokens, revoke.

The App is installed on all of the owner's repos and holds contents:write,
pull_requests:write, issues:write, checks:read, metadata:read and
artifact_metadata:read. vigild mints only for repos configs/repos.yaml lists
(plus claude-mods, read-only), and every token is down-scoped to its use:
publishing a branch needs contents:write + pull_requests:write; label intake
needs issues:read. Tokens live at most one hour; `lease()` revokes them.
claude-mods tokens are always `{"contents": "read"}`: the App's
installation there could write (GitHub sets one permission set per App), so
this module refuses every other request for that repo, whatever the caller
asks for. For now the coord uses this same App (one App per role is
deferred; docs/architecture.md).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx2
import jwt

API = "https://api.github.com"
WORKER_PERMISSIONS: Mapping[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
}
READ_ONLY: Mapping[str, str] = {"contents": "read"}
# The most each permission may ever be minted at. Nothing else is minted.
ALLOWED: Mapping[str, set[str]] = {
    "contents": {"read", "write"},
    "pull_requests": {"read", "write"},
    "metadata": {"read"},
    "issues": {"read"},
}
TIMEOUT = 20.0


class TokenPolicyError(ValueError):
    pass


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallationToken:
    repo: str
    permissions: Mapping[str, str]
    expires_at: str
    token: str = field(repr=False)

    def __str__(self) -> str:
        return (
            f"InstallationToken({self.repo}, {dict(self.permissions)}, expires {self.expires_at})"
        )


def _norm(repo: str) -> str:
    return repo.strip().lower()


class GitHubApp:
    def __init__(
        self,
        app_id: str,
        private_key_pem: bytes,
        *,
        http: httpx2.Client,
        claude_mods: str,
        allowed_repos: list[str],
        api: str = API,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = app_id
        self.allowed = {_norm(r) for r in allowed_repos}
        self._key = private_key_pem
        self.http = http
        self.claude_mods = _norm(claude_mods)
        self.api = api.rstrip("/")
        self.clock = clock

    def jwt(self) -> str:
        now = int(self.clock())
        claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def _app_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def check_policy(self, repo: str, permissions: Mapping[str, str]) -> None:
        perms = dict(permissions)
        if _norm(repo) == self.claude_mods:
            if perms != dict(READ_ONLY):
                raise TokenPolicyError(
                    f"{repo} tokens are always {dict(READ_ONLY)}; refused {perms}"
                )
            return
        if _norm(repo) not in self.allowed:
            raise TokenPolicyError(f"{repo} is not in configs/repos.yaml; no token minted")
        bad = {k: v for k, v in perms.items() if v not in ALLOWED.get(k, set())}
        if bad:
            raise TokenPolicyError(f"refusing permissions {bad}; allowed: {dict(ALLOWED)}")
        if not perms:
            raise TokenPolicyError("a token must name its permissions")

    def installation_id(self, repo: str) -> int:
        r = self.http.get(
            f"{self.api}/repos/{repo.strip()}/installation",
            headers=self._app_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise GitHubError(f"no vigil-worker installation on {repo} (HTTP {r.status_code})")
        return int(r.json()["id"])

    def mint(self, repo: str, permissions: Mapping[str, str]) -> InstallationToken:
        self.check_policy(repo, permissions)
        repo = repo.strip()
        inst = self.installation_id(repo)
        body = {"repositories": [repo.split("/")[-1]], "permissions": dict(permissions)}
        r = self.http.post(
            f"{self.api}/app/installations/{inst}/access_tokens",
            headers=self._app_headers(),
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code != 201:
            raise GitHubError(f"minting a token for {repo} failed (HTTP {r.status_code})")
        data = r.json()
        granted = data.get("permissions", {})
        # GitHub adds metadata:read to every token; anything else extra is refused.
        if {**granted, "metadata": "read"} != {**dict(permissions), "metadata": "read"}:
            self.revoke(data["token"])
            raise TokenPolicyError(f"GitHub granted {granted}, asked {dict(permissions)}; revoked")
        return InstallationToken(repo, granted, data.get("expires_at", ""), data["token"])

    def mint_claude_mods(self) -> InstallationToken:
        return self.mint(self.claude_mods, READ_ONLY)

    def revoke(self, token: str) -> None:
        r = self.http.delete(
            f"{self.api}/installation/token",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
        if r.status_code not in (204, 401):  # 401: already expired or revoked
            raise GitHubError(f"revoking a token failed (HTTP {r.status_code})")

    @contextmanager
    def lease(
        self, repo: str, permissions: Mapping[str, str] = WORKER_PERMISSIONS
    ) -> Iterator[InstallationToken]:
        tok = self.mint(repo, permissions)
        try:
            yield tok
        finally:
            self.revoke(tok.token)

    def is_private(self, repo: str) -> bool:
        """The repo's real visibility, read with a metadata-only token (item 10)."""
        with self.lease(repo, {"metadata": "read"}) as tok:
            r = self.http.get(
                f"{self.api}/repos/{repo.strip()}",
                headers={
                    "Authorization": f"Bearer {tok.token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=TIMEOUT,
            )
        if r.status_code != 200:
            raise GitHubError(f"reading {repo} failed (HTTP {r.status_code})")
        return bool(r.json()["private"])
