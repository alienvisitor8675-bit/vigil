"""vigild's startup: load configs and the env file once, build the services,
serve the REST API and the model gateway on two listeners.

  uv run python -m vigild
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from vigild import config
from vigild.api.app import create_app
from vigild.budget import Budget
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.devimage import DevImages
from vigild.gateway.app import create_gateway_app
from vigild.gateway.service import Gateway
from vigild.jobs.runner import JobRunner
from vigild.jobs.service import JobService, Runner
from vigild.services import Services, Tokens
from vigild.telemetry.spans import Telemetry

log = logging.getLogger("vigild")

# Features and the variables that switch them on (runbook "Credentials").
FEATURES: dict[str, tuple[str, ...]] = {
    "api": ("VIGIL_API_TOKEN",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "github-app": ("GITHUB_APP_ID", "GITHUB_APP_KEY_PATH"),
    "tracing": ("PHOENIX_ADMIN_SECRET",),
    "jev-cloudflare": ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
    "metrics-token": ("VIGIL_METRICS_TOKEN",),
}


def degraded(secrets: Secrets) -> set[str]:
    return {f for f, names in FEATURES.items() if not all(secrets.get(n) for n in names)}


class NullRunner:
    def on_job_created(self, job_id: str, needs_spec: bool) -> None:
        log.info("job %s created (needs_spec=%s); no runner attached", job_id, needs_spec)

    def on_approved(self, job_id: str) -> None:
        log.info("job %s approved; no runner attached", job_id)

    def on_resume(self, job_id: str) -> None:
        log.info("job %s resumed; no runner attached", job_id)

    def on_rebuild(self, repo: str, base_sha: str) -> None:
        log.info("dev image rebuild for %s asked; no runner attached", repo)


@dataclass
class Vigild:
    cfg: Config
    secrets: Secrets
    store: Store
    budget: Budget
    gateway: Gateway
    telemetry: Telemetry
    services: Services


def build(
    *,
    config_dir: Path = config.CONFIG_DIR,
    env_file: Path = config.DEFAULT_ENV_FILE,
    data_dir: Path | None = None,
    runner: Runner | None = None,
    **gateway_kwargs: Any,
) -> Vigild:
    cfg = config.load(config_dir)
    override = data_dir if data_dir is not None else config.env_override("VIGIL_DATA_DIR")
    if override is not None:
        cfg.vigild.data_dir = str(override)  # a second vigild (a test run) keeps its own data
    secrets = config.load_secrets(env_file)
    data = cfg.vigild.data_path
    data.mkdir(parents=True, exist_ok=True)
    store = Store.open(data / "vigil.db")
    budget = Budget(store, cfg.budgets)
    gateway = Gateway(cfg, store, budget, secrets, **gateway_kwargs)
    from vigild.breakers import Breakers

    gateway.breakers = Breakers(store, cfg)  # shared with the runner (item 4)
    off = degraded(secrets)
    telemetry = Telemetry(None if "tracing" in off else cfg.vigild.collector.otlp_http)
    tokens = Tokens(
        admin=secrets.get("VIGIL_API_TOKEN"),
        read=secrets.get("VIGIL_METRICS_TOKEN"),
        heartbeat=secrets.get("VIGIL_HEARTBEAT_TOKEN"),
    )
    jobs = JobService(cfg, store, runner or NullRunner())
    services = Services(cfg=cfg, store=store, tokens=tokens, jobs=jobs, degraded=off)
    for f in sorted(off):
        log.warning("config: %s is off (missing %s)", f, ", ".join(FEATURES[f]))
    return Vigild(cfg, secrets, store, budget, gateway, telemetry, services)


async def serve(v: Vigild, on_ready: Callable[[], Any] | None = None) -> None:
    """Loopback: the bearer-only API and the gateway. The Tailscale address: the
    API with the UI's cookie login (P7). No Tailscale address, no UI login.
    `on_ready` runs (in a thread) once every listener, the gateway's included,
    has started (review round 2, N11)."""
    api = v.cfg.vigild.api
    gw = v.cfg.vigild.gateway
    configs = [
        uvicorn.Config(create_app(v.services), host=api.host, port=api.port, log_level="info"),
        uvicorn.Config(create_gateway_app(v.gateway), host=gw.host, port=gw.port, log_level="info"),
    ]
    if api.tailscale_host:
        configs.append(
            uvicorn.Config(
                create_app(v.services, ui=True),
                host=api.tailscale_host,
                port=api.port,
                log_level="info",
            )
        )
    else:
        log.warning("config: web UI login is off (api.tailscale_host is not set)")
    servers = [uvicorn.Server(c) for c in configs]

    async def ready() -> None:
        while not all(getattr(srv, "started", False) for srv in servers):
            if any(getattr(srv, "should_exit", False) for srv in servers):
                return
            await asyncio.sleep(0.05)
        if on_ready is not None:
            await asyncio.to_thread(on_ready)

    await asyncio.gather(*(srv.serve() for srv in servers), ready())


def _github_app(v: Vigild) -> Any:
    import httpx2

    from vigild.github.app import GitHubApp

    app_id, key_path = v.secrets.get("GITHUB_APP_ID"), v.secrets.get("GITHUB_APP_KEY_PATH")
    if not (app_id and key_path):
        return None
    return GitHubApp(
        app_id,
        Path(key_path).expanduser().read_bytes(),
        http=httpx2.Client(timeout=30),
        claude_mods=v.cfg.vigild.github.claude_mods,
        allowed_repos=list(v.cfg.repos.repos),
    )


def build_runner(v: Vigild, app: Any) -> JobRunner:
    """The real executor, workspace and publisher behind the job service."""
    import docker
    import httpx2

    from vigild.db import utcnow
    from vigild.devimage import DockerImages, SessionImages
    from vigild.executors.runtime import DockerRuntime, DockerTestRunner
    from vigild.github.app import READ_ONLY
    from vigild.jobs.deps import DepsExecutor, HttpRegistry
    from vigild.jobs.runner import GitHubPRs, GitHubPublisher, NoPublisher, Publisher
    from vigild.verifier import Verifier
    from vigild.workspace import Workspace

    client = docker.from_env()

    @contextmanager
    def read_token(repo: str) -> Iterator[str | None]:
        """Mirror fetches use a read-scoped App token, never host credentials (item 22)."""
        with app.lease(repo, dict(READ_ONLY)) as tok:
            yield tok.token

    ws = Workspace(v.cfg.vigild.data_path, token_for=read_token)
    publisher: Publisher = GitHubPublisher(app, app.http, ws) if app else NoPublisher()
    runtime = DockerRuntime(v.cfg, client)
    tests = DockerTestRunner(v.cfg, client)
    images = DevImages(v.cfg, v.store, DockerImages(client), ws, tests)
    gh = v.cfg.vigild.github

    def claude_mods() -> Path:
        dest = v.cfg.vigild.data_path / "claude-mods" / gh.claude_mods_sha
        return ws.export_tree(gh.claude_mods, gh.claude_mods_sha, dest)

    sessions = SessionImages(images, claude_mods)
    runner = JobRunner(
        v.cfg,
        v.store,
        v.budget,
        v.gateway,
        runtime,
        ws,
        publisher,
        v.telemetry,
        images,
        Verifier(ws, tests),
        reaper=runtime.reap,
        session_images=sessions,
        breakers=v.gateway.breakers,
        deps=DepsExecutor(
            ws,
            runtime.run_deps,
            HttpRegistry(httpx2.Client(timeout=30)),
            utcnow,
            v.cfg.vigild.data_path / "deps",
        ),
        pr_state=GitHubPRs(app).state if app else None,
    )
    runner.closers.append(client.close)
    images.on_failure = runner.dev_image_failed
    sessions.on_failure = runner.dev_image_failed
    runner.jobs = v.services.jobs

    def files(repo: str, base: str) -> Callable[[str], str | None]:
        """The base's files for the spec gate, from the mirror, fetched first
        when there's none yet (round 5)."""
        from vigild.verifier import FileNotAvailable

        try:
            if not ws._mirror_path(repo).exists():
                ws.mirror(repo)
        except Exception as e:
            reason = f"the repo's mirror isn't available ({type(e).__name__})"

            def unavailable(path: str) -> str | None:
                raise FileNotAvailable(reason)

            return unavailable
        return lambda path: ws.mirror_file(repo, base, path)

    v.services.jobs.files = files
    return runner


