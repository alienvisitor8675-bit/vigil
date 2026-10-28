"""`vigil backup` and `vigil restore` (verify only; issue #23): the durability
story until there is a second machine.

A backup *set* is in the destination, named by UTC time:
`vigil-YYYYMMDDTHHMMSSZ.db` (SQLite online backup of `<data>/vigil.db`, mode
600), `vigil-YYYYMMDDTHHMMSSZ.vault/` (rsync of `<data>/vault/`) and
`vigil-YYYYMMDDTHHMMSSZ.marker` (holds the run id). Retention prunes only sets
that have the marker, so a person's own file with a matching name is never
removed. A set is built in `.vigil-<stamp>.tmp/` (marker inside) and moved out.
This module never opens anything under `~/.config/vigil/`; the destination
comes from the host (`--dest` or `VIGIL_BACKUP_DIR`), never from the repo.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from vigild.db import MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = Path.home() / ".config" / "vigil"
_STAMP = r"\d{8}T\d{6}Z"
_MARKER = re.compile(rf"^vigil-({_STAMP})\.marker$")
_TMP = re.compile(rf"^\.vigil-{_STAMP}\.tmp$")
TMP_MARKER = ".vigil-backup"
RSYNC_VANISHED = 24
RSYNC_FLAGS = ["-a", "--safe-links", "--no-specials", "--no-devices"]


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupSet:
    db: Path
    vault: Path
    warnings: list[str] = field(default_factory=list)


@contextlib.contextmanager
def _umask(mask: int) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _check_dest(data: Path, dest: Path) -> Path:
    dest = dest.expanduser().resolve()
    for name, bad in (
        ("the data dir", data.resolve()),
        ("the repo", REPO_ROOT),
        ("~/.config/vigil", SECRETS_DIR.resolve()),
    ):
        if _inside(dest, bad):
            raise BackupError(f"destination {dest} is inside {name}")
        if _inside(bad, dest):
            raise BackupError(f"destination {dest} contains {name}")
    return dest


def run_backup(
    data: Path,
    dest: Path,
    *,
    now: datetime | None = None,
    keep_days: int | None = 14,
) -> BackupSet:
    """Back up `<data>/vigil.db` and `<data>/vault/` into `dest`, then prune."""
    src = data / "vigil.db"
    if not src.is_file():
        raise BackupError(f"no database at {src}")
    dest = _check_dest(data, dest)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    run_id = secrets.token_hex(8)
    warnings: list[str] = []
    with _umask(0o077):
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        _clean_tmp(dest)
        final_db = dest / f"vigil-{stamp}.db"
        final_vault = dest / f"vigil-{stamp}.vault"
        final_marker = dest / f"vigil-{stamp}.marker"
        if final_db.exists() or final_vault.exists() or final_marker.exists():
            raise BackupError(f"backup set {stamp} already exists")
        work = dest / f".vigil-{stamp}.tmp"
        work.mkdir()
        (work / TMP_MARKER).write_text(run_id)
        try:
            _copy_db(src, work / "db")
            warnings += _copy_vault(data / "vault", work / "vault")
            os.rename(work / "vault", final_vault)
            os.rename(work / "db", final_db)
            (work / TMP_MARKER).rename(final_marker)  # last: the set is complete
        except BaseException:
            for p in (final_db, final_vault, final_marker):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if keep_days is not None:
        prune(dest, keep_days, now=now)
    return BackupSet(final_db, final_vault, warnings)


def _copy_db(src: Path, out: Path) -> None:
    # Read-only source (mode=ro): a backup never creates -wal/-shm in the data
    # dir. The online backup API is consistent while vigild writes. The umask
    # makes sqlite create the file 600; there is no wider window.
    # With no -wal/-shm beside it nobody has the file open, so immutable=1 keeps
    # a read-only WAL reader from creating them; with them, vigild is live and
    # they already exist.
    live = any(src.with_name(src.name + sfx).exists() for sfx in ("-wal", "-shm"))
    flags = "mode=ro" if live else "mode=ro&immutable=1"
    s = sqlite3.connect(f"file:{quote(str(src))}?{flags}", uri=True, timeout=30)
    d = sqlite3.connect(str(out))
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
        d.commit()
    finally:
        d.close()
        s.close()
    os.chmod(out, 0o600)


def _copy_vault(vault_src: Path, out: Path) -> list[str]:
    out.mkdir()
    if not vault_src.is_dir():
        return []
    r = subprocess.run(
        ["rsync", *RSYNC_FLAGS, "--", f"{vault_src}/", f"{out}/"],
        capture_output=True,
        text=True,
    )
    if r.returncode == RSYNC_VANISHED:
        return ["rsync: some files vanished while copying (the vault changed underneath)"]
    if r.returncode != 0:
        raise BackupError(f"rsync failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return []


def _clean_tmp(dest: Path) -> None:
    """Remove leftovers of failed runs: `.vigil-<stamp>.tmp/` dirs that hold
    this tool's marker. Anything else is not ours."""
    for p in dest.iterdir():
        if _TMP.match(p.name) and p.is_dir() and not p.is_symlink() and (p / TMP_MARKER).is_file():
            shutil.rmtree(p, ignore_errors=True)


def prune(dest: Path, keep_days: int, *, now: datetime) -> list[Path]:
    """Remove dated sets older than `keep_days`, only those with the marker."""
    cutoff = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for m in sorted(dest.iterdir()):
        mm = _MARKER.match(m.name)
        if not mm or not m.is_file() or m.is_symlink():
            continue
        when = datetime.strptime(mm.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if when >= cutoff:
            continue
        db = dest / f"vigil-{mm.group(1)}.db"
        vault = dest / f"vigil-{mm.group(1)}.vault"
        if db.is_file() and not db.is_symlink():
            db.unlink()
            removed.append(db)
        if vault.is_dir() and not vault.is_symlink():
            shutil.rmtree(vault)
            removed.append(vault)
        m.unlink()
        removed.append(m)
    return removed


def verify_backup(db: Path) -> list[str]:
    """Open `db` read-only and check it. Returns problems (empty: fine). Writes
    nothing: `mode=ro` plus `immutable=1` keeps sqlite from making -wal/-shm."""
    if not db.is_file():
        return [f"{db} does not exist"]
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as e:
        return [f"cannot open read-only: {e}"]
    try:
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as e:
            return [f"not a readable database: {e}"]
    finally:
        conn.close()
    problems: list[str] = []
    if [r[0] for r in rows] != ["ok"]:
        problems.append("integrity_check: " + "; ".join(str(r[0]) for r in rows[:5]))
    if version != len(MIGRATIONS):
        problems.append(f"user_version is {version}, expected {len(MIGRATIONS)}")
    return problems
