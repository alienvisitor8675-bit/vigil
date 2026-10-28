"""The `vigil` CLI. Phase 1 ships `doctor` and `dev-image`; `kill` is the first
client of the API; `run`, `batch`, `ls` and `trace` follow in phase 2.

  uv run python -m vigil doctor
  uv run python -m vigil dev-image --repo owner/name [--rebuild]
  uv run python -m vigil hidden show <task>
  uv run python -m vigil backup [--dest DIR] [--keep-days 14]
  uv run python -m vigil restore <backup.db>   (verify only)
  uv run python -m vigil kill <job>
  uv run python cli/vigil mcp                  (stdio MCP server; VIGIL_API_TOKEN in the env)
  uv run python -m vigil batch submit <file.yaml>
  uv run python -m vigil batch status <batch>
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    import httpx2

app = typer.Typer(no_args_is_help=True, add_completion=False)
hidden_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Hidden tests.")
app.add_typer(hidden_app, name="hidden")
batch_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Batches of jobs.")
app.add_typer(batch_app, name="batch")
review_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Review tools.")
app.add_typer(review_app, name="review")


@review_app.command("replay-set")
def review_replay_set(
    prompt_version: str = typer.Option(
        ..., "--prompt-version", help="A git ref of this repo, or the hash of prompts/reviewer.md."
    ),
) -> None:
    """Replay the review regression set (tests/fixtures/review_cases/) with a reviewer
    prompt through the running vigild. Each case is one reviewer session, booked to the
    experiment line (cap $0.40 each); prints recall and the total cost."""
    import httpx2

    from vigild import config
    from vigild.review.replay import render_report

    cfg = config.load()
    admin = config.load_secrets().get("VIGIL_API_TOKEN")
    if not admin:
        typer.echo("the admin API credential is not set")
        raise typer.Exit(1)
    api = cfg.vigild.api
    try:
        r = httpx2.post(
            f"http://{api.host}:{api.port}/api/review/replay-set",
            headers={"Authorization": f"Bearer {admin}"},
            json={"prompt_version": prompt_version},
            timeout=3600,
        )
    except httpx2.HTTPError as e:
        typer.echo(f"vigild is not answering: {type(e).__name__}")
        raise typer.Exit(1) from e
    if r.status_code != 200:
        typer.echo(f"refused ({r.status_code}): {r.json().get('detail', r.text)}"[:500])
        raise typer.Exit(1)
    typer.echo(render_report(r.json()))


spec_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Spec tools.")
app.add_typer(spec_app, name="spec")


@spec_app.command("replay")
def spec_replay(
    job: str = typer.Argument(..., help="a job id, e.g. vi-0002"),
    packet: str = typer.Option("classic", "--packet", help="The coord packet variant."),
) -> None:
    """Re-draft a job's approved spec in one coord session through the running vigild, on
    the experiment line; prints the gate, AC coverage, packet size, tokens and cost."""
    import httpx2

    from vigild import config

    cfg = config.load()
    admin = config.load_secrets().get("VIGIL_API_TOKEN")
    if not admin:
        typer.echo("the admin API credential is not set")
        raise typer.Exit(1)
    api = cfg.vigild.api
    try:
        r = httpx2.post(
            f"http://{api.host}:{api.port}/api/spec/replay",
            headers={"Authorization": f"Bearer {admin}"},
            json={"job_id": job, "packet": packet},
            timeout=3600,
        )
    except httpx2.HTTPError as e:
        typer.echo(f"vigild is not answering: {type(e).__name__}")
        raise typer.Exit(1) from e
    if r.status_code != 200:
        typer.echo(f"refused ({r.status_code}): {r.json().get('detail', r.text)}"[:500])
        raise typer.Exit(1)
    d = r.json()
    typer.echo(f"{d['id']}  job {d['job_id']}  packet {d['packet']}  status {d['status']}")
    gate, cov, t = d.get("gate"), d.get("ac_coverage"), d.get("tokens") or {}
    if gate is not None:
        typer.echo("gate: " + ("ok" if gate["ok"] else "FAILED"))
        for err in gate["errors"]:
            typer.echo(f"  - {err}")
    if cov is not None:
        lost = f"; missing {', '.join(cov['missing'])}" if cov["missing"] else ""
        typer.echo(f"AC coverage: {cov['covered']} of {cov['approved']}{lost}")
    typer.echo(f"packet: {d.get('packet_chars', 0)} chars")
    typer.echo(f"tokens: in {t.get('in', 0)}, cache read {t.get('cache_read', 0)}, cache write "
               f"5m {t.get('cache_write_5m', 0)} / 1h {t.get('cache_write_1h', 0)}, "
               f"out {t.get('out', 0)}")  # fmt: skip
    typer.echo(f"cost: ${d['cost_usd']:.4f}")
    if d.get("error"):
        typer.echo(f"error: {d['error']}")


@hidden_app.command("show")
def hidden_show(task: str = typer.Argument(..., help="a task id, e.g. pa-0002-w2")) -> None:
    """Print a task's hidden tests and record that you saw them. Owner-only, on
    the host: it reads vigild's store (VIGIL_DATA_DIR honoured), not the API.
    Shown to a human, not to a model; pasting them into a session would be."""
    from pathlib import Path

    from vigild import config
    from vigild.db import Store
    from vigild.hidden import HiddenError, show
    from vigild.workspace import Workspace

    cfg = config.load()
    if override := config.env_override("VIGIL_DATA_DIR"):
        cfg.vigild.data_dir = str(Path(override))
    data = cfg.vigild.data_path
    if not (data / "vigil.db").exists():
        typer.echo(f"no vigild data at {data}")
        raise typer.Exit(1)
    store = Store.open(data / "vigil.db")
    try:
        typer.echo(show(store, Workspace(data), task), nl=False)
    except HiddenError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e


def _data_dir(override: str | None) -> Path:
    from vigild import config

    if override:
        return Path(override).expanduser()
    if env := config.env_override("VIGIL_DATA_DIR"):
        return Path(env).expanduser()
    return config.load().vigild.data_path


@app.command()
def backup(
    dest: str = typer.Option(
        "", "--dest", envvar="VIGIL_BACKUP_DIR", help="Outside the repo and the data dir."
    ),
    keep_days: int = typer.Option(14, "--keep-days", min=1, help="Prune older dated sets."),
    data_dir: str = typer.Option("", "--data-dir", help="Default: VIGIL_DATA_DIR or the config."),
) -> None:
    """Back up the database (SQLite online backup, mode 600) and the vault
    (rsync) into a dated set; keep N days. Never reads ~/.config/vigil/."""
    from vigild.backup import BackupError, run_backup

    if not dest:
        typer.echo("no destination: pass --dest or set VIGIL_BACKUP_DIR")
        raise typer.Exit(2)
    try:
        made = run_backup(_data_dir(data_dir or None), Path(dest), keep_days=keep_days)
    except BackupError as e:
        typer.echo(f"backup failed: {e}")
        raise typer.Exit(1) from e
    for w in made.warnings:
        typer.echo(f"warning: {w}")
    typer.echo(f"backed up {made.db.name} and {made.vault.name}")


@app.command()
def restore(
    backup_file: str = typer.Argument(..., help="a vigil-<stamp>.db from `vigil backup`"),
) -> None:
    """Verify a backup (verify only; writes nothing): it opens read-only,
    passes integrity_check and has the expected user_version. To restore, see
    the runbook."""
    from vigild.backup import verify_backup
    from vigild.db import MIGRATIONS

    problems = verify_backup(Path(backup_file))
    if problems:
        typer.echo("backup failed verification: " + "; ".join(problems))
        raise typer.Exit(1)
    typer.echo(f"{Path(backup_file).name} passes integrity_check, user_version {len(MIGRATIONS)}")


@app.command()
def doctor(
    repo: list[str] = typer.Option(  # noqa: B008
        None, "--repo", help="Mint and revoke a test token only for these repos."
    ),
    no_docker: bool = typer.Option(False, "--no-docker", help="Skip the Docker checks."),
) -> None:
    """One health check per credential and dependency: names and pass/fail only."""
    import httpx2

    from vigild import config
    from vigild.doctor import render, run_checks

    cfg = config.load()
    secrets = config.load_secrets()
    docker_client = None
    if not no_docker:
        try:
            import docker

            docker_client = docker.from_env()
        except Exception:
            docker_client = None
    try:
        with httpx2.Client(timeout=30) as http:
            checks = run_checks(
                cfg, secrets, http=http, docker=docker_client, repos=list(repo) if repo else None
            )
    finally:
        if docker_client is not None:
            docker_client.close()
    typer.echo(render(checks))
    raise typer.Exit(0 if all(c.ok is not False for c in checks) else 1)


def _api_client() -> httpx2.Client:
    """A client for vigild's REST API with the admin token: `VIGIL_API_TOKEN` from the
    environment, else from the env file vigild itself reads (`vigild.config`). The URL is
    `VIGIL_API_URL`, else the configured loopback listener."""
    import httpx2

    from vigild import config

    cfg = config.load()
    token = config.env_override("VIGIL_API_TOKEN") or config.load_secrets().get("VIGIL_API_TOKEN")
    if not token:
        typer.echo("no VIGIL_API_TOKEN (set it, or put it in the env file)")
        raise typer.Exit(1)
    base = config.env_override("VIGIL_API_URL") or (
        f"http://{cfg.vigild.api.host}:{cfg.vigild.api.port}"
    )
    # a kill waits for in-flight model calls to drain (30 s) before it answers
    return httpx2.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=120)


@app.command()
def kill(job: str = typer.Argument(..., help="a job id, e.g. pa-0007")) -> None:
    """Stop a job cleanly: its sessions and sidecars, its gateway tokens and its
    reservations, then the job is abandoned. Open PRs stay, for you to close. Killing a
    job that is already finished or killed changes nothing and says so."""
    import httpx2

    with _api_client() as client:
        try:
            r = client.post(f"/api/jobs/{job}/kill", json={"channel": "cli"})
        except httpx2.HTTPError as e:
            typer.echo(f"could not reach vigild: {type(e).__name__}")
            raise typer.Exit(1) from e
    if r.status_code != 200:
        typer.echo(f"kill failed: HTTP {r.status_code} {r.text[:300]}")
        raise typer.Exit(1)
    body = r.json()
    typer.echo(body.get("message") or f"{job}: {body.get('state')}")
    if body.get("killed"):
        typer.echo(
            f"  sessions stopped {len(body.get('sessions_stopped') or [])}, "
            f"tokens revoked {body.get('tokens_revoked', 0)}, "
            f"reservations settled {len(body.get('reservations_settled') or [])}, "
            f"containers removed {body.get('containers_removed', 0)}"
        )
        if not body.get("drained", True):
            typer.echo(
                "  a model call is still in flight; its spend settles when it lands: "
                + ", ".join(body.get("pending_calls") or [])
            )
        if body.get("reservations_open"):
            typer.echo(f"  still open: {body['reservations_open']}")


@app.command("mcp")
def mcp_serve() -> None:
    """Run the MCP server on stdio, for a Claude Code session: tools submit_job,
    job_status, list_inbox, inbox_act and kill over vigild's API. The token is
    VIGIL_API_TOKEN from the environment only; VIGIL_API_URL defaults to the loopback API."""
    from vigil.mcp_server import serve

    serve()


def load_batch_file(path: Path) -> dict[str, Any]:
    """A batch file: `name:` and `jobs:`, a list of what `POST /api/jobs` takes. Raises
    `ValueError` (with the reason) for anything else; vigild checks the jobs themselves."""
    import yaml

    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"cannot read {path}: {type(e).__name__}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("name"), str) or not doc["name"]:
        raise ValueError("a batch file is a mapping with a `name`")
    jobs = doc.get("jobs")
    if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) for j in jobs):
        raise ValueError("`jobs` must be a non-empty list of job requests")
    return {"name": doc["name"], "jobs": jobs}


@batch_app.command("submit")
def batch_submit(file: Annotated[Path, typer.Argument(help="a batch YAML file")]) -> None:
    """Submit a batch: `name:` and `jobs:` (each what POST /api/jobs takes). All jobs are made
    or none; a refusal names the job. Each then waits for its spec and your approval."""
    import httpx2

    try:
        body = load_batch_file(file)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    with _api_client() as client:
        try:
            r = client.post("/api/batch", json=body)
        except httpx2.HTTPError as e:
            typer.echo(f"could not reach vigild: {type(e).__name__}")
            raise typer.Exit(1) from e
    if r.status_code != 202:
        try:
            detail = r.json().get("detail")
            errors = detail.get("errors") if isinstance(detail, dict) else None
        except ValueError:
            errors = None
        typer.echo(f"refused: HTTP {r.status_code}; nothing was created")
        for line in errors or [r.text[:300]]:
            typer.echo(f"  {line}")
        raise typer.Exit(1)
    out = r.json()
    typer.echo(f"batch {out['batch_id']} ({out['name']}): {len(out['jobs'])} jobs")
    for job_id in out["jobs"]:
        typer.echo(f"  {job_id}")


@batch_app.command("status")
def batch_status(batch: str = typer.Argument(..., help="a batch id, e.g. b-0001")) -> None:
    """Each job's state and cost, and the total (spent plus held)."""
    import re
    from urllib.parse import quote

    import httpx2

    if not re.fullmatch(r"b-[0-9]+", batch):
        typer.echo("a batch id looks like b-0001")
        raise typer.Exit(1)
    with _api_client() as client:
        try:
            r = client.get(f"/api/batch/{quote(batch, safe='')}")
        except httpx2.HTTPError as e:
            typer.echo(f"could not reach vigild: {type(e).__name__}")
            raise typer.Exit(1) from e
    if r.status_code != 200:
        typer.echo(f"batch status failed: HTTP {r.status_code} {r.text[:300]}")
        raise typer.Exit(1)
    b = r.json()
    typer.echo(f"batch {b['batch_id']}: {b['name']}")
    for j in b["jobs"]:
        typer.echo(
            f"  {j['job_id']:<10} {j['state']:<14} ${j['total_usd']:.4f}"
            f" (spent {j['spent_usd']:.4f}, held {j['held_usd']:.4f})  {j['title']}"
        )
    c = b["cost"]
    typer.echo(
        f"  total ${c['total_usd']:.4f} (spent {c['spent_usd']:.4f}, held {c['held_usd']:.4f})"
    )


