"""Owner, #49 round 2: every review_cases fixture points at a file that exists at that
job's recorded head, read from vigild's vault. The fixtures' file and line were
once guessed; this keeps that from coming back.

The vault is real data, so the test runs only when `VIGIL_VAULT_CHECK_DIR` names a
vigild data dir. It opens the DB read-only and only reads the vault (no `init`, no
fetch): `VIGIL_VAULT_CHECK_DIR=~/vigil-data uv run pytest tests/review/test_review_cases_exist.py`."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from vigild.gitsafe import git
from vigild.review.replay import load_cases

DATA = os.environ.get("VIGIL_VAULT_CHECK_DIR")
pytestmark = pytest.mark.skipif(not DATA, reason="set VIGIL_VAULT_CHECK_DIR to a vigild data dir")


def test_every_fixture_file_exists_at_its_head() -> None:
    data = Path(str(DATA)).expanduser()
    db = sqlite3.connect(f"file:{data / 'vigil.db'}?mode=ro", uri=True)
    try:
        missing: list[str] = []
        for case in load_cases():
            row = db.execute("SELECT repo FROM jobs WHERE id=?", (case.job_id,)).fetchone()
            assert row is not None, f"{case.id}: no job {case.job_id} in {data}"
            owner, name = str(row[0]).split("/")
            vault = data / "vault" / owner / f"{name}.git"
            assert vault.is_dir(), f"{case.id}: no vault for {row[0]}"
            for exp in case.expected:
                entry = git(vault, "ls-tree", "-z", case.head, "--", exp.file).rstrip("\0")
                if not entry or " blob " not in entry.split("\t")[0]:
                    missing.append(f"{case.id}: {exp.file} at {case.head[:9]}")
                    continue
                blob = git(vault, "cat-file", "blob", entry.split()[2])
                if exp.line > len(blob.splitlines()):
                    missing.append(f"{case.id}: {exp.file} has no line {exp.line}")
        assert missing == []
    finally:
        db.close()
