"""vigil doctor: one health check per credential; prints names and pass/fail only."""

from __future__ import annotations

import json
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vigild import doctor
from vigild.config import Config, Secrets
from vigild.doctor import Check, render, run_checks

SECRET_VALUES = ["sk-ant-fake-value-123", "a1-phoenix-admin-fake-456", "api-token-fake-789"]


def _pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


PRIVATE: dict[str, bool] = {}


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
        return httpx2.Response(200, json={"private": PRIVATE.get("vigil", False)})
    if p == "/repos/thomasbardhi01/parkagent":
        return httpx2.Response(200, json={"private": False})
    if req.url.host == "openrouter.ai":
        ok = req.headers.get("authorization") == "Bearer sk-or-fake-value-000"
        return httpx2.Response(200 if ok else 401, json={"data": {}})
    if req.url.host == "api.cloudflare.com" and p.endswith("/tokens/verify"):
        # an account-owned token: the user endpoint refuses it, the account's takes it
        ok = "/accounts/" in p and req.headers.get("authorization") == "Bearer cf-fake-value-000"
        return httpx2.Response(200 if ok else 401, json={"success": ok})
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
    assert vis.ok and vis.note == "public, live"  # GitHub's live answer (owner, 2026-10-23)
    # no private repo uses Jev on Cloudflare, and no credentials: skipped
    assert names["CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN"].ok is None
    assert names["OPENROUTER_API_KEY"].ok is False  # missing: reported, not fatal
    # parkagent wasn't selected: config only, no call about it (review A1)
    pa = names["decider: thomasbardhi01/parkagent (not checked: not selected)"]
    assert pa.ok is None


def test_doctor_makes_no_call_about_a_repo_it_was_not_asked_about(
    cfg: Config, tmp_path: object
) -> None:
    pem = tmp_path / "app.pem"  # type: ignore[operator]
    pem.write_bytes(_pem())
    secrets = Secrets({"GITHUB_APP_ID": "123456", "GITHUB_APP_KEY_PATH": str(pem),
                       "OPENROUTER_API_KEY": "sk-or-fake-value-000"})  # fmt: skip
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return _handler(req)

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    run_checks(cfg, secrets, http=http, docker=None, repos=["thomasbardhi01/vigil"],
               include_claude_mods=False)  # fmt: skip
    about_pa = [r for r in seen if b"parkagent" in r.content or "parkagent" in r.url.path]
    assert about_pa == []
    assert any(r.url.path == "/repos/thomasbardhi01/vigil" for r in seen)


def test_doctor_names_each_repos_live_decider_route(cfg: Config, tmp_path: object) -> None:
    pem = tmp_path / "app.pem"  # type: ignore[operator]
    pem.write_bytes(_pem())
    secrets = Secrets({
        "GITHUB_APP_ID": "123456", "GITHUB_APP_KEY_PATH": str(pem),
        "OPENROUTER_API_KEY": "sk-or-fake-value-000",
        "CLOUDFLARE_ACCOUNT_ID": "acct-fake", "CLOUDFLARE_API_TOKEN": "cf-fake-value-000",
    })  # fmt: skip
    http = httpx2.Client(transport=httpx2.MockTransport(_handler))
    PRIVATE["vigil"] = False
    names = {c.name: c for c in run_checks(cfg, secrets, http=http, docker=None, repos=None,
                                           include_claude_mods=False)}  # fmt: skip
    assert names["OPENROUTER_API_KEY"].ok and names["OPENROUTER_API_KEY"].note == "GET /api/v1/key"
    for repo in ("thomasbardhi01/parkagent", "thomasbardhi01/vigil"):
        assert names[f"decider: {repo} (public, live) → jev-openrouter (haiku escalation)"].ok
    cf = names["CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN"]
    assert cf.ok and cf.note == "unused: no private repo lists jev-cloudflare"
    # vigil goes private: its Jev is Cloudflare's, and the credentials are used
    PRIVATE["vigil"] = True
    checks = run_checks(cfg, secrets, http=http, docker=None, repos=None, include_claude_mods=False)
    names = {c.name: c for c in checks}
    assert names[
        "decider: thomasbardhi01/vigil (private, live) → jev-cloudflare (haiku escalation)"
    ].ok
    assert names["CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN"].note == ""
    PRIVATE["vigil"] = False
    out = render(checks)
    for v in ("sk-or-fake-value-000", "cf-fake-value-000", "acct-fake"):
        assert v not in out


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