@app.command("dev-image")
def dev_image(
    repo: str = typer.Option(..., "--repo", help="owner/name, as in configs/repos.yaml"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Build again even if validated."),
) -> None:
    """Build and validate a repo's dev image, then its session image (the
    executor layer over it), at its base branch's tip, as the first job would
    (vigild/devimage.py). Prints each suite's outcome."""
    import json

    from vigild.devimage import StaleDevImage
    from vigild.server import _github_app, build, build_runner

    v = build()
    runner = build_runner(v, _github_app(v))
    images = runner.dev_images
    ok = True
    try:
        base = runner.workspace.head_sha(repo, "main")
        tag = images.ensure_fresh(repo, base, force=rebuild)
        typer.echo(f"pass  {repo}  {tag}")
        if runner.session_images is not None:
            session = runner.session_images.ensure_fresh(repo, base, force=rebuild)
            typer.echo(f"pass  {repo}  {session}")
    except StaleDevImage as e:
        ok = False
        typer.echo(f"fail  {repo}  {e}")
    finally:
        runner.close()  # the docker client, or its pool's __del__ writes to a closed stderr
    last = images.latest(repo)
    if last:
        detail = json.loads(last["output"])
        for name, suite in (detail.get("suites") or {}).items():
            typer.echo(f"  {suite['outcome']:<8} {name}")
        for name, why in (detail.get("excluded") or {}).items():
            typer.echo(f"  excluded {name}: {why}")
    raise typer.Exit(0 if ok else 1)
