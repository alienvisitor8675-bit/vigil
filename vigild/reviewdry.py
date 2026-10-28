"""Rerun the reviewer on a past task with no effects (`scripts/review-dry-run.py`).

The given vigild data dir is only read: its database through a `mode=ro` URI, its
vault and mirror through `vigild/gitsafe.py`. A scratch runtime (its own data dir,
database, workspace and gateway) gets a copy of the job, the task, the spec, the
worker's report, the verifier's record and the head commit, then runs the one
reviewer session the way `JobRunner._review_task` does, through the same
`_review_inputs` and `_review_session`. The scratch database is the only place
the run writes; no job or task state changes, no inbox item, no PR, no GitHub.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vigild import config
from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config, Secrets
from vigild.contract.models import ReviewOutput
from vigild.db import Store, utcnow
from vigild.devimage import DevImages, SessionImages
from vigild.executors.base import Executor
from vigild.gateway.service import Gateway
from vigild.gitsafe import git
from vigild.jobs.runner import JobRunner, NoPublisher
from vigild.repos import repo_rule_errors, set_visibility_source
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier
from vigild.workspace import SHA, Workspace

GATEWAY_PORT = 8788  # where containers reach the gateway (the egress sidecar allows it)

# tables copied for the run, by the column that selects the rows
COPIED = ("jobs", "tasks")


class DryRunError(RuntimeError):
    pass


@dataclass
class Wiring:
    """What differs between the real run and a test: the executor, the images,
    and whether the gateway listens on its port."""

    executor: Executor
    images: Callable[[Config, Store, Workspace], tuple[DevImages, SessionImages | None]]
    serve_gateway: bool = True


def open_readonly(data_dir: Path) -> sqlite3.Connection:
    db = data_dir / "vigil.db"
    if not db.is_file():
        raise DryRunError(f"no vigil.db in {data_dir}")
    # immutable: sqlite opens no -wal or -shm, so nothing is created beside the
    # database. Safe because vigild is stopped while the tool runs.
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _insert(store: Store, table: str, rows: list[sqlite3.Row]) -> None:
    for r in rows:
        cols = list(r.keys())
        marks = ",".join("?" for _ in cols)
        store.x(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({marks})",  # noqa: S608
            tuple(r[c] for c in cols),
        )


def _latest_head(src: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = src.execute(
        "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        raise DryRunError(f"task {task_id} has no pr_open event")
    return dict(json.loads(row["data"]))


def seed(src: sqlite3.Connection, store: Store, job_id: str, task_id: str) -> dict[str, Any]:
    """Copy the rows a review reads into the scratch store; return the latest
    `pr_open` event's data."""
    job = src.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchall()
    task = src.execute("SELECT * FROM tasks WHERE id=? AND job_id=?", (task_id, job_id)).fetchall()
    if not job or not task:
        raise DryRunError(f"no task {task_id} in job {job_id}")
    pr = _latest_head(src, task_id)
    store.x("PRAGMA foreign_keys=OFF")
    _insert(store, "jobs", job)
    _insert(store, "tasks", task)
    arts: list[sqlite3.Row] = []
    for name, tid in (("spec.md", None), ("worker_report.json", task_id), ("tests.json", task_id)):
        arts += src.execute(
            "SELECT * FROM artifacts WHERE job_id=? AND name=? AND task_id IS ?"
            " ORDER BY id DESC LIMIT 1",
            (job_id, name, tid),
        ).fetchall()
    _insert(store, "artifacts", arts)
    _insert(store, "dev_images", src.execute("SELECT * FROM dev_images").fetchall())
    return pr


def seed_budget(src: sqlite3.Connection, store: Store, task_id: str) -> None:
    """Copy the production ledger the caps are measured against: this month's
    spend (so today's and the month's), the task's own spend, and every open
    reservation. Read only on the source; the scratch ledger then refuses what
    production would."""
    month = utcnow().strftime("%Y-%m")
    store.x("PRAGMA foreign_keys=OFF")
    for table, where in (
        ("ledger", "month=? OR task_id=?"),
        ("reservations", "status='open' OR month=? OR task_id=?"),
    ):
        rows = src.execute(f"SELECT * FROM {table} WHERE {where}", (month, task_id)).fetchall()  # noqa: S608
        for r in rows:
            cols = [c for c in r.keys() if c != "id"]  # noqa: SIM118
            store.x(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",  # noqa: S608
                tuple(r[c] for c in cols),
            )


