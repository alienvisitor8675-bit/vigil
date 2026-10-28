"""`vigil doctor`: one health call per credential and dependency. Prints the
check's name and pass, fail or skip; never a value. The GitHub checks are the
coordinator's App verification: GET /app, one installation, and a mint and
revoke for each repo in configs/repos.yaml (plus claude-mods, read-only)."""

from __future__ import annotations

import os
import plistlib
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from vigild import config
from vigild.config import Config, Secrets
from vigild.repos import JEV, is_bench_repo, live_private, repo_rule_errors

TIMEOUT = 15.0
BACKUP_PLIST = Path.home() / "Library" / "LaunchAgents" / "dev.vigil.backup.plist"
BACKUP_PLACEHOLDER = "__BACKUP_DESTINATION__"


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


def _experiment_line(cfg: Config) -> Check:
    """The experiment line's spend this month (bench; owner, 2026-10-27). Read-only:
    doctor never writes the store."""
    from vigild.budget import Budget
    from vigild.db import Store

    cap = cfg.budgets.experiment_monthly_usd
    db = cfg.vigild.data_path / "vigil.db"
    if not db.exists():
        return Check("experiment budget", None, f"no vigil.db yet; cap ${cap:.2f} a month")

    def spend() -> tuple[bool, str]:
        b = Budget(Store.open(db), cfg.budgets)
        spent, held = b.spent_experiment_month(), b.open_experiment()
        return spent + held <= cap + 1e-9, (
            f"${spent:.2f} spent + ${held:.2f} held of ${cap:.2f} this month (UTC)"
        )

    return _safe("experiment budget", spend)


def _backup_dest(environ: Mapping[str, str], plist: Path) -> tuple[Path | None, bool | None, str]:
    """(destination, verdict, note): VIGIL_BACKUP_DIR, else the installed plist's.
    No destination and verdict None is a skip; a plist that exists but is
    unusable is a failure (verdict False)."""
    env = environ.get("VIGIL_BACKUP_DIR")
    if env:
        return Path(env).expanduser(), None, ""
    if not plist.exists():
        return None, None, "no backup destination (VIGIL_BACKUP_DIR unset, plist not installed)"
    try:
        env_vars = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables", {})
    except Exception as e:  # the kind only, never the content
        return None, False, f"plist unreadable ({type(e).__name__})"
    value = env_vars.get("VIGIL_BACKUP_DIR") if isinstance(env_vars, dict) else None
    if not isinstance(value, str) or not value:
        return None, False, "plist has no VIGIL_BACKUP_DIR"
    if BACKUP_PLACEHOLDER in value:
        return None, False, "plist still holds the template destination"
    return Path(value).expanduser(), None, ""


def backup_freshness_check(
    *,
    environ: Mapping[str, str] | None = None,
    plist: Path | None = None,
    now: datetime | None = None,
) -> Check:
    """The newest complete backup set is at most 36 hours old. Skips only while
    neither VIGIL_BACKUP_DIR nor the installed plist names a destination."""
    from vigild.backup import freshness

    name = "backup freshness"
    try:
        dest, verdict, note = _backup_dest(
            os.environ if environ is None else environ, plist or BACKUP_PLIST
        )
    except OSError as e:
        return Check(name, False, f"destination unreadable ({type(e).__name__})")
    if dest is None:
        return Check(name, verdict, note)
    return _safe(name, lambda: freshness(dest, now=now or datetime.now(UTC)))