# --- backup freshness ---------------------------------------------------------


BNOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _bset(dest: Path, when: datetime) -> None:
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(exist_ok=True)
    (dest / f"vigil-{stamp}.marker").write_text("b" * 16)
    (dest / f"vigil-{stamp}.db").write_bytes(b"x")
    (dest / f"vigil-{stamp}.vault").mkdir()


def _plist(path: Path, value: str | None) -> Path:
    env = {} if value is None else {"VIGIL_BACKUP_DIR": value}
    path.write_bytes(plistlib.dumps({"Label": "dev.vigil.backup", "EnvironmentVariables": env}))
    return path


def _bcheck(env: dict[str, str], plist: Path) -> Check:
    return doctor.backup_freshness_check(environ=env, plist=plist, now=BNOW)


def test_env_var_wins_over_the_plist(tmp_path: Path) -> None:
    good, stale = tmp_path / "good", tmp_path / "stale"
    _bset(good, BNOW - timedelta(hours=1))
    _bset(stale, BNOW - timedelta(hours=90))
    plist = _plist(tmp_path / "p.plist", str(stale))
    c = _bcheck({"VIGIL_BACKUP_DIR": str(good)}, plist)
    assert c.name == "backup freshness" and c.ok is True
    assert str(good) not in c.note


def test_plist_value_is_used_when_the_env_var_is_unset(tmp_path: Path) -> None:
    stale = tmp_path / "stale"
    _bset(stale, BNOW - timedelta(hours=90))
    assert _bcheck({}, _plist(tmp_path / "p.plist", str(stale))).ok is False
    good = tmp_path / "good"
    _bset(good, BNOW - timedelta(hours=1))
    assert _bcheck({}, _plist(tmp_path / "p.plist", str(good))).ok is True


def test_no_env_var_and_no_plist_skips(tmp_path: Path) -> None:
    c = _bcheck({}, tmp_path / "absent.plist")
    assert c.ok is None
    assert c.note == "no backup destination (VIGIL_BACKUP_DIR unset, plist not installed)"


def test_an_installed_plist_with_a_problem_fails_not_skips(tmp_path: Path) -> None:
    bad = tmp_path / "bad.plist"
    bad.write_text("not a plist")
    cases = [
        _plist(tmp_path / "a.plist", "__BACKUP_DESTINATION__"),
        _plist(tmp_path / "b.plist", None),
        bad,
        _plist(tmp_path / "c.plist", str(tmp_path / "nowhere")),
    ]
    for p in cases:
        c = _bcheck({}, p)
        assert c.ok is False, p.name
        assert str(tmp_path) not in c.note


def test_env_var_is_checked_even_without_a_plist(tmp_path: Path) -> None:
    assert _bcheck({"VIGIL_BACKUP_DIR": str(tmp_path / "nowhere")}, tmp_path / "x").ok is False


def test_run_checks_includes_backup_freshness(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIGIL_BACKUP_DIR", raising=False)
    monkeypatch.setattr(doctor, "BACKUP_PLIST", tmp_path / "absent.plist")
    http = httpx2.Client(transport=httpx2.MockTransport(_handler))
    names = {c.name: c for c in run_checks(cfg, Secrets({}), http=http, docker=None, repos=[])}
    assert names["backup freshness"].ok is None
    d = tmp_path / "d"
    _bset(d, datetime.now(UTC) - timedelta(hours=2))
    monkeypatch.setenv("VIGIL_BACKUP_DIR", str(d))
    names = {c.name: c for c in run_checks(cfg, Secrets({}), http=http, docker=None, repos=[])}
    assert names["backup freshness"].ok is True
