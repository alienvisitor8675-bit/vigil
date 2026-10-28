"""`vigil backup` and `vigil restore` (vigild/backup.py): online SQLite backup,
vault rsync, retention, and a read-only restore check."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vigild import backup
from vigild.db import MIGRATIONS, Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

NOW = datetime(2026, 10, 27, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "vault" / "sub").mkdir(parents=True)
    (root / "vault" / "sub" / "blob").write_text("hello")
    store = Store.open(root / "vigil.db")
    store.create_job(repo="o/r", base="main", title="t", request="r", notes=[], source="human")
    store.conn.close()
    return root


def test_backup_file_is_mode_600_and_named_by_time(data: Path, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    old = os.umask(0o022)  # a lax caller umask must not leak into the backup
    try:
        made = backup.run_backup(data, dest, now=NOW)
    finally:
        os.umask(old)
    assert made.db.name == "vigil-20261027T030000Z.db"
    assert stat.S_IMODE(made.db.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert (made.vault / "sub" / "blob").read_text() == "hello"
    assert not [p for p in dest.iterdir() if p.name.endswith(".tmp")]


def test_backup_is_consistent_while_a_writer_holds_the_db(data: Path, tmp_path: Path) -> None:
    writer = sqlite3.connect(data / "vigil.db", isolation_level=None, check_same_thread=False)
    writer.execute("PRAGMA journal_mode=WAL")
    stop = threading.Event()

    def churn() -> None:
        i = 0
        while not stop.is_set():
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO events (ts, type, data, provenance) VALUES ('t', 'x', ?, 'human')",
                (f"{i}" * 200,),
            )
            writer.execute("COMMIT")
            i += 1

    t = threading.Thread(target=churn)
    t.start()
    try:
        made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    finally:
        stop.set()
        t.join()
        writer.close()
    ro = sqlite3.connect(f"file:{made.db}?mode=ro", uri=True)
    assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ro.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_retention_prunes_only_old_sets_this_tool_made(data: Path, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    for days in (20, 15, 13, 1):
        backup.run_backup(data, dest, now=NOW - timedelta(days=days), keep_days=None)
    (dest / "notes.txt").write_text("mine")
    (dest / "vigil-old.db").write_text("not a dated set")
    (dest / "vigil-20200101T000000Z.db.bak").write_text("not ours")
    (dest / "other").mkdir()
    made = backup.run_backup(data, dest, now=NOW, keep_days=14)
    left = sorted(p.name for p in dest.iterdir())
    assert "vigil-20261007T030000Z.db" not in left  # 20 days
    assert "vigil-20261007T030000Z.vault" not in left
    assert "vigil-20261012T030000Z.db" not in left  # 15 days
    assert "vigil-20261014T030000Z.db" in left  # 13 days
    assert made.db.name in left
    for keep in ("notes.txt", "vigil-old.db", "vigil-20200101T000000Z.db.bak", "other"):
        assert keep in left


def test_destination_may_not_sit_in_the_data_dir_or_repo(data: Path) -> None:
    with pytest.raises(backup.BackupError):
        backup.run_backup(data, data / "backups", now=NOW)
    repo = Path(backup.__file__).resolve().parents[1]
    with pytest.raises(backup.BackupError):
        backup.run_backup(data, repo / "backups", now=NOW)


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(backup.BackupError):
        backup.run_backup(tmp_path / "empty", tmp_path / "dest", now=NOW)


def test_dry_run_passes_a_good_backup_and_writes_nothing(data: Path, tmp_path: Path) -> None:
    made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    before = sorted((tmp_path / "dest").rglob("*"))
    problems = backup.verify_backup(made.db)
    assert problems == []
    assert sorted((tmp_path / "dest").rglob("*")) == before  # no -wal, no -shm


def test_dry_run_catches_missing_corrupt_and_wrong_version(data: Path, tmp_path: Path) -> None:
    assert backup.verify_backup(tmp_path / "nope.db")
    made = backup.run_backup(data, tmp_path / "dest", now=NOW)

    corrupt = tmp_path / "corrupt.db"
    raw = bytearray(made.db.read_bytes())
    for i in range(4096 * 2, len(raw)):
        raw[i] = 0xAB
    corrupt.write_bytes(bytes(raw))
    assert backup.verify_backup(corrupt)

    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database at all, not even close" * 50)
    assert backup.verify_backup(junk)

    wrong = tmp_path / "wrong.db"
    wrong.write_bytes(made.db.read_bytes())
    c = sqlite3.connect(wrong)
    c.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 3}")
    c.commit()
    c.close()
    problems = backup.verify_backup(wrong)
    assert any("user_version" in p for p in problems)


def test_restore_defaults_to_dry_run_and_refuses_the_live_dir(data: Path, tmp_path: Path) -> None:
    made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    target = tmp_path / "restored"
    backup.restore(made.db, target, live_dir=data, dry_run=True)
    assert not target.exists()
    with pytest.raises(backup.BackupError, match="live"):
        backup.restore(made.db, data, live_dir=data, dry_run=False)
    with pytest.raises(backup.BackupError, match="running"):
        backup.restore(
            made.db, data, live_dir=data, dry_run=False, allow_live=True, running=lambda: True
        )
    backup.restore(made.db, target, live_dir=data, dry_run=False)
    assert (target / "vault" / "sub" / "blob").read_text() == "hello"
    assert backup.verify_backup(target / "vigil.db") == []
    assert stat.S_IMODE((target / "vigil.db").stat().st_mode) == 0o600
    with pytest.raises(backup.BackupError, match="exists"):
        backup.restore(made.db, target, live_dir=data, dry_run=False)


def test_cli_backup_and_restore(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigil.main import app

    monkeypatch.setenv("VIGIL_DATA_DIR", str(data))
    dest = tmp_path / "dest"
    r = CliRunner().invoke(app, ["backup", "--dest", str(dest)])
    assert r.exit_code == 0, r.output
    db = next(dest.glob("vigil-*.db"))
    r = CliRunner().invoke(app, ["restore", str(db)])
    assert r.exit_code == 0 and "dry run" in r.output, r.output
    r = CliRunner().invoke(app, ["restore", str(tmp_path / "missing.db")])
    assert r.exit_code == 1


def test_backup_never_reads_the_secrets_dir(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = (Path.home() / ".config" / "vigil").resolve()
    real_open = os.open
    seen: list[str] = []

    def guard(path: object, *a: object, **k: object) -> int:
        if str(Path(str(path)).resolve()).startswith(str(secrets)):
            seen.append(str(path))
        return real_open(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", guard)
    monkeypatch.setattr(Path, "read_text", _guarded(Path.read_text, secrets, seen), raising=True)
    from vigil.main import app

    monkeypatch.setenv("VIGIL_DATA_DIR", str(data))
    CliRunner().invoke(app, ["backup", "--dest", str(tmp_path / "dest")])
    assert seen == []
    with pytest.raises(backup.BackupError):
        backup.run_backup(data, secrets / "backups", now=NOW)


def _guarded(fn, secrets: Path, seen: list[str]):  # type: ignore[no-untyped-def]
    def inner(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if str(self.resolve()).startswith(str(secrets)):
            seen.append(str(self))
        return fn(self, *a, **k)

    return inner