def run_checks(
    cfg: Config,
    secrets: Secrets,
    *,
    http: httpx2.Client,
    docker: Any,
    repos: list[str] | None = None,
    include_claude_mods: bool = True,
    host_checks: Any = None,
) -> list[Check]:
    out: list[Check] = []
    vis: dict[str, bool] = {}  # each repo's live visibility, as read here
    app: Any = None
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
            allowed_repos=[r for r in cfg.repos.repos if not is_bench_repo(r)],
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
            """GitHub's live answer is the only source (owner, 2026-10-23); the
            private-repo rule is checked against it."""
            private = live_private(repo, app.is_private)
            vis[repo] = private
            errors = repo_rule_errors(cfg, repo, private)
            word = "private" if private else "public"
            return (False, "; ".join(errors)) if errors else (True, f"{word}, live")

        # bench repos are not on GitHub: nothing about them is asked of it
        on_github = [
            r for r in (repos if repos is not None else list(cfg.repos.repos))
            if not is_bench_repo(r)
        ]  # fmt: skip
        for repo in on_github:
            out.append(_safe(f"visibility: {repo}", lambda r=repo: visibility(r)))
        for repo in on_github:
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

    # Jev (owner, 2026-10-23): OpenRouter for public repos, Cloudflare only
    # for private ones, by each repo's live visibility; Haiku the escalation.
    # Live calls only about the repos asked for (`--repo`); the rest get a
    # config-only line (review A1).
    selected = list(repos) if repos is not None else list(cfg.repos.repos)
    lists_jev = {r for r, pol in cfg.repos.repos.items() if JEV in pol.decider}
    cloudflare_used = any(vis[r] for r in lists_jev if r in vis)  # of the repos read
    or_key = secrets.get("OPENROUTER_API_KEY")
    if or_key:
        out.append(
            _safe(
                "OPENROUTER_API_KEY",
                lambda: (
                    http.get(
                        "https://openrouter.ai/api/v1/key",
                        headers={"Authorization": f"Bearer {or_key}"},
                        timeout=TIMEOUT,
                    ).status_code
                    == 200,
                    "GET /api/v1/key",
                ),
            )
        )
    else:
        out.append(Check("OPENROUTER_API_KEY", False, "missing: Jev off for public repos"))
    account, cf_token = secrets.get("CLOUDFLARE_ACCOUNT_ID"), secrets.get("CLOUDFLARE_API_TOKEN")
    if account and cf_token:
        note = "" if cloudflare_used else "unused: no private repo lists jev-cloudflare"

        def cf_verify() -> tuple[bool, str]:
            """A free token check: the user endpoint, then the account's (an
            account-owned token answers only there)."""
            hdr2 = {"Authorization": f"Bearer {cf_token}"}
            base = "https://api.cloudflare.com/client/v4"
            for url in (f"{base}/user/tokens/verify", f"{base}/accounts/{account}/tokens/verify"):
                r = http.get(url, headers=hdr2, timeout=TIMEOUT)
                if r.status_code == 200 and r.json().get("success"):
                    return True, note
            return False, "token verify refused"

        out.append(_safe("CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN", cf_verify))
    elif cloudflare_used:
        out.append(Check("CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN", False,
                         "missing: Jev off for private repos"))  # fmt: skip
    else:
        out.append(Check("CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN", None,
                         "no private repo lists jev-cloudflare"))  # fmt: skip
    from vigild.decider.policy import decider_routes
    from vigild.decider.wiring import available_routes

    have = available_routes(secrets)
    for repo in cfg.repos.repos:
        if repo not in selected:
            out.append(Check(f"decider: {repo} (not checked: not selected)", None,
                             f"configured: {', '.join(cfg.repo(repo).decider)}"))  # fmt: skip
            continue
        private = vis.get(repo, True)  # unread: private (fail closed)
        how = ("private" if private else "public") + (", live" if repo in vis else ", unread")
        primary, escalation = decider_routes(cfg, repo, have, private=private)
        tail = f" ({escalation} escalation)" if escalation else ""
        out.append(
            Check(f"decider: {repo} ({how}) → {primary or 'none'}{tail}", primary is not None)
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
        out += _sandbox_host_lines(cfg, docker, host_checks)
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
    out.append(_experiment_line(cfg))
    out.append(backup_freshness_check())
    return out


def _sandbox_host_lines(cfg: Config, docker: Any, host_checks: Any) -> list[Check]:
    """ADR-0004: one line per remote sandbox host per rule. With only the
    default `local`, one line says so and nothing remote is touched."""
    remote = cfg.sandbox_hosts.remote_hosts()
    if not remote:
        return [Check("sandbox hosts", True, "local only (no remote host configured)")]
    from vigild.executors.hostcheck import check_hosts
    from vigild.executors.hosts import Hosts, client_for

    hosts = Hosts(cfg, make_client=lambda h: client_for(h) if h.remote else docker)
    try:
        return (host_checks or check_hosts)(cfg, hosts)
    finally:
        hosts.close_remote()
