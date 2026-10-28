"""vigild's SQLite store (B2): stdlib sqlite3, WAL, one writer.

Every table that holds an artifact or a record of work carries a
`provenance` column constrained to claude | local | human | eval-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vigild.jobs.model import (
    MAX_FIX_DEPTH,
    ArtifactRow,
    EventRow,
    FixDepthExceeded,
    InboxItem,
    Job,
    JobState,
    Role,
    SessionRow,
    TaskRow,
)
from vigild.provenance import Provenance, require
from vigild.redact import redact
from vigild.repos import is_bench_repo

log = logging.getLogger(__name__)

PROV = "provenance TEXT NOT NULL CHECK (provenance IN ('claude','local','human','eval-only'))"

# Phase 2a: each new table's DDL is a constant, used by SCHEMA (a new
# database) and by its migration step (an old one).
LADDER_DDL = f"""CREATE TABLE IF NOT EXISTS ladder (
  shape TEXT PRIMARY KEY, rung INTEGER NOT NULL, window_from INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, task_id TEXT NOT NULL,
  shape TEXT NOT NULL, rung INTEGER NOT NULL, verify_level INTEGER NOT NULL,
  match_rate REAL NOT NULL, missed_high INTEGER NOT NULL, missed_medium INTEGER NOT NULL,
  false_findings INTEGER NOT NULL, ts TEXT NOT NULL, {PROV})"""

INTAKE_DDL = """CREATE TABLE IF NOT EXISTS intake_seen (
  repo TEXT NOT NULL, number INTEGER NOT NULL, job_id TEXT, seen_at TEXT NOT NULL,
  PRIMARY KEY (repo, number))"""

TRANSCRIPT_DDL = f"""CREATE TABLE IF NOT EXISTS transcripts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, sdk_session_id TEXT NOT NULL,
  subpath TEXT, entry TEXT NOT NULL, ts TEXT NOT NULL, {PROV});
CREATE INDEX IF NOT EXISTS transcripts_session ON transcripts(session_id, id)"""

BREAKER_DDL = """CREATE TABLE IF NOT EXISTS breakers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, scope TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('closed','open','half_open')), count INTEGER NOT NULL,
  item_id INTEGER, held TEXT NOT NULL, detail TEXT NOT NULL, reopen_at TEXT,
  updated_at TEXT NOT NULL, UNIQUE (kind, scope))"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, base TEXT NOT NULL, title TEXT NOT NULL,
  request TEXT NOT NULL, notes TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
  blocked_from TEXT, parent_job_id TEXT REFERENCES jobs(id), fix_depth INTEGER NOT NULL,
  coord_session_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, {PROV});
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), contract TEXT NOT NULL,
  state TEXT NOT NULL, tier TEXT, tier_reason TEXT, backend_preset TEXT,
  created_at TEXT NOT NULL, {PROV});
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), task_id TEXT REFERENCES tasks(id),
  role TEXT NOT NULL, parent_session_id TEXT REFERENCES sessions(id), backend_preset TEXT,
  status TEXT NOT NULL, container_id TEXT, branch TEXT, pr_url TEXT, clone_path TEXT,
  cost_usd REAL NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0, started_at TEXT, ended_at TEXT,
  created_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{{}}', {PROV});
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, session_id TEXT, task_id TEXT,
  ts TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, {PROV});
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, task_id TEXT, name TEXT NOT NULL,
  content TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL, {PROV});
CREATE TABLE IF NOT EXISTS inbox_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, queue TEXT NOT NULL
    CHECK (queue IN ('specs','ready','questions')),
  kind TEXT NOT NULL, job_id TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, resolved_at TEXT,
  resolved_by TEXT, {PROV});
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, inbox_item_id INTEGER NOT NULL,
  actor TEXT NOT NULL, channel TEXT NOT NULL, edits TEXT, ts TEXT NOT NULL, {PROV});
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, task_id TEXT, job_id TEXT,
  purpose TEXT NOT NULL, provider TEXT NOT NULL, backend_preset TEXT, model TEXT NOT NULL,
  tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, tokens_cache_read INTEGER NOT NULL,
  tokens_cache_write_5m INTEGER NOT NULL, tokens_cache_write_1h INTEGER NOT NULL,
  cost_usd REAL NOT NULL, provider_cost_usd REAL, status INTEGER, latency_ms INTEGER,
  timings TEXT, ts TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0, {PROV});
CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, task_id TEXT, purpose TEXT NOT NULL,
  amount_usd REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ('open','settled','released')),
  day TEXT NOT NULL, month TEXT NOT NULL, created_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id INTEGER, session_id TEXT, task_id TEXT,
  amount_usd REAL NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, task_id TEXT, question TEXT NOT NULL,
  answer TEXT NOT NULL, probability REAL NOT NULL, backend TEXT NOT NULL,
  model_version TEXT, action TEXT NOT NULL, ts TEXT NOT NULL, {PROV});
