"""The `vigil` CLI. Phase 1 ships `doctor` and `dev-image`; `run`, `batch`,
`ls`, `trace` and `kill` arrive in phase 2 as clients of the API.

  uv run python -m vigil doctor
  uv run python -m vigil dev-image --repo owner/name [--rebuild]
  uv run python -m vigil hidden show <task>
  uv run python -m vigil backup [--dest DIR] [--keep-days 14]
  uv run python -m vigil restore <backup.db> [--no-dry-run --target DIR]
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)
hidden_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Hidden tests.")
app.add_typer(hidden_app, name="hidden")


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


def _vigild_listening() -> bool:
    import socket

    from vigild import config

    try:
        bind = config.load().vigild.api
        with socket.create_connection((bind.host, bind.port), timeout=1):
            return True
    except Exception:
        return False


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
    typer.echo(f"backed up {made.db.name} and {made.vault.name}")


@app.command()
def restore(
    backup_file: str = typer.Argument(..., help="a vigil-<stamp>.db from `vigil backup`"),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Default: check the backup, write nothing."
    ),
    target: str = typer.Option("", "--target", help="Data dir to restore into (empty dir)."),
    allow_live: bool = typer.Option(False, "--allow-live", help="Permit the live data dir."),
    data_dir: str = typer.Option("", "--data-dir", help="The live data dir, for the guard."),
) -> None:
    """Verify a backup opens read-only, passes integrity_check and has the
    expected user_version. With --no-dry-run --target DIR, restore it; refuses
    while vigild runs, and the live data dir without --allow-live."""
    from vigild.backup import BackupError
    from vigild.backup import restore as do_restore

    live = _data_dir(data_dir or None)
    if not dry_run and not target:
        typer.echo("a real restore needs --target")
        raise typer.Exit(2)
    try:
        lines = do_restore(
            Path(backup_file),
            Path(target) if target else live,
            live_dir=live,
            dry_run=dry_run,
            allow_live=allow_live,
            running=_vigild_listening,
        )
    except BackupError as e:
        typer.echo(f"restore refused: {e}")
        raise typer.Exit(1) from e
    for line in lines:
        typer.echo(line)


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
