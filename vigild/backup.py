"""`vigil backup` and `vigil restore` (verify only; issue #23): the durability
story until there is a second machine.

A backup *set* is in the destination, named by UTC time:
`vigil-YYYYMMDDTHHMMSSZ.db` (SQLite online backup of `<data>/vigil.db`, mode
600), `vigil-YYYYMMDDTHHMMSSZ.vault/` (rsync of `<data>/vault/`) and
`vigil-YYYYMMDDTHHMMSSZ.marker` (holds the run id; a plain copy of the temp
marker, made with an exclusive create, so it works on exFAT and FAT drives
that have no hard links and is never written through a planted symlink).
`freshness` says whether the newest complete set is recent (`vigil doctor`).
Retention prunes only sets
whose marker holds a run id this tool wrote, so a person's own file with a matching name is never
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
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
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
        if any(os.path.lexists(p) for p in (final_db, final_vault, final_marker)):
            raise BackupError(f"backup set {stamp} already exists")
        work = dest / f".vigil-{stamp}.tmp"
        work.mkdir()
        (work / TMP_MARKER).write_text(run_id)
        placed: list[Path] = []  # what this run put in dest; only these are discarded
        try:
            _copy_db(src, work / "db")
            warnings += _copy_vault(data / "vault", work / "vault")
            # The marker lands first, so a piece in dest is never unmarked; a
            # set cut short by a kill is marked and prune removes it when old.
            # A copy, not a hard link (exFAT/FAT have none); "x" never opens an
            # existing file or a symlink at that name.
            with open(final_marker, "x") as f:
                placed.append(final_marker)
                f.write(run_id)
            os.rename(work / "db", final_db)
            placed.append(final_db)
            os.rename(work / "vault", final_vault)
            placed.append(final_vault)
        except BaseException:
            _discard(placed)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if keep_days is not None:
        prune(dest, keep_days, now=now)
    return BackupSet(final_db, final_vault, warnings)


def _discard(paths: list[Path]) -> None:
    for p in paths:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _output_problems(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if [r[0] for r in rows] != ["ok"]:
        problems.append("integrity_check: " + "; ".join(str(r[0]) for r in rows[:5]))
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) <= 0:
        problems.append("user_version is 0")
    return problems


def _copy_db(src: Path, out: Path) -> None:
    # A normal connection and the online backup API: a consistent snapshot
    # under WAL while vigild runs. It may create -wal/-shm beside the source;
    # that is accepted. The umask makes sqlite create the output 600. The
    # output is checked before the caller renames it into place.
    s = sqlite3.connect(str(src), timeout=30)
    d = sqlite3.connect(str(out))
    try:
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
            d.commit()
            problems = _output_problems(d)
        except sqlite3.Error as e:
            raise BackupError(f"database backup failed: {e}") from e
    finally:
        d.close()
        s.close()
    if problems:
        out.unlink(missing_ok=True)
        raise BackupError("backup output failed its check: " + "; ".join(problems))
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


def _ours(marker: Path) -> bool:
    """True if `marker` is a plain file holding exactly a run id this tool wrote."""
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 64:
            return False
        return bool(_RUN_ID.fullmatch(marker.read_text()))
    except OSError, UnicodeDecodeError:
        return False


def _clean_tmp(dest: Path) -> None:
    """Remove leftovers of failed runs: `.vigil-<stamp>.tmp/` dirs that hold
    this tool's marker. Anything else is not ours."""
    for p in dest.iterdir():
        if _TMP.match(p.name) and p.is_dir() and not p.is_symlink() and _ours(p / TMP_MARKER):
            shutil.rmtree(p, ignore_errors=True)


def prune(dest: Path, keep_days: int, *, now: datetime) -> list[Path]:
    """Remove dated sets older than `keep_days`, only those with the marker."""
    cutoff = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for m in sorted(dest.iterdir()):
        mm = _MARKER.match(m.name)
        if not mm or not _ours(m):
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
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro&immutable=1", uri=True)
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


MAX_AGE = timedelta(hours=36)


def freshness(dest: Path, *, now: datetime, max_age: timedelta = MAX_AGE) -> tuple[bool, str]:
    """Is the newest complete set in `dest` at most `max_age` old? A complete
    set is a marker that is ours, a regular-file db and a vault directory. Age
    is `now` minus the stamp in the name. The note never holds the path."""
    try:
        names = sorted(p.name for p in dest.iterdir())
    except OSError as e:
        return False, f"destination unreachable: {type(e).__name__}"
    newest: datetime | None = None
    for name in names:
        mm = _MARKER.match(name)
        if not mm or not _ours(dest / name):
            continue
        db, vault = dest / f"vigil-{mm.group(1)}.db", dest / f"vigil-{mm.group(1)}.vault"
        if not (db.is_file() and not db.is_symlink() and vault.is_dir() and not vault.is_symlink()):
            continue
        when = datetime.strptime(mm.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if newest is None or when > newest:
            newest = when
    if newest is None:
        return False, "no complete backup set"
    age = now.astimezone(UTC) - newest
    note = (
        f"newest set {newest.strftime('%Y%m%dT%H%M%SZ')}, {int(age.total_seconds() // 3600)}h old"
    )
    return age <= max_age, note
