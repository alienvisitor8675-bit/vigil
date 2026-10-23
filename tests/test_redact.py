"""The one redactor: token shapes and PEM private keys (phase 1 run, finding #1),
and the stored-data scrub that re-applies it."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from vigild import db
from vigild.db import Store
from vigild.redact import redact
from vigild.verifier import RunResult, Verifier

# fake, assembled so no whole block sits in the source
BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\nabcdEFGH1234+/ijkl=="
BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
END = "-----END " + "PRIVATE KEY-----"


@pytest.mark.parametrize("kind", ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY"])
def test_a_pem_block_with_real_newlines_is_redacted(kind: str) -> None:
    text = f"before\n-----BEGIN {kind}-----\n{BODY}\n-----END {kind}-----\nafter"
    assert redact(text) == "before\n[redacted]\nafter"


def test_a_pem_block_with_escaped_newlines_in_a_json_string_is_redacted() -> None:
    stored = json.dumps({"tail": f"x {BEGIN}\n{BODY}\n{END}\n y", "n": 1})
    assert "\\n" in stored and BEGIN in stored
    out = redact(stored)
    assert "MIIEvQ" not in out and "abcdEFGH" not in out
    assert json.loads(out)["n"] == 1  # still parses


def test_redacting_a_value_then_dumping_round_trips() -> None:
    value = {"a": [f"{BEGIN}\n{BODY}\n{END}", "ok"], "b": {"c": 2}}
    out = redact(value)
    assert json.loads(json.dumps(out)) == out
    assert out == {"a": ["[redacted]", "ok"], "b": {"c": 2}}


def test_a_truncated_pem_with_no_end_is_redacted_to_the_end_of_the_string() -> None:
    assert redact(f"log {BEGIN}\n{BODY}") == "log [redacted]"
    stored = json.dumps({"tail": f"log {BEGIN}\n{BODY}", "n": 1})
    assert json.loads(redact(stored)) == {"tail": "log [redacted]", "n": 1}


def test_a_public_key_and_plain_text_are_left_alone() -> None:
    text = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\nPRIVATE KEY is a phrase"
    assert redact(text) == text


def test_the_verifiers_output_tail_is_redacted() -> None:
    """The error path stored `r.output[-TAIL:]` as it came."""

    class Runner:
        def run(self, *a: Any, **k: Any) -> RunResult:
            return RunResult(1, f"boom\n{BEGIN}\n{BODY}\n{END}\n")

    class Ws:
        def verify_checkout(self, *a: Any, **k: Any) -> Path:
            d = Path(tempfile.mkdtemp()) / "src"
            d.mkdir()
            return d

    v = Verifier(Ws(), Runner())  # type: ignore[arg-type]
    out = v._run("r", "img", "n", "sha", "pytest", "pytest", [])
    assert "MIIEvQ" not in out["tail"] and "[redacted]" in out["tail"]


def _pem_db(path: Path) -> tuple[str, str]:
    """A DB that stored a private key unredacted in an inbox body, an event and an
    artifact, beside clean rows."""
    dirty = json.dumps(
        {"title": "t", "tail": f"oops {BEGIN}\n{BODY}\n{END}\n", "n": 1}, sort_keys=True
    )
    clean = json.dumps({"title": "fine", "tail": "all good", "n": 2}, sort_keys=True)
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    for body in (dirty, clean):
        conn.execute(
            "INSERT INTO inbox_items (queue, kind, title, body, created_at, provenance)"
            " VALUES ('questions','task_failed','t',?,'2026-10-22T00:00:00+00:00','human')",
            (body,),
        )
    conn.execute(
        "INSERT INTO events (ts, type, data, provenance)"
        " VALUES ('2026-10-22T00:00:00+00:00','task_failed',?,'human')",
        (dirty,),
    )
    conn.execute(
        "INSERT INTO artifacts (job_id, name, content, sha256, created_at, provenance)"
        " VALUES ('j','tests.json',?,'x','2026-10-22T00:00:00+00:00','human')",
        (dirty,),
    )
    conn.commit()
    conn.close()
    return dirty, clean


def test_a_stored_private_key_is_scrubbed_and_clean_rows_are_untouched(tmp_path: Path) -> None:
    path = tmp_path / "vigil.db"
    _, clean = _pem_db(path)
    store = Store.open(path)
    bodies = [r["body"] for r in store.q("SELECT body FROM inbox_items ORDER BY id")]
    assert "MIIEvQ" not in bodies[0] and json.loads(bodies[0])["tail"] == "oops [redacted]\n"
    assert bodies[1] == clean  # byte-identical
    assert store.q("SELECT status FROM inbox_items WHERE id=1")[0][0] == "open"
    assert "MIIEvQ" not in store.q("SELECT data FROM events")[0][0]
    art = store.q("SELECT content, sha256 FROM artifacts")[0]
    assert "MIIEvQ" not in art["content"]
    assert art["sha256"] == hashlib.sha256(art["content"].encode()).hexdigest()
    store.conn.close()
    again = Store.open(path)  # idempotent
    assert [r["body"] for r in again.q("SELECT body FROM inbox_items ORDER BY id")] == bodies


def test_a_double_encoded_json_with_an_unterminated_pem_still_parses() -> None:
    inner = json.dumps({"k": f"x {BEGIN}\n{BODY}"})
    outer = json.dumps({"o": inner, "p": "z"})
    out = json.loads(redact(outer))
    assert out["p"] == "z" and "MIIEvQ" not in out["o"]
    assert json.loads(out["o"]) == {"k": "x [redacted]"}


def test_the_migration_step_directly_is_idempotent_and_leaves_clean_rows(tmp_path: Path) -> None:
    path = tmp_path / "vigil.db"
    _, clean = _pem_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db._m5_scrub_private_keys(conn)
    first = [r[0] for r in conn.execute("SELECT body FROM inbox_items ORDER BY id")]
    assert "MIIEvQ" not in first[0] and first[1] == clean
    db._m5_scrub_private_keys(conn)
    assert [r[0] for r in conn.execute("SELECT body FROM inbox_items ORDER BY id")] == first