CREATE TABLE IF NOT EXISTS gateway_tokens (
  token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL, repo TEXT NOT NULL,
  backend_preset TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  expires_at TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS dev_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, image TEXT NOT NULL,
  image_id TEXT, ok INTEGER NOT NULL, output TEXT NOT NULL, validated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_sessions (
  id_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, revoked_at TEXT);
{LADDER_DDL};
{INTAKE_DDL};
{TRANSCRIPT_DDL};
{BREAKER_DDL};
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
CREATE INDEX IF NOT EXISTS calls_session ON model_calls(session_id);
CREATE INDEX IF NOT EXISTS res_day ON reservations(day, status);
"""

ID_PREFIX = re.compile(r"[^a-z0-9]")


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _m1_gateway_token_binding(conn: sqlite3.Connection) -> None:
    """Review round 1: tokens carry the session's model and an expiry."""
    _add_columns(
        conn, "gateway_tokens", {"model": "TEXT NOT NULL DEFAULT ''", "expires_at": "TEXT"}
    )


# Ordered steps (review round 2, N8). migrate() runs all of them on every start,
# so each must be idempotent (SCHEMA's CREATE IF NOT EXISTS may already have
# made a table in its newest shape). Append only; never reorder or renumber.
def _m2_dev_image_keys(conn: sqlite3.Connection) -> None:
    """Review round 2: dev images are built per lockfile key and validated by stage."""
    _add_columns(conn, "dev_images", {"key": "TEXT", "stage": "TEXT", "base_sha": "TEXT"})


def _m3_job_blocked_on(conn: sqlite3.Connection) -> None:
    """Round 3: a job blocked on a dependency names the deps job it waits for."""
    _add_columns(conn, "jobs", {"blocked_on": "TEXT"})


def _m4_call_truncated(conn: sqlite3.Connection) -> None:
    """A call that stopped at max_tokens is recorded as truncated."""
    _add_columns(conn, "model_calls", {"truncated": "INTEGER NOT NULL DEFAULT 0"})


def _scrub(text: str) -> str:
    """`text` with `redact` applied. JSON is redacted as a value and written back
    the way `_j` writes it; text that isn't JSON is redacted as it stands. A text
    that is already clean comes back byte-identical."""
    try:
        value = json.loads(text)
    except ValueError:
        return str(redact(text))
    clean = redact(value)
    return text if clean == value else _j(clean)


def _m5_scrub_private_keys(conn: sqlite3.Connection) -> None:
    """Phase 1 run, finding #1: a PEM private key reached the inbox, the event log
    and `tests.json` unredacted (`redact` had no PEM pattern). Re-apply it to what
    is stored; rows it leaves alone are not rewritten, and a status is not touched."""
    for table, col, where in (
        ("inbox_items", "body", ""),
        ("events", "data", ""),
        ("artifacts", "content", " WHERE name='tests.json'"),
    ):
        for row in conn.execute(f"SELECT rowid, {col} FROM {table}{where}").fetchall():  # noqa: S608
            clean = _scrub(row[1])
            if clean == row[1]:
                continue
            extra = ", sha256=?" if table == "artifacts" else ""
            args = [clean, hashlib.sha256(clean.encode()).hexdigest()] if extra else [clean]
            conn.execute(f"UPDATE {table} SET {col}=?{extra} WHERE rowid=?", (*args, row[0]))  # noqa: S608


def _create(conn: sqlite3.Connection, ddl: str) -> None:
    """CREATE statements one at a time (executescript would commit the
    migration's transaction)."""
    for stmt in filter(str.strip, ddl.split(";")):
        conn.execute(stmt)


def _m6_trust_ladder(conn: sqlite3.Connection) -> None:
    """Phase 2a, item 1: the ladder's live rungs and the audits that move them."""
    _create(conn, LADDER_DDL)


def _m7_label_intake(conn: sqlite3.Connection) -> None:
    """Phase 2a, item 2: each labelled issue becomes a job exactly once."""
    _create(conn, INTAKE_DDL)


def _m8_transcripts(conn: sqlite3.Connection) -> None:
    """Phase 2a, items 2-3: every session's transcript, for resume and forks."""
    _create(conn, TRANSCRIPT_DDL)


def _m9_breakers(conn: sqlite3.Connection) -> None:
    """Phase 2a, item 4: one table for every circuit breaker."""
    _create(conn, BREAKER_DDL)


JEV_DRIFT_DDL = """CREATE TABLE IF NOT EXISTS jev_drift (
  route TEXT NOT NULL, version TEXT NOT NULL, pin TEXT NOT NULL, item_id INTEGER,
  first_seen TEXT NOT NULL, PRIMARY KEY (route, version))"""


def _m10_live_visibility(conn: sqlite3.Connection) -> None:
    """Owner, 2026-10-23: a session's token keeps the visibility read live at
    dispatch; each Jev version drift pages once."""
    _add_columns(conn, "gateway_tokens", {"private": "INTEGER"})
    _create(conn, JEV_DRIFT_DDL)


# A provenance column added to an existing table needs a default: these rows
# are vigild's own records, which it tags `human` like its events.
PROV_ADDED = (
    "TEXT NOT NULL DEFAULT 'human' CHECK (provenance IN ('claude','local','human','eval-only'))"
)

LADDER_CHANGES_DDL = f"""CREATE TABLE IF NOT EXISTS ladder_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shape TEXT NOT NULL, old_rung INTEGER NOT NULL,
  new_rung INTEGER NOT NULL, reason TEXT NOT NULL, sample_size INTEGER NOT NULL,
  match_rate REAL, ts TEXT NOT NULL, {PROV});
CREATE INDEX IF NOT EXISTS ladder_changes_shape ON ladder_changes(shape, id)"""


def _m11_round2(conn: sqlite3.Connection) -> None:
    """2a round 2: provenance on the four phase 2a tables (review B14); the
    repo on every model call, for the per-repo fallback cap (owner ruling 4);
    each decision's question type (ruling 6); one row per rung change
    (ruling 1)."""
    for table in ("ladder", "intake_seen", "breakers", "jev_drift"):
        _add_columns(conn, table, {"provenance": PROV_ADDED})
    _add_columns(conn, "model_calls", {"repo": "TEXT"})
    _add_columns(conn, "decisions", {"kind": "TEXT NOT NULL DEFAULT ''"})
    _create(conn, LADDER_CHANGES_DDL)


def _m12_issue_link(conn: sqlite3.Connection) -> None:
    """A job's GitHub issue, as a column (owner, 2026-10-27): intake asks "does a
    live job already carry this issue?" in one indexed query, which an artifact
    could not answer. Jobs from before carry none (NULL); text is never parsed.
    The partial unique index is the guard: one job per (repo, issue) that isn't
    abandoned, enforced by the database, so a race or a second API call cannot
    make two. (Merged and done jobs count: follow-up work is a new job.)"""
    _add_columns(conn, "jobs", {"issue": "INTEGER"})
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS jobs_live_issue ON jobs(repo, issue)"
        " WHERE issue IS NOT NULL AND state != 'abandoned'"
    )


