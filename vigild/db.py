"""vigild's SQLite store (B2): stdlib sqlite3, WAL, one writer.

Every table that holds an artifact or a record of work carries a
`provenance` column constrained to claude | local | human.
"""

from __future__ import annotations

import hashlib
import json
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

PROV = "provenance TEXT NOT NULL CHECK (provenance IN ('claude','local','human'))"

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
  timings TEXT, ts TEXT NOT NULL, {PROV});
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


# Ordered steps; PRAGMA user_version is how many have run (review round 2, N8).
# Each step is idempotent, since SCHEMA's CREATE IF NOT EXISTS may already
# have made a table in its newest shape. Append only; never reorder.
def _m2_dev_image_keys(conn: sqlite3.Connection) -> None:
    """Review round 2: dev images are built per lockfile key and validated by stage."""
    _add_columns(conn, "dev_images", {"key": "TEXT", "stage": "TEXT", "base_sha": "TEXT"})


def _m3_job_blocked_on(conn: sqlite3.Connection) -> None:
    """Round 3: a job blocked on a dependency names the deps job it waits for."""
    _add_columns(conn, "jobs", {"blocked_on": "TEXT"})


def _create(conn: sqlite3.Connection, ddl: str) -> None:
    """CREATE statements one at a time (executescript would commit the
    migration's transaction)."""
    for stmt in filter(str.strip, ddl.split(";")):
        conn.execute(stmt)


def _m4_trust_ladder(conn: sqlite3.Connection) -> None:
    """Phase 2a, item 1: the ladder's live rungs and the audits that move them."""
    _create(conn, LADDER_DDL)


def _m5_label_intake(conn: sqlite3.Connection) -> None:
    """Phase 2a, item 2: each labelled issue becomes a job exactly once."""
    _create(conn, INTAKE_DDL)


def _m6_transcripts(conn: sqlite3.Connection) -> None:
    """Phase 2a, items 2-3: every session's transcript, for resume and forks."""
    _create(conn, TRANSCRIPT_DDL)


def _m7_breakers(conn: sqlite3.Connection) -> None:
    """Phase 2a, item 4: one table for every circuit breaker."""
    _create(conn, BREAKER_DDL)


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m1_gateway_token_binding,
    _m2_dev_image_keys,
    _m3_job_blocked_on,
    _m4_trust_ladder,
    _m5_label_intake,
    _m6_transcripts,
    _m7_breakers,
]


def migrate(conn: sqlite3.Connection) -> int:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for n, step in enumerate(MIGRATIONS, start=1):
        if n > version:
            conn.execute("BEGIN IMMEDIATE")
            try:
                step(conn)
                conn.execute(f"PRAGMA user_version = {n}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    return len(MIGRATIONS)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


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
    ) -> Job:
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
                " updated_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
        return self.get_job(job_id)

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
                require(provenance).value,
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
                require(provenance).value,
            ),
        )
        return self.get_session(sid)

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
        now, prov = iso(), require(provenance).value
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
            (job_id, session_id, task_id, iso(), type, _j(dict(data)), require(provenance).value),
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
            (job_id, task_id, name, content, digest, iso(), require(provenance).value),
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
            (queue, kind, job_id, title, _j(dict(body)), iso(), require(provenance).value),
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
            (job_id, inbox_item_id, actor, channel, edits, iso(), Provenance.HUMAN.value),
        )
