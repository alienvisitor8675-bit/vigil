"""vigild's own calls settle exactly once by call_key (#29)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

import vigild.gateway.service as gsvc
from tests.jobs.test_round9 import PA, Up, _booked, _metered, _setup
from vigild.config import Config


def _haiku(gw: Any) -> None:
    gw.call(repo=PA, preset="claude-haiku", purpose="decider", job_id=None, private=False,
            body={"max_tokens": 100, "messages": [{"role": "user", "content": "q"}]})  # fmt: skip


def _page(store: Any) -> Any:
    (page,) = [i for i in store.list_inbox() if "metering failed" in i.title]
    return page


def test_failure_before_the_insert_books_the_reservation(cfg: Config, monkeypatch: Any) -> None:
    store, _b, gw, _s, _c = _setup(cfg, monkeypatch, Up())

    def boom(*a: Any, **k: Any) -> float:
        raise RuntimeError("before insert")

    monkeypatch.setattr(gsvc, "record_call", boom)
    _haiku(gw)
    reserved = store.q("SELECT amount_usd a FROM reservations")[0]["a"]
    assert _booked(store) == pytest.approx(reserved)
    assert _metered(store) == 0
    assert _page(store).body["row_written"] is False


def test_failure_after_the_commit_books_the_row_once(cfg: Config, monkeypatch: Any) -> None:
    store, _b, gw, _s, _c = _setup(cfg, monkeypatch, Up())
    real = gsvc.record_call

    def late(*a: Any, **k: Any) -> float:
        real(*a, **k)
        raise RuntimeError("after commit")

    monkeypatch.setattr(gsvc, "record_call", late)
    _haiku(gw)
    assert store.q("SELECT COUNT(*) n FROM model_calls")[0]["n"] == 1
    assert _metered(store) > 0
    assert _booked(store) == pytest.approx(_metered(store))
    assert _page(store).body["row_written"] is True


def test_concurrent_own_calls_are_each_counted_once(cfg: Config, monkeypatch: Any) -> None:
    store, _b, gw, _s, _c = _setup(cfg, monkeypatch, Up())
    n = 8
    errors: list[BaseException] = []

    def one() -> None:
        try:
            _haiku(gw)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.q("SELECT COUNT(DISTINCT call_key) n FROM model_calls")[0]["n"] == n
    assert store.q("SELECT COUNT(*) n FROM model_calls")[0]["n"] == n
    assert _booked(store) == pytest.approx(_metered(store))