OLD_PROV_CHECK = "'claude','local','human')"
NEW_PROV_CHECK = "'claude','local','human','eval-only')"


def _needs_widening(conn: sqlite3.Connection) -> list[sqlite3.Row | tuple[str, str]]:
    """The tables whose stored CREATE statement still has the old provenance CHECK."""
    return conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='table' AND sql LIKE ?",
        (f"%{OLD_PROV_CHECK}%",),
    ).fetchall()


def _db_file(conn: sqlite3.Connection) -> str:
    return next((str(r[2]) for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")


def _backup_before_rewrite(conn: sqlite3.Connection, tag: str) -> Path | None:
    """`VACUUM INTO '<db>.<tag>-<utc stamp>.bak'` beside a file-backed database
    (never in the repo), mode 600, before a schema-rewriting migration. None for
    `:memory:`, which has nothing to restore."""
    file = _db_file(conn)
    if not file:
        log.info("no backup before %s: the database is in memory", tag)
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = f"{file}.{tag}-{stamp}.bak"
    old = os.umask(0o077)  # created private: no window with default permissions
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        os.umask(old)
    os.chmod(dest, 0o600)
    log.warning("about to rewrite the schema (%s); backup at %s", tag, dest)
    return Path(dest)


def _m13_eval_only(conn: sqlite3.Connection) -> None:
    """Bench 1a: `eval-only` joins the provenance CHECK (owner, 2026-10-27).

    SQLite can't alter a CHECK in place. The 12-step rebuild (new table, copy, drop,
    rename) would have to run for sixteen tables, three of them referenced by foreign
    keys (jobs, tasks, sessions), and it needs `foreign_keys=OFF`, which SQLite
    ignores inside the transaction every step runs in. A new column would leave the
    CHECK-bound `provenance` saying something else than the row's real tag. So this
    uses SQLite's documented in-place route for loosening a constraint
    (lang_altertable.html, "Making Other Kinds Of Table Schema Changes"): edit the
    stored CREATE statement of each table, bump `schema_version`, and check the
    result. Widening a CHECK can't make an existing row invalid, so no row is
    rewritten. Idempotent: a table whose statement already lists `eval-only` is not
    touched, and the SQL replaced is the exact text SCHEMA and PROV_ADDED write."""
    rows = _needs_widening(conn)
    if not rows:
        return
    version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema=ON")
    try:
        for name, sql in rows:
            conn.execute(
                "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name=?",
                (sql.replace(OLD_PROV_CHECK, NEW_PROV_CHECK), name),
            )
    finally:
        conn.execute("PRAGMA writable_schema=OFF")
    # after the edit, so every connection (and this one) re-reads the schema
    conn.execute(f"PRAGMA schema_version={version + 1}")
    problems = conn.execute("PRAGMA integrity_check").fetchall()
    if [tuple(r) for r in problems] != [("ok",)]:
        raise sqlite3.DatabaseError(f"integrity_check after widening provenance: {problems}")
    refused = probe_eval_only(conn)
    if refused:  # raising rolls the step's transaction back: the old schema stays
        raise sqlite3.DatabaseError(f"provenance CHECK still refuses 'eval-only' in: {refused}")


def _provenance_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND sql LIKE '%provenance%'"
            " AND sql LIKE '%CHECK (provenance IN (%'"
        )
    ]


