"""`vigil backup` and `vigil restore`: the durability story until there is a
second machine (issue #23).

A backup *set* is two siblings in the destination, named by UTC time:
`vigil-YYYYMMDDTHHMMSSZ.db` (SQLite online backup of `<data>/vigil.db`, mode
600) and `vigil-YYYYMMDDTHHMMSSZ.vault/` (rsync of `<data>/vault/`). Retention
and pruning touch only names matching that pattern. This module never opens
anything under `~/.config/vigil/`; the destination comes from the host
(`--dest` or `VIGIL_BACKUP_DIR`), never from the repo.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vigild.db import MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = Path.home() / ".config" / "vigil"
_SET = re.compile(r"^vigil-(\d{8}T\d{6}Z)\.(db|vault)$")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupSet:
    db: Path
    vault: Path


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
    with _umask(0o077):
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        final_db = dest / f"vigil-{stamp}.db"
        final_vault = dest / f"vigil-{stamp}.vault"
        if final_db.exists() or final_vault.exists():
            raise BackupError(f"backup set {stamp} already exists")
        tmp = dest / f".vigil-{stamp}.db.tmp"
        tmp.unlink(missing_ok=True)
        # The online backup API: consistent while vigild writes. The umask
        # makes sqlite create the file 600; there is no wider window.
        s = sqlite3.connect(str(src), timeout=30)
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
            d.commit()
        except BaseException:
            d.close()
            s.close()
            tmp.unlink(missing_ok=True)
            raise
        d.close()
        s.close()
        os.chmod(tmp, 0o600)
        vault_src = data / "vault"
        tmp_vault = dest / f".vigil-{stamp}.vault.tmp"
        shutil.rmtree(tmp_vault, ignore_errors=True)
        tmp_vault.mkdir()
        if vault_src.is_dir():
            r = subprocess.run(
                ["rsync", "-a", "--", f"{vault_src}/", f"{tmp_vault}/"],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                tmp.unlink(missing_ok=True)
                shutil.rmtree(tmp_vault, ignore_errors=True)
                raise BackupError(f"rsync failed ({r.returncode}): {r.stderr.strip()[:300]}")
        os.rename(tmp_vault, final_vault)
        os.rename(tmp, final_db)  # the .db appears last: a set with a .db is complete
    if keep_days is not None:
        prune(dest, keep_days, now=now)
    return BackupSet(final_db, final_vault)


def prune(dest: Path, keep_days: int, *, now: datetime) -> list[Path]:
    """Remove dated sets older than `keep_days`; nothing else in `dest`."""
    cutoff = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for p in sorted(dest.iterdir()):
        m = _SET.match(p.name)
        if not m:
            continue
        when = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if when >= cutoff:
            continue
        if m.group(2) == "db" and p.is_file() and not p.is_symlink():
            p.unlink()
        elif m.group(2) == "vault" and p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            continue
        removed.append(p)
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


def vault_of(db: Path) -> Path:
    m = _SET.match(db.name)
    return db.with_name(f"vigil-{m.group(1)}.vault") if m else db.with_name(db.stem + ".vault")


def restore(
    db: Path,
    target: Path,
    *,
    live_dir: Path,
    dry_run: bool = True,
    allow_live: bool = False,
    running: Callable[[], bool] = lambda: False,
) -> list[str]:
    """Check a backup, and unless `dry_run`, copy it into `target` (a data dir).
    Returns what it did or would do."""
    problems = verify_backup(db)
    if problems:
        raise BackupError("; ".join(problems))
    vault = vault_of(db)
    plan = [f"{db.name} passes integrity_check, user_version {len(MIGRATIONS)}"]
    plan.append(f"vault: {vault if vault.is_dir() else 'none in this set'}")
    if dry_run:
        return [*plan, f"dry run: would restore into {target}; nothing written"]
    target = target.expanduser().resolve()
    is_live = target == live_dir.expanduser().resolve()
    if is_live and not allow_live:
        raise BackupError(f"{target} is the live data dir; pass --allow-live to restore into it")
    # A scratch target collides with nothing a running vigild holds; the live
    # dir is the one a running vigild has open.
    if is_live and running():
        raise BackupError("vigild is running; stop it first")
    if (target / "vigil.db").exists():
        raise BackupError(f"{target / 'vigil.db'} exists; restore into an empty dir")
    with _umask(0o077):
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(db, target / "vigil.db")
        os.chmod(target / "vigil.db", 0o600)
        (target / "vault").mkdir(exist_ok=True)
        if vault.is_dir():
            r = subprocess.run(
                ["rsync", "-a", "--", f"{vault}/", f"{target / 'vault'}/"],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise BackupError(f"rsync failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return [*plan, f"restored into {target}"]
