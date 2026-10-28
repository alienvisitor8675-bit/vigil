"""Per-call metering: tokens x the dated price table, any provider."""

from __future__ import annotations

import json

import pytest

from vigild.config import Config
from vigild.db import Store
from vigild.metering.calls import CallUsage, SSEUsageTap, record_call, usage_from_anthropic
from vigild.metering.prices import PriceTable, UnpricedModel


def test_sonnet_price(cfg: Config) -> None:
    pt = PriceTable(cfg.prices)
    u = CallUsage(
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        tokens_cache_read=1_000_000,
        tokens_cache_write_5m=1_000_000,
        tokens_cache_write_1h=1_000_000,
    )
    assert pt.cost("claude-sonnet-5-5", u) == pytest.approx(2 + 10 + 0.10 + 2.50 + 4)


def test_haiku_long_context_rates(cfg: Config) -> None:
    pt = PriceTable(cfg.prices)
    short = CallUsage(tokens_in=100_000, tokens_out=0)
    long = CallUsage(tokens_in=100_001, tokens_out=0)
    assert pt.cost("claude-haiku-5-5", short) == pytest.approx(0.01)
    assert pt.cost("claude-haiku-5-5", long) == pytest.approx(100_001 * 0.50 / 1e6)


def test_unknown_model_is_refused(cfg: Config) -> None:
    with pytest.raises(UnpricedModel):
        PriceTable(cfg.prices).cost("claude-mystery", CallUsage(tokens_in=1))


def test_usage_from_anthropic_splits_cache_writes() -> None:
    u = usage_from_anthropic(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
            "cache_creation": {"ephemeral_5m_input_tokens": 15, "ephemeral_1h_input_tokens": 25},
        }
    )
    assert (u.tokens_in, u.tokens_out, u.tokens_cache_read) == (10, 20, 30)
    assert (u.tokens_cache_write_5m, u.tokens_cache_write_1h) == (15, 25)
    assert usage_from_anthropic({"cache_creation_input_tokens": 7}).tokens_cache_write_5m == 7


def _sse(*events: dict[str, object]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def test_sse_tap_reads_usage_across_chunk_boundaries() -> None:
    stream = _sse(
        {
            "type": "message_start",
            "message": {
                "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 900},
            },
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    )
    tap = SSEUsageTap()
    for i in range(0, len(stream), 7):  # feed in awkward chunks
        tap.feed(stream[i : i + 7])
    assert tap.model == "claude-sonnet-5-5"
    u = tap.usage()
    assert (u.tokens_in, u.tokens_out, u.tokens_cache_read) == (100, 42, 900)


def test_record_call_writes_the_row_and_session_totals(cfg: Config, store: Store) -> None:
    from vigild.jobs.model import Role

    s = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    cost = record_call(
        store,
        PriceTable(cfg.prices),
        session_id=s.id,
        task_id=None,
        job_id=None,
        purpose="worker",
        provider="anthropic",
        backend_preset="claude-sonnet",
        model="claude-sonnet-5-5",
        usage=CallUsage(tokens_in=1000, tokens_out=500),
        provider_cost_usd=0.0071,
    )
    assert cost == pytest.approx(1000 * 2 / 1e6 + 500 * 10 / 1e6)
    row = store.q("SELECT * FROM model_calls")[0]
    assert row["provider_cost_usd"] == pytest.approx(0.0071) and row["provenance"] == "claude"
    got = store.get_session(s.id)
    assert got.cost_usd == pytest.approx(cost) and got.tokens_in == 1000


def test_per_call_rows_sum_to_the_session_total(cfg: Config, store: Store) -> None:
    from vigild.jobs.model import Role

    s = store.create_session(None, Role.WORKER, backend_preset="claude-sonnet")
    pt = PriceTable(cfg.prices)
    for n in (1, 2, 3):
        record_call(
            store,
            pt,
            session_id=s.id,
            task_id=None,
            job_id=None,
            purpose="worker",
            provider="anthropic",
            backend_preset="claude-sonnet",
            model="claude-sonnet-5-5",
            usage=CallUsage(tokens_in=n * 1000, tokens_out=n * 100),
        )
    total = store.q("SELECT SUM(cost_usd) c FROM model_calls WHERE session_id=?", (s.id,))[0]["c"]
    assert store.get_session(s.id).cost_usd == pytest.approx(total)
    assert total == pytest.approx(
        pt.cost("claude-sonnet-5-5", CallUsage(tokens_in=6000, tokens_out=600))
    )


def test_the_same_call_key_recorded_twice_is_one_row(cfg: Config) -> None:
    store = Store.open(":memory:")
    pt = PriceTable(cfg.prices)
    kw: dict = dict(
        session_id=None, task_id=None, job_id=None, purpose="decider", provider="anthropic",
        backend_preset="claude-haiku", model="claude-haiku-5-5", call_key="k-1",
    )  # fmt: skip
    first = record_call(store, pt, usage=CallUsage(tokens_in=1000, tokens_out=10), **kw)
    again = record_call(store, pt, usage=CallUsage(tokens_in=5000, tokens_out=99), **kw)
    assert again == first > 0
    assert store.q("SELECT COUNT(*) n FROM model_calls WHERE call_key='k-1'")[0]["n"] == 1
