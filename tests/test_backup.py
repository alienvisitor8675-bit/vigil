"""`vigil backup` and `vigil restore` (vigild/backup.py): online SQLite backup,
vault rsync, retention, and a read-only restore check."""

from __future__ import annotations

import builtins
import errno
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


def test_backup_file_is_mode_600_and_named_by_time(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "dest"
    real_chmod = os.chmod
    at_chmod: list[int] = []

    def spy(path: object, mode: int, *a: object, **k: object) -> None:
        # What the file's mode is the moment before anything chmods it.
        if Path(str(path)).is_file():
            at_chmod.append(stat.S_IMODE(Path(str(path)).stat().st_mode))
        real_chmod(path, mode, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "chmod", spy)
    old = os.umask(0o022)  # a lax caller umask must not leak into the backup
    try:
        made = backup.run_backup(data, dest, now=NOW)
    finally:
        os.umask(old)
    assert at_chmod and set(at_chmod) == {0o600}  # 600 from the moment it exists
    assert made.db.name == "vigil-20261027T030000Z.db"
    assert stat.S_IMODE(made.db.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert (made.vault / "sub" / "blob").read_text() == "hello"
    assert not [p for p in dest.iterdir() if p.name.endswith(".tmp")]


def test_backup_is_a_committed_snapshot_while_a_writer_inserts(data: Path, tmp_path: Path) -> None:
    writer = sqlite3.connect(data / "vigil.db", isolation_level=None, check_same_thread=False)
    writer.execute("PRAGMA journal_mode=WAL")
    committed = [writer.execute("SELECT count(*) FROM events").fetchone()[0]]
    stop = threading.Event()
    started = threading.Event()

    def churn() -> None:
        i = 0
        while not stop.is_set():
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO events (ts, type, data, provenance) VALUES ('t', 'x', ?, 'human')",
                (f"{i}" * 200,),
            )
            writer.execute("COMMIT")
            committed.append(committed[-1] + 1)
            started.set()
            i += 1

    t = threading.Thread(target=churn)
    t.start()
    started.wait(10)
    try:
        made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    finally:
        stop.set()
        t.join()
        writer.close()
    ro = sqlite3.connect(f"file:{made.db}?mode=ro", uri=True)
    assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ro.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert ro.execute("SELECT count(*) FROM events").fetchone()[0] in committed
    ro.close()


def test_retention_prunes_only_old_sets_this_tool_made(data: Path, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    for days in (20, 15, 13, 1):
        backup.run_backup(data, dest, now=NOW - timedelta(days=days), keep_days=None)
    (dest / "notes.txt").write_text("mine")
    (dest / "vigil-old.db").write_text("not a dated set")
    (dest / "vigil-20200101T000000Z.db.bak").write_text("not ours")
    (dest / "vigil-20200101T000000Z.db").write_text("a person's own file, no marker")
    (dest / "vigil-20200101T000000Z.vault").mkdir()
    (dest / "other").mkdir()
    made = backup.run_backup(data, dest, now=NOW, keep_days=14)
    left = sorted(p.name for p in dest.iterdir())
    assert "vigil-20261007T030000Z.db" not in left  # 20 days
    assert "vigil-20261007T030000Z.vault" not in left
    assert "vigil-20261012T030000Z.db" not in left  # 15 days
    assert "vigil-20261014T030000Z.db" in left  # 13 days
    assert made.db.name in left
    for keep in (
        "notes.txt",
        "vigil-old.db",
        "vigil-20200101T000000Z.db.bak",
        "vigil-20200101T000000Z.db",
        "vigil-20200101T000000Z.vault",
        "other",
    ):
        assert keep in left
    assert not [n for n in left if n.startswith("vigil-20261007") or n.startswith("vigil-20261012")]


def test_leftover_tmp_is_cleaned_only_when_it_carries_the_marker(
    data: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    ours = dest / ".vigil-20200101T000000Z.tmp"
    ours.mkdir()
    (ours / ".vigil-backup").write_text("0123456789abcdef")
    (ours / "x.db").write_text("half")
    theirs = dest / ".vigil-mine.tmp"
    theirs.mkdir()
    (theirs / "keep").write_text("mine")
    odd = dest / ".vigil-20200102T000000Z.tmp"
    odd.mkdir()
    (odd / ".vigil-backup").write_text("someone else's note")
    backup.run_backup(data, dest, now=NOW)
    assert not ours.exists()
    assert (theirs / "keep").exists()
    assert odd.exists()  # the marker's content is not this tool's


def test_destination_may_not_sit_in_the_data_dir_or_repo(data: Path) -> None:
    with pytest.raises(backup.BackupError):
        backup.run_backup(data, data / "backups", now=NOW)
    repo = Path(backup.__file__).resolve().parents[1]
    with pytest.raises(backup.BackupError):
        backup.run_backup(data, repo / "backups", now=NOW)


def test_destination_may_not_contain_the_data_dir_repo_or_secrets(data: Path) -> None:
    with pytest.raises(backup.BackupError, match="contains"):
        backup.run_backup(data, data.parent, now=NOW)
    with pytest.raises(backup.BackupError, match="contains"):
        backup.run_backup(data, Path.home(), now=NOW)
    with pytest.raises(backup.BackupError, match="contains"):
        backup.run_backup(data, Path(backup.__file__).resolve().parents[1].parent, now=NOW)


def test_vault_symlink_pointing_outside_is_not_followed(data: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("do not copy")
    (data / "vault" / "escape").symlink_to(outside)
    (data / "vault" / "escape-file").symlink_to(outside / "secret")
    made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    assert not (made.vault / "escape").exists() and not (made.vault / "escape").is_symlink()
    assert not (made.vault / "escape-file").is_symlink()
    assert not list((tmp_path / "dest").rglob("secret"))
    assert (made.vault / "sub" / "blob").read_text() == "hello"


def test_rsync_vanished_files_is_a_warning_other_errors_fail(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def fake(code: int):  # type: ignore[no-untyped-def]
        return lambda cmd, **k: subprocess.CompletedProcess(cmd, code, "", "boom")

    dest = tmp_path / "dest"
    monkeypatch.setattr(backup.subprocess, "run", fake(24))
    made = backup.run_backup(data, dest, now=NOW)
    assert made.warnings and made.db.is_file()
    monkeypatch.setattr(backup.subprocess, "run", fake(23))
    with pytest.raises(backup.BackupError, match="rsync"):
        backup.run_backup(data, dest, now=NOW + timedelta(hours=1))
    assert sorted(p.name for p in dest.iterdir() if "20261027T040000Z" in p.name) == []
    assert not [p for p in dest.iterdir() if p.name.endswith(".tmp")]


def test_backup_leaves_the_data_content_unchanged(data: Path, tmp_path: Path) -> None:
    def snapshot() -> tuple[int, int]:
        c = sqlite3.connect(data / "vigil.db")
        try:
            return (
                int(c.execute("PRAGMA user_version").fetchone()[0]),
                int(c.execute("SELECT count(*) FROM jobs").fetchone()[0]),
            )
        finally:
            c.close()

    before_files = {p.name for p in data.rglob("*")}
    before = snapshot()
    backup.run_backup(data, tmp_path / "dest", now=NOW)
    assert snapshot() == before
    new = {p.name for p in data.rglob("*")} - before_files
    assert new <= {"vigil.db-wal", "vigil.db-shm"}


def test_output_is_checked_before_it_is_renamed_into_place(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "dest"
    monkeypatch.setattr(backup, "_output_problems", lambda c: ["integrity_check: broken"])
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.run_backup(data, dest, now=NOW)
    assert list(dest.iterdir()) == []


def test_output_with_user_version_zero_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    c = sqlite3.connect(root / "vigil.db")
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    dest = tmp_path / "dest"
    with pytest.raises(backup.BackupError, match="user_version"):
        backup.run_backup(root, dest, now=NOW)
    assert list(dest.iterdir()) == []


def test_prune_requires_the_tools_exact_marker_content(data: Path, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    old = NOW - timedelta(days=30)
    made = backup.run_backup(data, dest, now=old, keep_days=None)
    marker = made.db.with_suffix(".marker")
    assert len(marker.read_text()) == 16
    other = backup.run_backup(data, dest, now=old - timedelta(days=1), keep_days=None)
    other.db.with_suffix(".marker").write_text("my own notes")
    backup.run_backup(data, dest, now=NOW, keep_days=14)
    assert not made.db.exists() and not marker.exists()
    assert other.db.exists() and other.vault.exists()
    assert other.db.with_suffix(".marker").read_text() == "my own notes"


def test_a_kill_between_renames_never_leaves_an_unmarked_piece(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Killed(BaseException):
        pass

    real = os.rename
    for n in (1, 2):
        dest = tmp_path / f"dest{n}"
        calls = 0

        def flaky(a: object, b: object, _n: int = n) -> None:
            nonlocal calls
            calls += 1
            if calls == _n:
                raise Killed
            real(a, b)  # type: ignore[arg-type]

        with monkeypatch.context() as m:
            m.setattr(os, "rename", flaky)
            # a real kill runs no cleanup
            m.setattr(backup, "_discard", lambda paths: None)
            m.setattr(backup.shutil, "rmtree", lambda *a, **k: None)
            with pytest.raises(Killed):
                backup.run_backup(data, dest, now=NOW)
        for p in dest.iterdir():
            if p.name.startswith("vigil-"):
                assert (dest / "vigil-20261027T030000Z.marker").is_file(), (n, p.name)
        backup.run_backup(data, dest, now=NOW + timedelta(days=30), keep_days=14)
        assert [p.name for p in dest.iterdir() if "20261027T030000Z" in p.name] == []


def test_verify_handles_a_path_with_uri_characters(data: Path, tmp_path: Path) -> None:
    made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    odd = tmp_path / "we?ird#dir"
    odd.mkdir()
    copy = odd / "b%20.db"
    copy.write_bytes(made.db.read_bytes())
    assert backup.verify_backup(copy) == []


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
    assert r.exit_code == 0 and "passes integrity_check" in r.output, r.output
    r = CliRunner().invoke(app, ["restore", str(tmp_path / "missing.db")])
    assert r.exit_code == 1
    for gone in ("--no-dry-run", "--target", "--allow-live"):
        assert CliRunner().invoke(app, ["restore", str(db), gone]).exit_code == 2


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
    real_builtin_open = builtins.open

    def guard_builtin(file: object, *a: object, **k: object):  # type: ignore[no-untyped-def]
        if isinstance(file, (str, os.PathLike)) and str(Path(file).resolve()).startswith(
            str(secrets)
        ):
            seen.append(str(file))
        return real_builtin_open(file, *a, **k)  # type: ignore[call-overload]

    monkeypatch.setattr(builtins, "open", guard_builtin)
    monkeypatch.setattr(Path, "read_text", _guarded(Path.read_text, secrets, seen), raising=True)
    from vigil.main import app

    monkeypatch.setenv("VIGIL_DATA_DIR", str(data))
    r = CliRunner().invoke(app, ["backup", "--dest", str(tmp_path / "dest")])
    assert r.exit_code == 0, r.output
    db = next((tmp_path / "dest").glob("vigil-*.db"))
    assert backup.verify_backup(db) == []  # the verify path too
    CliRunner().invoke(app, ["restore", str(db)])
    assert seen == []
    with pytest.raises(backup.BackupError):
        backup.run_backup(data, secrets / "backups", now=NOW)


def _guarded(fn, secrets: Path, seen: list[str]):  # type: ignore[no-untyped-def]
    def inner(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if str(self.resolve()).startswith(str(secrets)):
            seen.append(str(self))
        return fn(self, *a, **k)

    return inner


def test_marker_is_a_copy_so_a_filesystem_without_hard_links_works(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_link(*a: object, **k: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    made = backup.run_backup(data, tmp_path / "dest", now=NOW)
    marker = made.db.with_suffix(".marker")
    assert backup._ours(marker) and made.db.is_file() and made.vault.is_dir()


def test_a_dangling_symlink_at_the_marker_name_fails_and_is_not_written_through(
    data: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    target = tmp_path / "planted"
    (dest / "vigil-20261027T030000Z.marker").symlink_to(target)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.run_backup(data, dest, now=NOW)
    assert not target.exists()
    assert sorted(p.name for p in dest.iterdir()) == ["vigil-20261027T030000Z.marker"]


def test_exclusive_create_refuses_a_symlink_that_appears_after_the_precheck(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "dest"
    target = tmp_path / "planted"
    real = backup._copy_vault

    def plant(*a: object, **k: object) -> list[str]:
        out = real(*a, **k)  # type: ignore[arg-type]
        (dest / "vigil-20261027T030000Z.marker").symlink_to(target)  # the race
        return out

    monkeypatch.setattr(backup, "_copy_vault", plant)
    with pytest.raises(FileExistsError):
        backup.run_backup(data, dest, now=NOW)
    assert not target.exists()
    assert not [p for p in dest.iterdir() if p.suffix in (".db", ".vault")]
    # not ours, so the failure path leaves it where it was
    assert (dest / "vigil-20261027T030000Z.marker").is_symlink()


def _set(
    dest: Path, when: datetime, *, db: bool = True, vault: bool = True, run_id: str = "a" * 16
) -> None:
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(exist_ok=True)
    (dest / f"vigil-{stamp}.marker").write_text(run_id)
    if db:
        (dest / f"vigil-{stamp}.db").write_bytes(b"x")
    if vault:
        (dest / f"vigil-{stamp}.vault").mkdir()


def test_freshness_passes_for_a_recent_complete_set(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    _set(dest, NOW - timedelta(hours=1))
    ok, note = backup.freshness(dest, now=NOW)
    assert ok and note == "newest set 20261027T020000Z, 1h old"


def test_freshness_fails_past_36_hours_and_gives_the_age(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    _set(dest, NOW - timedelta(hours=37))
    ok, note = backup.freshness(dest, now=NOW)
    assert not ok and "37h old" in note


def test_a_newer_incomplete_set_does_not_mask_an_older_complete_one(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    _set(dest, NOW - timedelta(hours=40))
    _set(dest, NOW - timedelta(hours=2), db=False, vault=False)
    _set(dest, NOW - timedelta(hours=3), vault=False)
    ok, note = backup.freshness(dest, now=NOW)
    assert not ok and "40h old" in note and "20261025T110000Z" in note


def test_unmarked_or_foreign_marker_sets_do_not_count(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    _set(dest, NOW - timedelta(hours=30))
    _set(dest, NOW - timedelta(hours=2), run_id="my own notes")
    stamp = (NOW - timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
    (dest / f"vigil-{stamp}.db").write_bytes(b"x")
    (dest / f"vigil-{stamp}.vault").mkdir()  # no marker at all
    ok, note = backup.freshness(dest, now=NOW)
    assert ok and "30h old" in note


def test_an_empty_destination_fails(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    dest.mkdir()
    ok, note = backup.freshness(dest, now=NOW)
    assert not ok and "no complete backup set" in note


def test_an_unreachable_destination_fails_without_naming_it(tmp_path: Path) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    for bad in (tmp_path / "secret-missing-dir", f):
        ok, note = backup.freshness(bad, now=NOW)
        assert not ok and "unreachable" in note
        assert str(tmp_path) not in note and bad.name not in note