def preflight(store: Store, cfg: Config, task_id: str) -> None:
    """Refuse, before any call, a run the production caps would refuse: the
    run's own cap (`review_session_usd`) must fit the day, the month and the
    task, as the seeded ledger stands."""
    amount = cfg.budgets.review_session_usd
    budget = Budget(store, cfg.budgets)
    try:
        rid = budget.reserve(
            session_id=None, task_id=task_id, amount=amount, purpose="dry-run preflight"
        )
    except BudgetExceeded as e:
        raise DryRunError(f"refused before any call: {e}") from e
    store.x("DELETE FROM reservations WHERE id=?", (rid,))


def _import_head(
    data_dir: Path, ws: Workspace, repo: str, task_id: str, head: str, base_sha: str, base: str
) -> None:
    """The head commit from the source vault into the scratch vault (git reads
    the source; nothing is written there), and the base record `_review_inputs`
    reads."""
    owner, name = repo.split("/")
    src_vault = data_dir / "vault" / owner / f"{name}.git"
    if not src_vault.is_dir():
        raise DryRunError(f"no vault for {repo} in {data_dir}")
    vault = ws.vault(repo)
    git(
        vault, "-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "--quiet", "--no-tags",
        str(src_vault), head,
    )  # fmt: skip
    git(vault, "update-ref", f"refs/vigil/{task_id}", head)
    rec = ws.data_dir / "clones" / f"{task_id}.base"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({"repo": repo, "base": base, "base_sha": base_sha}))


