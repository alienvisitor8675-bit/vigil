"""One row per model call: tokens in, out, cache read, cache write; cost
from the price table; the provider's own cost when it reports one."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from vigild.db import Store, iso
from vigild.metering.prices import PriceTable
from vigild.provenance import Provenance


@dataclass
class CallUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write_5m: int = 0
    tokens_cache_write_1h: int = 0


def usage_from_anthropic(u: dict[str, Any]) -> CallUsage:
    cc = u.get("cache_creation") or {}
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if not cc:
        w5 = int(u.get("cache_creation_input_tokens") or 0)
    return CallUsage(
        tokens_in=int(u.get("input_tokens") or 0),
        tokens_out=int(u.get("output_tokens") or 0),
        tokens_cache_read=int(u.get("cache_read_input_tokens") or 0),
        tokens_cache_write_5m=w5,
        tokens_cache_write_1h=w1,
    )


class SSEUsageTap:
    """Reads usage out of an Anthropic SSE stream as it passes through.
    `message_start` carries input and cache tokens; `message_delta` carries
    the cumulative output count."""

    def __init__(self) -> None:
        self._buf = b""
        self.model: str | None = None
        self._start: dict[str, Any] = {}
        self._out: int | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n\n" in self._buf:
            block, self._buf = self._buf.split(b"\n\n", 1)
            for line in block.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "message_start":
                    msg = ev.get("message", {})
                    self.model = msg.get("model", self.model)
                    self._start = dict(msg.get("usage") or {})
                elif ev.get("type") == "message_delta" and ev.get("usage"):
                    usage = ev["usage"]
                    if "output_tokens" in usage:
                        self._out = int(usage["output_tokens"])
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ):
                        if usage.get(k) is not None:
                            self._start[k] = usage[k]

    def usage(self) -> CallUsage:
        u = usage_from_anthropic(self._start)
        if self._out is not None:
            u.tokens_out = self._out
        return u


def record_call(
    store: Store,
    prices: PriceTable,
    *,
    session_id: str | None,
    task_id: str | None,
    job_id: str | None,
    purpose: str,
    provider: str,
    backend_preset: str | None,
    model: str,
    usage: CallUsage,
    provider_cost_usd: float | None = None,
    status: int | None = 200,
    latency_ms: int | None = None,
    timings: dict[str, Any] | None = None,
    provenance: Provenance = Provenance.CLAUDE,
) -> float:
    cost = prices.cost(model, usage)
    store.x(
        "INSERT INTO model_calls (session_id, task_id, job_id, purpose, provider, backend_preset,"
        " model, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write_5m,"
        " tokens_cache_write_1h, cost_usd, provider_cost_usd, status, latency_ms, timings, ts,"
        " provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            task_id,
            job_id,
            purpose,
            provider,
            backend_preset,
            model,
            usage.tokens_in,
            usage.tokens_out,
            usage.tokens_cache_read,
            usage.tokens_cache_write_5m,
            usage.tokens_cache_write_1h,
            cost,
            provider_cost_usd,
            status,
            latency_ms,
            json.dumps(timings) if timings else None,
            iso(),
            provenance.value,
        ),
    )
    if session_id:
        prompt = usage.tokens_in + usage.tokens_cache_read + usage.tokens_cache_write_5m
        prompt += usage.tokens_cache_write_1h
        store.add_session_usage(session_id, cost, prompt, usage.tokens_out)
    return cost


def session_cost(store: Store, session_id: str) -> float:
    row = store.q(
        "SELECT COALESCE(SUM(cost_usd), 0) c FROM model_calls WHERE session_id=?", (session_id,)
    )
    return float(row[0]["c"])