def start_pollers(v: Vigild, app: Any, runner: JobRunner) -> threading.Event:
    """vigild's background loops (plan 2a): label intake for repos that turn
    it on. Each runs in its own daemon thread until the event is set."""
    from vigild.intake.github import LabelIntake, RestIssues, every

    stop = threading.Event()
    sched = v.cfg.vigild.scheduler
    if runner.pr_state is not None:
        # merged and closed fleet PRs: a deps job's merge resumes its parent (item 5)
        threading.Thread(
            target=every, args=(sched.pr_poll_seconds, runner.poll_prs, stop),
            name="vigil-prs", daemon=True,
        ).start()  # fmt: skip
    if app is not None and any(p.intake for p in v.cfg.repos.repos.values()):
        import httpx2

        from vigild.decider.wiring import build_decider

        decider = build_decider(v.cfg, v.store, v.gateway, v.secrets, httpx2.Client(timeout=30))
        intake = LabelIntake(v.cfg, v.store, v.services.jobs, RestIssues(app), decider)
        threading.Thread(
            target=every, args=(sched.intake_poll_seconds, intake.poll, stop),
            name="vigil-intake", daemon=True,
        ).start()  # fmt: skip
    return stop


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from vigild.repos import RepoPolicyError, set_visibility_source, verify_visibility

    v = build()
    app = _github_app(v)
    if app is None:
        raise SystemExit(
            "vigild: the GitHub App isn't configured, so repo visibility can't be "
            "verified; refusing to start (GITHUB_APP_ID, GITHUB_APP_KEY_PATH)"
        )
    try:
        seen = verify_visibility(v.cfg, app.is_private)
    except RepoPolicyError as e:
        raise SystemExit(f"vigild: {e}") from e
    log.info("visibility (live): %s", {r: "private" if p else "public" for r, p in seen.items()})
    # every later read (dispatch, each Jev call) asks GitHub again
    set_visibility_source(app.is_private)
    runner = build_runner(v, app)
    v.services.jobs.runner = runner
    # Hold every job a restart interrupted. Approved builds start only once the
    # gateway is listening (review round 2, N11), and every session checks its
    # repo's dev image first: built and validated when its lockfiles change,
    # never on a timer (the nightly validation is gone).
    log.info("reconcile: %s", runner.reconcile())

    def ready() -> None:
        runner.start_deferred()
        start_pollers(v, app, runner)

    asyncio.run(serve(v, on_ready=ready))