def _serve_gateway(gateway: Gateway) -> Callable[[], None]:
    import uvicorn

    from vigild.gateway.app import create_gateway_app

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", GATEWAY_PORT)) == 0:
            raise DryRunError(f"port {GATEWAY_PORT} is in use: stop vigild first")
    server = uvicorn.Server(
        uvicorn.Config(
            create_gateway_app(gateway), host="127.0.0.1", port=GATEWAY_PORT, log_level="warning"
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)

    def stop() -> None:
        server.should_exit = True
        t.join(timeout=10)

    return stop


def docker_wiring(cfg: Config) -> Wiring:
    """The real runtime: Docker containers and the repo's built session image."""
    import docker

    from vigild.devimage import DockerImages
    from vigild.executors.runtime import DockerRuntime, DockerTestRunner

    client = docker.from_env()

    def images(c: Config, store: Store, ws: Workspace) -> tuple[DevImages, SessionImages | None]:
        dev = DevImages(c, store, DockerImages(client), ws, DockerTestRunner(c, client))

        def claude_mods() -> Path:
            raise DryRunError("the session image isn't built yet: start vigild once to build it")

        return dev, SessionImages(dev, claude_mods)

    return Wiring(DockerRuntime(cfg, client), images)


def dry_run(
    *,
    data_dir: Path,
    job_id: str,
    task_id: str,
    head: str | None,
    out: Path,
    cfg: Config | None = None,
    secrets: Secrets | None = None,
    scratch: Path | None = None,
    private: bool = True,
    wiring: Wiring | None = None,
) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    out = out.expanduser().resolve()
    scratch = (scratch or Path(tempfile.mkdtemp(prefix="vigil-review-dry-"))).resolve()
    for p in (scratch, out):
        if p == data_dir or data_dir in p.parents:
            raise DryRunError(f"{p} is inside the data dir; the dry run never writes there")
    if head is not None and not SHA.match(head):
        raise DryRunError("--head must be a full 40-hex commit sha")
    src = open_readonly(data_dir)
    try:
        return _run(src, data_dir, job_id, task_id, head, out, cfg, secrets, scratch, private,
                    wiring)  # fmt: skip
    finally:
        src.close()


def _run(
    src: sqlite3.Connection,
    data_dir: Path,
    job_id: str,
    task_id: str,
    head: str | None,
    out: Path,
    cfg: Config | None,
    secrets: Secrets | None,
    scratch: Path,
    private: bool,
    wiring: Wiring | None,
) -> dict[str, Any]:
    cfg = (cfg or config.load()).model_copy(deep=True)
    cfg.vigild.data_dir = str(scratch)
    if secrets is None:
        secrets = config.load_secrets(config.DEFAULT_ENV_FILE)
    scratch.mkdir(parents=True, exist_ok=True)
    store = Store.open(scratch / "vigil.db")
    pr = seed(src, store, job_id, task_id)
    job = store.get_job(job_id)
    if not private and not repo_rule_errors(cfg, job.repo, True):
        raise DryRunError(
            f"{job.repo} lists only private-safe backends in configs/repos.yaml (private-only):"
            " --repo-visibility public is refused"
        )
    seed_budget(src, store, task_id)
    preflight(store, cfg, task_id)
    head = head or str(pr["head"])
    base_sha = str(pr.get("base_sha") or "")
    if not SHA.match(head) or not SHA.match(base_sha):
        raise DryRunError("the pr_open event names no base sha; pass --head and fix the record")
    # the source mirror is the scratch mirror's remote: a local clone, never GitHub
    owner, name = job.repo.split("/")
    mirror = data_dir / "mirrors" / owner / f"{name}.git"
    if not mirror.is_dir():
        raise DryRunError(f"no mirror for {job.repo} in {data_dir}")
    ws = Workspace(scratch, remote_url=lambda r: str(mirror))
    ws.mirror(job.repo)
    _import_head(data_dir, ws, job.repo, task_id, head, base_sha, job.base)

    wiring = wiring or docker_wiring(cfg)
    budget = Budget(store, cfg.budgets)
    gateway = Gateway(cfg, store, budget, secrets)
    from vigild.breakers import Breakers

    gateway.breakers = Breakers(store, cfg)
    images, session_images = wiring.images(cfg, store, ws)
    runner = JobRunner(
        cfg, store, budget, gateway, wiring.executor, ws, NoPublisher(), Telemetry(None), images,
        Verifier(ws, None),  # type: ignore[arg-type]
        session_images=session_images, background=False, breakers=gateway.breakers,
    )  # fmt: skip
    stop = _serve_gateway(gateway) if wiring.serve_gateway else (lambda: None)
    set_visibility_source(lambda repo: private)  # asked of the caller, never of GitHub
    try:
        t = store.get_task(task_id)
        ri = runner._review_inputs(job, t, head)
        review, why = runner._review_session(job, t, head, ri)
    finally:
        set_visibility_source(None)
        stop()
    cost = float(store.q("SELECT COALESCE(SUM(cost_usd),0) AS c FROM model_calls")[0]["c"])
    # (the model_calls table starts empty in the scratch database: this is this run's)
    result: dict[str, Any] = {
        "job": job_id,
        "task": task_id,
        "head": head,
        "base_sha": ri.base_sha,
        "round": ri.rnd,
        "reviewer": ri.reviewer,
        "cost_usd": round(cost, 6),
        "run_cap_usd": cfg.budgets.review_session_usd,
        "cost_recorded_in_production": False,
        "scratch": str(scratch),
        "error": why or None,
        "verdict": None,
        "findings": [],
        "checklist": None,
    }
    if review is not None:
        assert isinstance(review, ReviewOutput)
        result.update(review.model_dump(mode="json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def _cost_line(result: dict[str, Any]) -> str:
    return (
        f"COST ${result['cost_usd']:.4f}: NOT recorded in the production ledger; "
        "add it to your records"
    )


def render(result: dict[str, Any]) -> str:
    lines = [
        f"review of {result['task']} at {result['head'][:12]} (round {result['round']}, "
        f"{result['reviewer']}, ${result['cost_usd']:.4f})"
    ]
    if result["error"]:
        lines.append(f"no valid review: {result['error']}")
        lines.append(_cost_line(result))
        return "\n".join(lines)
    lines.append(f"verdict: {result['verdict']}")
    for f in result["findings"]:
        lines.append(f"- [{f['severity']}] {f['file']}:{f['line']} {f['claim']} -> {f['fix']}")
    if not result["findings"]:
        lines.append("(no findings)")
    lines.append("checklist: " + json.dumps(result["checklist"], sort_keys=True))
    lines.append(_cost_line(result))
    return "\n".join(lines)
