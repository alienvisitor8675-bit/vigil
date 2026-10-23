"""The `vigil` CLI. Phase 1 ships `doctor` and `dev-image`; `run`, `batch`,
`ls`, `trace` and `kill` arrive in phase 2 as clients of the API.

  uv run python -m vigil doctor
  uv run python -m vigil dev-image --repo owner/name [--rebuild]
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


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