def probe_eval_only(conn: sqlite3.Connection, kinds: dict[str, str] | None = None) -> list[str]:
    """The tables (those with a provenance CHECK) that refuse an `eval-only` row.
    Each gets one inserted inside a savepoint that is rolled back, so nothing
    stays. Must run inside a transaction. Other constraints are met with dummy
    values (foreign keys deferred). Only an IntegrityError naming a failed CHECK on
    provenance is a refusal (`kinds[table] = 'check'`). Any other error is
    inconclusive: the table counts as refused only if its stored CHECK lacks
    `eval-only` (`kinds[table] = 'schema'`), and a warning is logged either way.
    (Owner, 2026-10-27: a dummy value that clashes, say on a UNIQUE column, must
    not block a start.)"""
    conn.execute("PRAGMA defer_foreign_keys=ON")
    refused: list[str] = []
    for table in _provenance_tables(conn):
        sql = str(
            conn.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
        )
        # another column's `CHECK (col IN ('a', ...))` is met with its first value
        enums = dict(re.findall(r"CHECK \((\w+) IN \('([^']*)'", sql))
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        pks = sum(1 for c in cols if c[5])
        names: list[str] = []
        vals: list[object] = []
        for c in cols:
            name, typ, notnull, default, pk = c[1], str(c[2]).upper(), c[3], c[4], c[5]
            if name == "provenance":
                names.append(name)
                vals.append("eval-only")
            elif notnull and default is None and not (pk and pks == 1 and "INT" in typ):
                names.append(name)
                if name in enums:
                    vals.append(enums[name])
                elif "INT" in typ or "REAL" in typ:
                    vals.append(0)
                else:
                    vals.append("{}" if name == "data" else "x")
        conn.execute("SAVEPOINT probe")
        try:
            insert = (
                f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" * len(names))})'  # noqa: S608
            )
            conn.execute(insert, vals)
        except sqlite3.Error as e:
            msg = str(e).lower()
            if (
                isinstance(e, sqlite3.IntegrityError)
                and "check constraint failed" in msg
                and "provenance" in msg
            ):
                refused.append(table)  # a real provenance refusal
                if kinds is not None:
                    kinds[table] = "check"
            elif NEW_PROV_CHECK not in sql:
                log.warning("eval-only probe of %s inconclusive (%s); its CHECK lacks it", table, e)
                refused.append(table)  # inconclusive, and the stored CHECK lacks it
                if kinds is not None:
                    kinds[table] = "schema"
            else:  # inconclusive (a dummy value clashed): the stored CHECK lists it
                log.warning("eval-only probe of %s inconclusive (%s); its CHECK lists it", table, e)
        finally:
            conn.execute("ROLLBACK TO probe")
            conn.execute("RELEASE probe")
    return refused


def verify_fresh_connection(path: str, kinds: dict[str, str] | None = None) -> list[str]:
    """After m13 commits: a second, new connection (its own view of the schema,
    nothing cached) must be able to store `eval-only` in every provenance table."""
    fresh = sqlite3.connect(path, isolation_level=None)
    try:
        fresh.execute("BEGIN")
        try:
            return probe_eval_only(fresh, kinds)
        finally:
            fresh.execute("ROLLBACK")
    finally:
        fresh.close()


def _m14_experiment_pool(conn: sqlite3.Connection) -> None:
    """Bench 1a: a reservation and a ledger row say which pool they draw from, the
    operating budget (`ops`) or the experiment line (`experiment`)."""
    for table in ("reservations", "ledger"):
        _add_columns(conn, table, {"pool": "TEXT NOT NULL DEFAULT 'ops'"})


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m1_gateway_token_binding,
    _m2_dev_image_keys,
    _m3_job_blocked_on,
    _m4_call_truncated,
    _m5_scrub_private_keys,
    _m6_trust_ladder,
    _m7_label_intake,
    _m8_transcripts,
    _m9_breakers,
    _m10_live_visibility,
    _m11_round2,
    _m12_issue_link,
    _m13_eval_only,
    _m14_experiment_pool,
]


