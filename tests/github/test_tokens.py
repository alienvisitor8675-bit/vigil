"""GitHub App: JWT, installation tokens scoped to one repo, down-scoped, revoked."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vigild.github.app import (
    READ_ONLY,
    WORKER_PERMISSIONS,
    GitHubApp,
    TokenPolicyError,
)

CLAUDE_MODS = "thomasbardhi01/claude-mods"


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, rsa.RSAPublicKey]:
    # A throwaway key generated per test run; not a secret.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return pem, key.public_key()


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.revoked: list[str] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        path = req.url.path
        if path.endswith("/installation") and req.method == "GET":
            return httpx2.Response(200, json={"id": 4242})
        if path == "/app/installations/4242/access_tokens":
            body = json.loads(req.content)
            return httpx2.Response(
                201,
                json={
                    "token": "ghs_" + "x" * 36,
                    "expires_at": "2026-10-22T13:00:00Z",
                    "permissions": body["permissions"],
                    "repositories": [{"name": n} for n in body["repositories"]],
                },
            )
        if path == "/installation/token" and req.method == "DELETE":
            self.revoked.append(req.headers["authorization"])
            return httpx2.Response(204)
        return httpx2.Response(404)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def app(keypair: tuple[bytes, rsa.RSAPublicKey], fake: FakeGitHub) -> GitHubApp:
    return GitHubApp(
        "123456",
        keypair[0],
        http=httpx2.Client(transport=httpx2.MockTransport(fake)),
        claude_mods=CLAUDE_MODS,
        allowed_repos=["thomasbardhi01/parkagent", "thomasbardhi01/vigil"],
    )


def test_jwt_is_rs256_with_a_short_life(
    app: GitHubApp, keypair: tuple[bytes, rsa.RSAPublicKey]
) -> None:
    token = app.jwt()
    claims = jwt.decode(token, keypair[1], algorithms=["RS256"])
    now = time.time()
    assert claims["iss"] == "123456"  # the numeric App ID (GITHUB_APP_ID)
    assert now - 70 <= claims["iat"] <= now - 50
    assert claims["exp"] - claims["iat"] <= 600


def test_worker_token_is_one_repo_and_down_scoped(app: GitHubApp, fake: FakeGitHub) -> None:
    tok = app.mint("thomasbardhi01/parkagent", WORKER_PERMISSIONS)
    req = fake.requests[-1]
    body = json.loads(req.content)
    assert body["repositories"] == ["parkagent"]
    assert body["permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    assert req.headers["authorization"].startswith("Bearer ey")  # the App JWT
    assert tok.repo == "thomasbardhi01/parkagent"


@pytest.mark.parametrize(
    "perm", ["administration", "workflows", "secrets", "actions", "organization_administration"]
)
def test_forbidden_permissions_are_never_requested(
    app: GitHubApp, fake: FakeGitHub, perm: str
) -> None:
    with pytest.raises(TokenPolicyError):
        app.mint("thomasbardhi01/parkagent", {**WORKER_PERMISSIONS, perm: "write"})
    assert not any(r.url.path.endswith("access_tokens") for r in fake.requests)


def test_claude_mods_tokens_are_always_contents_read(app: GitHubApp, fake: FakeGitHub) -> None:
    app.mint_claude_mods()
    body = json.loads(fake.requests[-1].content)
    assert body == {"repositories": ["claude-mods"], "permissions": {"contents": "read"}}
    assert READ_ONLY == {"contents": "read"}


@pytest.mark.parametrize(
    "perms",
    [WORKER_PERMISSIONS, {"contents": "write"}, {"contents": "read", "metadata": "read"}, {}],
)
def test_no_code_path_mints_more_than_read_on_claude_mods(
    app: GitHubApp, perms: dict[str, str]
) -> None:
    with pytest.raises(TokenPolicyError):
        app.mint(CLAUDE_MODS, perms)


def test_case_and_spacing_cannot_sneak_past_the_claude_mods_rule(app: GitHubApp) -> None:
    with pytest.raises(TokenPolicyError):
        app.mint(" ThomasBardhi01/Claude-Mods ", WORKER_PERMISSIONS)


def test_lease_revokes_on_exit_even_on_error(app: GitHubApp, fake: FakeGitHub) -> None:
    with pytest.raises(RuntimeError), app.lease("thomasbardhi01/parkagent") as tok:
        assert tok.token.startswith("ghs_")
        raise RuntimeError("container died")
    assert fake.revoked == [f"Bearer {'ghs_' + 'x' * 36}"]


def test_token_repr_never_shows_the_token(app: GitHubApp) -> None:
    tok = app.mint("thomasbardhi01/parkagent", WORKER_PERMISSIONS)
    assert "ghs_" not in repr(tok) and "ghs_" not in str(tok)


@pytest.mark.parametrize(
    "repo", ["thomasbardhi01/hearth", "thomasbardhi01/other", "evil/parkagent"]
)
def test_no_token_for_a_repo_outside_repos_yaml(
    app: GitHubApp, fake: FakeGitHub, repo: str
) -> None:
    """The installation covers all eight of the owner's repos; vigild mints only
    for the ones configs/repos.yaml lists (plus claude-mods, read-only)."""
    with pytest.raises(TokenPolicyError, match=r"repos\.yaml"):
        app.mint(repo, WORKER_PERMISSIONS)
    assert fake.requests == []


@pytest.mark.parametrize("perm", ["issues", "checks", "artifact_metadata"])
def test_publish_tokens_never_carry_the_apps_other_permissions(perm: str) -> None:
    """The App holds issues:write, checks:read and artifact_metadata:read too;
    a publish token is down-scoped to what pushing and opening a PR needs."""
    assert perm not in WORKER_PERMISSIONS


def test_issues_read_is_allowed_for_label_intake(app: GitHubApp, fake: FakeGitHub) -> None:
    app.mint("thomasbardhi01/parkagent", {"issues": "read", "metadata": "read"})
    with pytest.raises(TokenPolicyError):
        app.mint("thomasbardhi01/parkagent", {"issues": "write"})


def test_github_adding_metadata_read_is_accepted_but_more_is_revoked(
    keypair: tuple[bytes, rsa.RSAPublicKey],
) -> None:
    extra: dict[str, str] = {"metadata": "read"}

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("/installation"):
            return httpx2.Response(200, json={"id": 1})
        if req.url.path.endswith("/access_tokens"):
            body = json.loads(req.content)
            return httpx2.Response(
                201,
                json={
                    "token": "ghs_y",
                    "expires_at": "z",
                    "permissions": {**body["permissions"], **extra},
                },
            )
        return httpx2.Response(204)

    app = GitHubApp(
        "1",
        keypair[0],
        http=httpx2.Client(transport=httpx2.MockTransport(handler)),
        claude_mods=CLAUDE_MODS,
        allowed_repos=[],
    )
    app.mint_claude_mods()  # contents:read + GitHub's metadata:read is fine
    extra["contents"] = "write"
    with pytest.raises(TokenPolicyError, match="revoked"):
        app.mint_claude_mods()


def test_is_private_uses_a_metadata_read_token_and_revokes_it(
    keypair: tuple[bytes, rsa.RSAPublicKey],
) -> None:  # item 10
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        if req.url.path.endswith("/installation"):
            return httpx2.Response(200, json={"id": 1})
        if req.url.path.endswith("/access_tokens"):
            body = json.loads(req.content)
            return httpx2.Response(
                201, json={"token": "ghs_m", "expires_at": "z", "permissions": body["permissions"]}
            )
        if req.url.path == "/repos/thomasbardhi01/vigil":
            return httpx2.Response(200, json={"private": True})
        return httpx2.Response(204)

    app = GitHubApp(
        "1",
        keypair[0],
        http=httpx2.Client(transport=httpx2.MockTransport(handler)),
        claude_mods=CLAUDE_MODS,
        allowed_repos=["thomasbardhi01/vigil"],
    )
    assert app.is_private("thomasbardhi01/vigil") is True
    mint = next(r for r in seen if r.url.path.endswith("/access_tokens"))
    assert json.loads(mint.content)["permissions"] == {"metadata": "read"}
    assert any(r.method == "DELETE" for r in seen)


@pytest.mark.parametrize("payload", [{"private": None}, {"private": "false"}, {"private": 0}, {}])
def test_is_private_reads_only_a_json_bool_and_anything_else_is_private(
    keypair: tuple[bytes, rsa.RSAPublicKey], payload: dict[str, Any]
) -> None:
    """Review A3: `bool(None)` made a null answer public. Only JSON true or
    false is read; null, a string, a number or a missing key count as private."""

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("/installation"):
            return httpx2.Response(200, json={"id": 1})
        if req.url.path.endswith("/access_tokens"):
            body = json.loads(req.content)
            return httpx2.Response(
                201, json={"token": "ghs_m", "expires_at": "z", "permissions": body["permissions"]}
            )
        if req.url.path == "/repos/thomasbardhi01/vigil":
            return httpx2.Response(200, json=payload)
        return httpx2.Response(204)

    app = GitHubApp("1", keypair[0], http=httpx2.Client(transport=httpx2.MockTransport(handler)),
                    claude_mods=CLAUDE_MODS, allowed_repos=["thomasbardhi01/vigil"])  # fmt: skip
    assert app.is_private("thomasbardhi01/vigil") is True
