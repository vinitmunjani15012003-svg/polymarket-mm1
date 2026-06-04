"""Simple feed cache abstraction."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class PriceTick:
    price: float
    ts: float
    source: str


class FeedCache:
    def __init__(self):
        self._ticks: dict[str, PriceTick] = {}

    def set(self, symbol: str, price: float, source: str, ts: float | None = None):
        self._ticks[symbol.upper()] = PriceTick(float(price), ts or time.time(), source)

    def get(self, symbol: str) -> PriceTick | None:
        return self._ticks.get(symbol.upper())