def migrate(conn: sqlite3.Connection) -> int:
    """Run every step, every start. `user_version` is informational: another
    branch's build may have raised it past steps this build has never run, so
    it never decides to skip one. Each step is idempotent, so a re-run is free."""
    for step in MIGRATIONS:
        if step is _m13_eval_only:
            if _needs_widening(conn):
                _backup_before_rewrite(conn, "pre-m13")  # not inside the step's tx
            else:
                log.info("no backup before migration 13: no table needs rewriting")
        conn.execute("BEGIN IMMEDIATE")
        try:
            step(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    # Every start, not only the one that made the backup (owner, 2026-10-27): a new
    # connection must store `eval-only` in every provenance table, inside a transaction
    # it rolls back. Fails closed: names the tables and the backup, repairs nothing.
    file = _db_file(conn)
    kinds: dict[str, str] = {}
    refused = verify_fresh_connection(file, kinds) if file else []
    if refused:
        baks = sorted(Path(file).parent.glob(f"{Path(file).name}.pre-m13-*.bak"))
        if "check" not in kinds.values():  # no real provenance refusal: no backup to name
            where = "the probe was inconclusive and the stored CHECK lacks 'eval-only'"
        else:
            where = f"restore the backup {baks[-1]}" if baks else "no pre-m13 backup is beside it"
        raise sqlite3.DatabaseError(
            f"a new connection can't store 'eval-only' in {refused} of {file}; vigild won't"
            f" start and repairs nothing: {where}"
        )
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    return len(MIGRATIONS)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


class SessionRoleError(ValueError):
    """A session was asked to do a job its recorded role forbids."""


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.lock = threading.RLock()

    @classmethod
    def open(cls, path: str | Path) -> Store:
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        migrate(conn)
        return cls(conn)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, args))

    def x(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return int(cur.lastrowid or 0)

    # --- bench provenance (bench 1a) ------------------------------------------------------

    def is_bench(
        self,
        *,
        job_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> bool:
        """Whether a record belongs to a bench job, found through whichever of the
        job, its session or its task the caller has."""
        if job_id is None and session_id is not None:
            rows = self.q("SELECT job_id FROM sessions WHERE id=?", (session_id,))
            job_id = rows[0][0] if rows else None
        if job_id is None and task_id is not None:
            rows = self.q("SELECT job_id FROM tasks WHERE id=?", (task_id,))
            job_id = rows[0][0] if rows else None
        if job_id is None:
            return False
        rows = self.q("SELECT source FROM jobs WHERE id=?", (job_id,))
        return bool(rows) and rows[0][0] == "bench"

    def row_provenance(
        self,
        provenance: Provenance | str,
        *,
        job_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """The tag a row is stored with: every row of a bench job is `eval-only`,
        whatever made it (the one place that decides), else the caller's own."""
        if self.is_bench(job_id=job_id, session_id=session_id, task_id=task_id):
            return Provenance.EVAL_ONLY.value
        return require(provenance).value

    # --- jobs ---------------------------------------------------------------

    def create_job(
        self,
        *,
        repo: str,
        base: str,
        title: str,
        request: str,
        notes: list[str],
        source: str = "human",
        parent_job_id: str | None = None,
        provenance: Provenance | str = Provenance.HUMAN,
        issue: int | None = None,
    ) -> Job:
        if (source == "bench") != is_bench_repo(repo):
            raise ValueError(
                f"a bench job lives on a bench/ repo and only there: {source!r} on {repo!r}"
            )
        if source == "bench":
            provenance = Provenance.EVAL_ONLY
        depth = 0
        if parent_job_id:
            depth = self.get_job(parent_job_id).fix_depth + 1
            if depth > MAX_FIX_DEPTH:
                raise FixDepthExceeded(f"fix-job depth {depth} exceeds {MAX_FIX_DEPTH}")
        prefix = ID_PREFIX.sub("", repo.split("/")[-1].lower())[:2] or "jb"
        now = iso()
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE ?", (f"{prefix}-%",)).fetchone()
            job_id = f"{prefix}-{int(n[0]) + 1:04d}"
            c.execute(
                "INSERT INTO jobs (id, repo, base, title, request, notes, source, state,"
                " blocked_from, parent_job_id, fix_depth, coord_session_id, created_at,"
                " updated_at, provenance, issue) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    repo,
                    base,
                    title,
                    request,
                    _j(notes),
                    source,
                    JobState.SPEC_PENDING.value,
                    None,
                    parent_job_id,
                    depth,
                    None,
                    now,
                    now,
                    require(provenance).value,
                    issue,
                ),
            )
        return self.get_job(job_id)

    def live_job_for_issue(self, repo: str, issue: int) -> str | None:
        """The id of the job of `repo` holding GitHub issue `issue` (any state but
        abandoned), if there is one; the unique index `jobs_live_issue` allows one."""
        rows = self.q(
            "SELECT id FROM jobs WHERE repo=? AND issue=? AND state != 'abandoned'", (repo, issue)
        )
        return str(rows[0]["id"]) if rows else None

    def jobs_for_issue(self, repo: str, issue: int) -> list[Job]:
        """Jobs of `repo` that carry GitHub issue `issue`, oldest first."""
        rows = self.q("SELECT id FROM jobs WHERE repo=? AND issue=? ORDER BY id", (repo, issue))
        return [self.get_job(r["id"]) for r in rows]

    def get_job(self, job_id: str) -> Job:
        rows = self.q("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            raise KeyError(job_id)
        r = rows[0]
        return Job(
            id=r["id"],
            repo=r["repo"],
            base=r["base"],
            title=r["title"],
            request=r["request"],
            notes=json.loads(r["notes"]),
            source=r["source"],
            state=JobState(r["state"]),
            blocked_from=JobState(r["blocked_from"]) if r["blocked_from"] else None,
            parent_job_id=r["parent_job_id"],
            fix_depth=r["fix_depth"],
            coord_session_id=r["coord_session_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            provenance=r["provenance"],
            blocked_on=r["blocked_on"],
            issue=r["issue"],
        )

    def list_jobs(self) -> list[Job]:
        return [self.get_job(r["id"]) for r in self.q("SELECT id FROM jobs ORDER BY created_at")]

    def child_jobs(self, job_id: str) -> list[str]:
        rows = self.q("SELECT id FROM jobs WHERE parent_job_id=? ORDER BY id", (job_id,))
        return [r["id"] for r in rows]

    def _set_blocked_on(self, job_id: str, blocker: str | None) -> None:
        """Only vigild.jobs.fsm calls this."""
        self.x("UPDATE jobs SET blocked_on=? WHERE id=?", (blocker, job_id))

    def _write_state(self, job_id: str, state: JobState, blocked_from: JobState | None) -> None:
        """Only vigild.jobs.fsm calls this."""
        self.x(
            "UPDATE jobs SET state=?, blocked_from=?, updated_at=? WHERE id=?",
            (state.value, blocked_from.value if blocked_from else None, iso(), job_id),
        )

    def _force_state(self, job_id: str, state: JobState) -> None:
        """Tests only: put a job in a state without a transition."""
        self._write_state(job_id, state, None)

    def set_coord_session(self, job_id: str, session_id: str) -> None:
        self.x("UPDATE jobs SET coord_session_id=? WHERE id=?", (session_id, job_id))

    # --- tasks --------------------------------------------------------------

    def create_task(
        self, job_id: str | None, contract: Mapping[str, Any], provenance: Provenance | str
    ) -> TaskRow:
        tid = str(contract["task_id"])
        self.x(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                tid,
                job_id,
                _j(dict(contract)),
                "submitted",
                None,
                None,
                None,
                iso(),
                self.row_provenance(provenance, job_id=job_id),
            ),
        )
        return self.get_task(tid)

    def get_task(self, task_id: str) -> TaskRow:
        rows = self.q("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise KeyError(task_id)
        r = rows[0]
        return TaskRow(
            id=r["id"],
            job_id=r["job_id"],
            contract=json.loads(r["contract"]),
            state=r["state"],
            tier=r["tier"],
            tier_reason=r["tier_reason"],
            backend_preset=r["backend_preset"],
            created_at=r["created_at"],
            provenance=r["provenance"],
        )

    def list_tasks(self, job_id: str | None = None) -> list[TaskRow]:
        if job_id is None:
            rows = self.q("SELECT id FROM tasks ORDER BY created_at, id")
        else:
            rows = self.q("SELECT id FROM tasks WHERE job_id=? ORDER BY id", (job_id,))
        return [self.get_task(r["id"]) for r in rows]

    def route_task(self, task_id: str, *, tier: str, tier_reason: str, backend_preset: str) -> None:
        self.x(
            "UPDATE tasks SET tier=?, tier_reason=?, backend_preset=?, state='queued' WHERE id=?",
            (tier, tier_reason, backend_preset, task_id),
        )

    def update_task(
        self, task_id: str, *, state: str | None = None, contract: Mapping[str, Any] | None = None
    ) -> None:
        if state is not None:
            self.x("UPDATE tasks SET state=? WHERE id=?", (state, task_id))
        if contract is not None:
            self.x("UPDATE tasks SET contract=? WHERE id=?", (_j(dict(contract)), task_id))

    # --- sessions -----------------------------------------------------------

    def create_session(
        self,
        job_id: str | None,
        role: Role,
        *,
        task_id: str | None = None,
        parent_session_id: str | None = None,
        backend_preset: str | None = None,
        branch: str | None = None,
        provenance: Provenance | str = Provenance.CLAUDE,
    ) -> SessionRow:
        self._check_role_separation(role, parent_session_id)
        sid = "s-" + uuid.uuid4().hex[:10]
        self.x(
            "INSERT INTO sessions (id, job_id, task_id, role, parent_session_id, backend_preset,"
            " status, branch, created_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                job_id,
                task_id,
                role.value,
                parent_session_id,
                backend_preset,
                "created",
                branch,
                iso(),
                self.row_provenance(provenance, job_id=job_id),
            ),
        )
        return self.get_session(sid)

    def _check_role_separation(self, role: Role, parent_session_id: str | None) -> None:
        """A hidden-test author is a fresh context: it has no parent, and no
        worker or reviewer session descends from one (learning ruling 14; the
        design's "never reused")."""
        if role is Role.HIDDEN_TEST_AUTHOR and parent_session_id is not None:
            raise SessionRoleError("a hidden-test author session starts fresh: it has no parent")
        seen: set[str] = set()
        cur = parent_session_id
        while cur and cur not in seen:
            seen.add(cur)
            try:
                parent = self.get_session(cur)
            except KeyError:
                return
            if parent.role is Role.HIDDEN_TEST_AUTHOR and role is not Role.HIDDEN_TEST_AUTHOR:
                raise SessionRoleError(
                    f"a {role.value} session can't continue hidden-test author session {cur}"
                )
            cur = parent.parent_session_id

    def get_session(self, session_id: str) -> SessionRow:
        rows = self.q("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not rows:
            raise KeyError(session_id)
        r = dict(rows[0])
        r["role"] = Role(r["role"])
        r["detail"] = json.loads(r["detail"])
        return SessionRow(**r)

    def list_sessions(self, job_id: str | None = None) -> list[SessionRow]:
        if job_id is None:
            rows = self.q("SELECT id FROM sessions ORDER BY rowid")
        else:
            rows = self.q("SELECT id FROM sessions WHERE job_id=? ORDER BY rowid", (job_id,))
        return [self.get_session(r["id"]) for r in rows]

    SESSION_FIELDS = frozenset(
        {
            "status",
            "container_id",
            "branch",
            "pr_url",
            "clone_path",
            "cost_usd",
            "tokens_in",
            "tokens_out",
            "started_at",
            "ended_at",
            "backend_preset",
        }
    )

    def update_session(
        self, session_id: str, *, detail: Mapping[str, Any] | None = None, **fields: Any
    ) -> SessionRow:
        bad = set(fields) - self.SESSION_FIELDS
        if bad:
            raise ValueError(f"unknown session fields {sorted(bad)}")
        with self.tx() as c:
            for k, v in fields.items():
                c.execute(f"UPDATE sessions SET {k}=? WHERE id=?", (v, session_id))  # noqa: S608
            if detail:
                cur = json.loads(
                    c.execute("SELECT detail FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
                )
                cur.update(detail)
                c.execute("UPDATE sessions SET detail=? WHERE id=?", (_j(cur), session_id))
        return self.get_session(session_id)

    def add_session_usage(
        self, session_id: str, cost: float, tokens_in: int, tokens_out: int
    ) -> None:
        self.x(
            "UPDATE sessions SET cost_usd=cost_usd+?, tokens_in=tokens_in+?,"
            " tokens_out=tokens_out+? WHERE id=?",
            (cost, tokens_in, tokens_out, session_id),
        )

    # --- transcripts (phase 2a) -----------------------------------------------

    def add_transcript(
        self,
        session_id: str,
        sdk_session_id: str,
        subpath: str | None,
        entries: list[Any],
        provenance: Provenance | str = Provenance.CLAUDE,
    ) -> None:
        """Transcript lines the harness mirrored from the SDK (its session
        store), in order. Entries are opaque JSON; the SDK reads them back."""
        now, prov = iso(), self.row_provenance(provenance, session_id=session_id)
        with self.tx() as c:
            c.executemany(
                "INSERT INTO transcripts (session_id, sdk_session_id, subpath, entry, ts,"
                " provenance) VALUES (?,?,?,?,?,?)",
                [(session_id, sdk_session_id, subpath, json.dumps(e), now, prov) for e in entries],
            )

    def transcript(self, session_id: str) -> tuple[str | None, list[Any]]:
        """A session's main transcript (no subagents): the SDK session id it
        last wrote under, and that session's entries in order."""
        rows = self.q(
            "SELECT sdk_session_id FROM transcripts WHERE session_id=? AND subpath IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not rows:
            return None, []
        sdk = str(rows[0]["sdk_session_id"])
        entries = self.q(
            "SELECT entry FROM transcripts WHERE session_id=? AND sdk_session_id=?"
            " AND subpath IS NULL ORDER BY id",
            (session_id, sdk),
        )
        return sdk, [json.loads(r["entry"]) for r in entries]

    # --- events -------------------------------------------------------------

    def add_event(
        self,
        type: str,
        data: Mapping[str, Any],
        provenance: Provenance | str,
        *,
        job_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        return self.x(
            "INSERT INTO events (job_id, session_id, task_id, ts, type, data, provenance)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                session_id,
                task_id,
                iso(),
                type,
                _j(dict(data)),
                self.row_provenance(
                    provenance, job_id=job_id, session_id=session_id, task_id=task_id
                ),
            ),
        )

    def events(
        self, *, job_id: str | None = None, session_id: str | None = None, after: int = 0
    ) -> list[EventRow]:
        sql, args = "SELECT * FROM events WHERE id>?", list[Any]([after])
        if job_id is not None:
            sql += " AND job_id=?"
            args.append(job_id)
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        rows = self.q(sql + " ORDER BY id", tuple(args))
        return [
            EventRow(
                r["id"],
                r["job_id"],
                r["session_id"],
                r["task_id"],
                r["ts"],
                r["type"],
                json.loads(r["data"]),
                r["provenance"],
            )
            for r in rows
        ]

    # --- artifacts ----------------------------------------------------------

    def add_artifact(
        self,
        job_id: str,
        name: str,
        content: str,
        provenance: Provenance | str,
        *,
        task_id: str | None = None,
    ) -> int:
        digest = hashlib.sha256(content.encode()).hexdigest()
        return self.x(
            "INSERT INTO artifacts (job_id, task_id, name, content, sha256, created_at, provenance)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                task_id,
                name,
                content,
                digest,
                iso(),
                self.row_provenance(provenance, job_id=job_id),
            ),
        )

    def get_artifact(
        self, job_id: str, name: str, task_id: str | None = None
    ) -> ArtifactRow | None:
        rows = self.q(
            "SELECT * FROM artifacts WHERE job_id=? AND name=? AND task_id IS ? ORDER BY id DESC"
            " LIMIT 1",
            (job_id, name, task_id),
        )
        return ArtifactRow(**dict(rows[0])) if rows else None

    def spec_before(self, job_id: str, artifact_id: int) -> ArtifactRow | None:
        """The job's spec.md as it was when artifact `artifact_id` was written."""
        rows = self.q(
            "SELECT * FROM artifacts WHERE job_id=? AND name='spec.md' AND task_id IS NULL"
            " AND id < ? ORDER BY id DESC LIMIT 1",
            (job_id, artifact_id),
        )
        return ArtifactRow(**dict(rows[0])) if rows else None

    def notes_since_approval(self, job_id: str) -> list[str]:
        """The owner's replan notes written since the job's last approval, oldest first."""
        approval = self.get_artifact(job_id, "approval.json")
        rows = self.q(
            "SELECT content FROM artifacts WHERE job_id=? AND name='owner_note.md'"
            " AND task_id IS NULL AND id > ? ORDER BY id",
            (job_id, approval.id if approval else 0),
        )
        return [r["content"] for r in rows]

    def list_artifacts(self, job_id: str) -> list[ArtifactRow]:
        """The newest version of each (name, task) for a job."""
        rows = self.q(
            "SELECT * FROM artifacts WHERE id IN (SELECT MAX(id) FROM artifacts WHERE job_id=?"
            " GROUP BY name, task_id) ORDER BY id",
            (job_id,),
        )
        return [ArtifactRow(**dict(r)) for r in rows]

    # --- inbox --------------------------------------------------------------

    def create_inbox_item(
        self,
        queue: str,
        kind: str,
        job_id: str | None,
        title: str,
        body: Mapping[str, Any],
        provenance: Provenance | str = Provenance.HUMAN,
    ) -> InboxItem:
        iid = self.x(
            "INSERT INTO inbox_items (queue, kind, job_id, title, body, created_at, provenance)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                queue,
                kind,
                job_id,
                title,
                _j(dict(body)),
                iso(),
                self.row_provenance(provenance, job_id=job_id),
            ),
        )
        return self.get_inbox_item(iid)

    def get_inbox_item(self, item_id: int) -> InboxItem:
        rows = self.q("SELECT * FROM inbox_items WHERE id=?", (item_id,))
        if not rows:
            raise KeyError(item_id)
        r = dict(rows[0])
        r["body"] = json.loads(r["body"])
        return InboxItem(**r)

    def list_inbox(self, status: str = "open") -> list[InboxItem]:
        rows = self.q("SELECT id FROM inbox_items WHERE status=? ORDER BY id", (status,))
        return [self.get_inbox_item(r["id"]) for r in rows]

    def resolve_inbox_item(self, item_id: int, by: str) -> None:
        self.x(
            "UPDATE inbox_items SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?",
            (iso(), by, item_id),
        )

    def add_approval(
        self, job_id: str, inbox_item_id: int, actor: str, channel: str, edits: str | None
    ) -> int:
        return self.x(
            "INSERT INTO approvals (job_id, inbox_item_id, actor, channel, edits, ts, provenance)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                inbox_item_id,
                actor,
                channel,
                edits,
                iso(),
                self.row_provenance(Provenance.HUMAN, job_id=job_id),
            ),
        )
