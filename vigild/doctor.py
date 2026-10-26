"""`vigil doctor`: one health call per credential and dependency. Prints the
check's name and pass, fail or skip; never a value. The GitHub checks are the
coordinator's App verification: GET /app, one installation, and a mint and
revoke for each repo in configs/repos.yaml (plus claude-mods, read-only)."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from vigild import config
from vigild.config import Config, Secrets

TIMEOUT = 15.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: skipped
    note: str = ""


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        word = "skip" if c.ok is None else ("pass" if c.ok else "fail")
        lines.append(f"{word}  {c.name}" + (f"  ({c.note})" if c.note else ""))
    return "\n".join(lines)


def _safe(name: str, fn: Any) -> Check:
    try:
        ok, note = fn()
        return Check(name, ok, note)
    except Exception as e:  # report the kind of failure, never a value
        return Check(name, False, type(e).__name__)


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex((host, port)) == 0


def run_checks(
    cfg: Config,
    secrets: Secrets,
    *,
    http: httpx2.Client,
    docker: Any,
    repos: list[str] | None = None,
    include_claude_mods: bool = True,
) -> list[Check]:
    out: list[Check] = []
    out.append(
        _safe("configs (incl. the private-repo rule)", lambda: (config.load() is not None, ""))
    )

    key = secrets.get("ANTHROPIC_API_KEY")
    if key:
        out.append(
            _safe(
                "ANTHROPIC_API_KEY",
                lambda: (
                    http.get(
                        "https://api.anthropic.com/v1/models",
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                        timeout=TIMEOUT,
                    ).status_code
                    == 200,
                    "GET /v1/models",
                ),
            )
        )
    else:
        out.append(Check("ANTHROPIC_API_KEY", False, "missing"))

    app_id, key_path = secrets.get("GITHUB_APP_ID"), secrets.get("GITHUB_APP_KEY_PATH")
    if app_id and key_path:
        from vigild.github.app import READ_ONLY, WORKER_PERMISSIONS, GitHubApp

        app = GitHubApp(
            app_id,
            Path(key_path).expanduser().read_bytes(),
            http=http,
            claude_mods=cfg.vigild.github.claude_mods,
            allowed_repos=list(cfg.repos.repos),
        )
        hdr = app._app_headers
        out.append(
            _safe(
                "GITHUB_APP_ID+GITHUB_APP_KEY_PATH: GET /app",
                lambda: (
                    http.get(f"{app.api}/app", headers=hdr(), timeout=TIMEOUT).json().get("slug")
                    == "vigil-worker",
                    "slug vigil-worker",
                ),
            )
        )
        out.append(
            _safe(
                "vigil-worker: one installation",
                lambda: (
                    len(
                        http.get(
                            f"{app.api}/app/installations", headers=hdr(), timeout=TIMEOUT
                        ).json()
                    )
                    == 1,
                    "",
                ),
            )
        )

        def mint_revoke(repo: str, perms: dict[str, str]) -> tuple[bool, str]:
            tok = app.mint(repo, perms)
            app.revoke(tok.token)
            return True, ""

        def visibility(repo: str) -> tuple[bool, str]:
            private = app.is_private(repo)
            yaml_private = cfg.repo(repo).visibility == "private"
            if private and not yaml_private:
                return False, "private on GitHub, public in repos.yaml"
            if yaml_private and not private:
                return True, "public on GitHub; repos.yaml keeps it private"
            return True, "matches"

        for repo in repos if repos is not None else list(cfg.repos.repos):
            out.append(_safe(f"visibility: {repo}", lambda r=repo: visibility(r)))
        for repo in repos if repos is not None else list(cfg.repos.repos):
            out.append(
                _safe(
                    f"token mint+revoke: {repo}",
                    lambda r=repo: mint_revoke(r, dict(WORKER_PERMISSIONS)),
                )
            )
        if include_claude_mods:
            cm = cfg.vigild.github.claude_mods
            out.append(
                _safe(
                    f"token mint+revoke: {cm} (contents:read)",
                    lambda: mint_revoke(cm, dict(READ_ONLY)),
                )
            )
    else:
        out.append(Check("GITHUB_APP_ID+GITHUB_APP_KEY_PATH", False, "missing"))

    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        out.append(
            Check(
                name,
                bool(secrets.get(name)),
                "" if secrets.get(name) else "missing: Jev off, Haiku covers",
            )
        )

    if secrets.get("PHOENIX_ADMIN_SECRET"):
        out.append(
            _safe(
                "Phoenix (healthz)",
                lambda: (
                    http.get("http://127.0.0.1:6006/healthz", timeout=TIMEOUT).status_code == 200,
                    "",
                ),
            )
        )
        out.append(
            _safe("OTel collector (127.0.0.1:4318)", lambda: (_port_open("127.0.0.1", 4318), ""))
        )
    else:
        out.append(Check("PHOENIX_ADMIN_SECRET", False, "missing: tracing off"))

    raw_key = secrets.get("VIGIL_HOST_KEY")
    have_key = bool(raw_key) and len(raw_key or "") >= 64
    # the host policy: hidden tests are off for a repo unless configs/repos.yaml says so
    for name, policy in cfg.repos.repos.items():
        on = policy.hidden_tests.enabled
        out.append(
            Check(
                f"hidden tests: {name}",
                True if on else None,
                "on (host policy)" if on else "off (host policy)",
            )
        )
    any_on = any(p.hidden_tests.enabled for p in cfg.repos.repos.values())
    out.append(
        Check(
            "VIGIL_HOST_KEY",
            have_key or not any_on,
            ""
            if have_key
            else "not needed: hidden tests are off for every repo"
            if not any_on
            else "too short: needs 64 hex characters; every hidden-test task escalates"
            if raw_key
            else "missing: every hidden-test task escalates",
        )
    )

    token = secrets.get("VIGIL_API_TOKEN")
    if token:
        api = cfg.vigild.api
        out.append(
            _safe(
                "VIGIL_API_TOKEN: GET /api/health",
                lambda: (
                    http.get(
                        f"http://{api.host}:{api.port}/api/health",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=TIMEOUT,
                    ).status_code
                    == 200,
                    "vigild must be running",
                ),
            )
        )
    else:
        out.append(Check("VIGIL_API_TOKEN", False, "missing"))

    if docker is None:
        out.append(Check("Docker", None, "not checked"))
    else:
        from vigild.devimage import DockerImages

        out.append(_safe("Docker", lambda: (bool(docker.ping()), "")))
        for image in (cfg.vigild.images.claude_executor, cfg.vigild.images.egress_proxy):
            out.append(
                _safe(
                    f"image {image}",
                    lambda i=image: (
                        DockerImages(docker).image_id_of(i) is not None,
                        "scripts/build-images.py",
                    ),
                )
            )
        from vigild.db import Store

        store = Store.open(cfg.vigild.data_path / "vigil.db")
        for repo in cfg.repos.repos:

            def last_build(r: str = repo) -> tuple[bool, str]:
                # what vigild last built and validated; doctor builds nothing
                rows = store.q(
                    "SELECT * FROM dev_images WHERE repo=? ORDER BY id DESC LIMIT 1", (r,)
                )
                if not rows:
                    return False, "never built (the first job builds it; or `vigil dev-image`)"
                row = rows[0]
                if not row["ok"]:
                    return False, f"{row['image']} failed its {row['stage'] or 'check'}"
                current = DockerImages(docker).image_id_of(row["image"])
                if current != row["image_id"]:
                    return False, f"{row['image']} is gone or changed since it was validated"
                return True, str(row["image"])

            out.append(_safe(f"dev image: {repo}", last_build))
    return out
