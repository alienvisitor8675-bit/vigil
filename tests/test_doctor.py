"""vigil doctor: one health check per credential; prints names and pass/fail only."""

from __future__ import annotations

import json

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vigild.config import Config, Secrets
from vigild.doctor import Check, render, run_checks

SECRET_VALUES = ["sk-ant-fake-value-123", "a1-phoenix-admin-fake-456", "api-token-fake-789"]


def _pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _handler(req: httpx2.Request) -> httpx2.Response:
    p = req.url.path
    if req.url.host == "api.anthropic.com":
        ok = req.headers.get("x-api-key") == SECRET_VALUES[0]
        return httpx2.Response(200 if ok else 401, json={"data": []})
    if p == "/app":
        return httpx2.Response(200, json={"slug": "vigil-worker"})
    if p == "/app/installations":
        return httpx2.Response(200, json=[{"id": 42}])
    if p.endswith("/installation") and req.method == "GET":
        return httpx2.Response(200, json={"id": 42})
    if p == "/app/installations/42/access_tokens":
        body = json.loads(req.content)
        return httpx2.Response(
            201, json={"token": "ghs_x", "expires_at": "z", "permissions": body["permissions"]}
        )
    if p == "/installation/token":
        return httpx2.Response(204)
    if p == "/repos/thomasbardhi01/vigil":
        return httpx2.Response(200, json={"private": False})
    if p == "/healthz":
        return httpx2.Response(200, text="OK")
    return httpx2.Response(404)


def test_doctor_checks_and_never_prints_values(cfg: Config, tmp_path: object) -> None:
    pem = tmp_path / "app.pem"  # type: ignore[operator]
    pem.write_bytes(_pem())
    secrets = Secrets(
        {
            "ANTHROPIC_API_KEY": SECRET_VALUES[0],
            "PHOENIX_ADMIN_SECRET": SECRET_VALUES[1],
            "VIGIL_API_TOKEN": SECRET_VALUES[2],
            "GITHUB_APP_ID": "123456",
            "GITHUB_APP_KEY_PATH": str(pem),
        }
    )
    http = httpx2.Client(transport=httpx2.MockTransport(_handler))
    checks = run_checks(
        cfg,
        secrets,
        http=http,
        docker=None,
        repos=["thomasbardhi01/vigil"],
        include_claude_mods=True,
    )
    names = {c.name: c for c in checks}
    assert names["ANTHROPIC_API_KEY"].ok
    assert names["GITHUB_APP_ID+GITHUB_APP_KEY_PATH: GET /app"].ok
    assert names["vigil-worker: one installation"].ok
    assert names["token mint+revoke: thomasbardhi01/vigil"].ok
    assert names["token mint+revoke: thomasbardhi01/claude-mods (contents:read)"].ok
    assert names["configs (incl. the private-repo rule)"].ok
    vis = names["visibility: thomasbardhi01/vigil"]
    assert vis.ok and "keeps it private" in vis.note
    assert not names["CLOUDFLARE_API_TOKEN"].ok  # missing: reported, not fatal
    out = render(checks)
    for v in [*SECRET_VALUES, "ghs_x"]:
        assert v not in out
    for line in out.splitlines():
        assert line.split()[0] in ("pass", "fail", "skip")


def test_render_format() -> None:
    out = render([Check("X", True, ""), Check("Y", False, "missing"), Check("Z", None, "docker")])
    assert out.splitlines() == ["pass  X", "fail  Y  (missing)", "skip  Z  (docker)"]


def test_doctor_reports_the_hidden_tests_switch_per_repo(cfg: Config) -> None:
    http = httpx2.Client(transport=httpx2.MockTransport(_handler))
    names = {c.name: c for c in run_checks(cfg, Secrets({}), http=http, docker=None, repos=[])}
    for repo in cfg.repos.repos:
        c = names[f"hidden tests: {repo}"]
        assert c.ok is None and c.note == "off (host policy)"
    # off everywhere: a missing VIGIL_HOST_KEY is not a failure
    assert names["VIGIL_HOST_KEY"].ok is not False
    some = next(iter(cfg.repos.repos))
    cfg.repos.repos[some].hidden_tests.enabled = True
    names = {c.name: c for c in run_checks(cfg, Secrets({}), http=http, docker=None, repos=[])}
    assert names[f"hidden tests: {some}"].note == "on (host policy)"
    assert names["VIGIL_HOST_KEY"].ok is False  # needed now
