"""vigil doctor fails a dev or session image whose key at main's tip isn't the
newest validated record (#75: doctor said "pass" on a stale image). Fakes, no Docker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vigild.db import Store
from vigild.doctor import stale_image_checks

REPO = "thomasbardhi01/vigil"


class FakeDev:
    def __init__(self, rows: list[dict[str, Any]], key: str = "a" * 64) -> None:
        self.rows, self._key = rows, key

    def key(self, repo: str, sha: str, source: str = "mirror") -> str:
        return self._key

    def tag(self, repo: str, key: str) -> str:
        return f"vigil-dev/vigil:{key[:16]}"

    def newest_validated(self, repo: str, stages: tuple[str, ...]) -> dict[str, Any] | None:
        ok = [r for r in self.rows if r["ok"] and r["stage"] in stages]
        return ok[-1] if ok else None


class FakeSessions:
    def __init__(self, dev: FakeDev, key: str) -> None:
        self.dev, self._key = dev, key

    def key(self, dev_tag: str) -> str:
        return self._key


def _row(key: str, stage: str = "validate", ok: bool = True) -> dict[str, Any]:
    return {"key": key, "stage": stage, "ok": ok}


def test_a_stale_dev_image_fails_naming_both_keys() -> None:
    dev = FakeDev([_row("b" * 64)])
    out = stale_image_checks(dev, None, REPO, "tip")
    assert [(c.name, c.ok) for c in out] == [(f"dev image fresh: {REPO}", False)]
    msg = out[0].note
    assert "key at main aaaaaaaaaaaa" in msg and "newest validated bbbbbbbbbbbb" in msg
    assert f"vigil dev-image --repo {REPO}" in msg


def test_a_fresh_dev_image_passes() -> None:
    dev = FakeDev([_row("b" * 64), _row("a" * 64)])
    out = stale_image_checks(dev, None, REPO, "tip")
    assert [c.ok for c in out] == [True]


def test_an_older_validated_record_does_not_hide_a_newer_stale_one() -> None:
    # the newest validated record is what counts, not any validated record
    dev = FakeDev([_row("a" * 64), _row("b" * 64)])
    assert stale_image_checks(dev, None, REPO, "tip")[0].ok is False


def test_the_session_image_key_is_checked_too() -> None:
    dev = FakeDev([_row("a" * 64), _row("c" * 64, "session-validate")])
    out = stale_image_checks(dev, FakeSessions(dev, "d" * 64), REPO, "tip")  # type: ignore[arg-type]
    assert [(c.name, c.ok) for c in out] == [
        (f"dev image fresh: {REPO}", True),
        (f"session image fresh: {REPO}", False),
    ]
    assert "key at main dddddddddddd, newest validated cccccccccccc" in out[1].note
    ok = stale_image_checks(dev, FakeSessions(dev, "c" * 64), REPO, "tip")  # type: ignore[arg-type]
    assert all(c.ok for c in ok)


def test_no_validated_record_fails() -> None:
    out = stale_image_checks(FakeDev([]), None, REPO, "tip")
    assert out[0].ok is False and "never validated" in out[0].note


def test_devimages_newest_validated_reads_the_table(tmp_path: Path, cfg: Any) -> None:
    from vigild.devimage import DevImages

    store = Store.open(tmp_path / "v.db")
    for key, stage, ok in [
        ("k1", "validate", 1),
        ("k2", "validate", 0),
        ("k3", "session-validate", 1),
    ]:
        store.x(
            "INSERT INTO dev_images (repo, image, image_id, ok, output, validated_at, key, stage,"
            " base_sha) VALUES (?,?,?,?,?,?,?,?,?)",
            (REPO, "i", "id", ok, "{}", "2026-10-28T00:00:00Z", key, stage, "s"),
        )
    dev = DevImages(cfg, store, None, None, None)  # type: ignore[arg-type]
    assert dev.newest_validated(REPO, ("validate",))["key"] == "k1"  # type: ignore[index]
    assert dev.newest_validated(REPO, ("session-validate",))["key"] == "k3"  # type: ignore[index]
