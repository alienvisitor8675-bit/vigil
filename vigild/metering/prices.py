"""Cost is always tokens x the dated price table in configs/prices.yaml (B13)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigild.config import LongContext, ModelPrice, Prices

if TYPE_CHECKING:
    from vigild.metering.calls import CallUsage

PER = 1_000_000


class UnpricedModel(KeyError):
    pass


class PriceTable:
    def __init__(self, prices: Prices) -> None:
        self.prices = prices

    def has(self, model: str) -> bool:
        return model in self.prices.models

    def cost(self, model: str, u: CallUsage) -> float:
        try:
            p = self.prices.models[model]
        except KeyError as e:
            raise UnpricedModel(f"no price for {model} in configs/prices.yaml") from e
        rates: ModelPrice | LongContext = p
        prompt = (
            u.tokens_in + u.tokens_cache_read + u.tokens_cache_write_5m + u.tokens_cache_write_1h
        )
        if p.long_context and prompt > p.long_context.threshold:
            rates = p.long_context
        return (
            float(
                u.tokens_in * rates.input
                + u.tokens_out * rates.output
                + u.tokens_cache_read * rates.cache_read
                + u.tokens_cache_write_5m * rates.cache_write_5m
                + u.tokens_cache_write_1h * rates.cache_write_1h
            )
            / PER
        )
