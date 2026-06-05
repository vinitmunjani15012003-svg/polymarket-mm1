"""Simple feed cache abstraction.

The cache facade is intentionally tiny and deterministic so it can be used in
unit tests and future wiring without changing the live PriceFeed behaviour.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from .feed_health import FeedFreshness, freshness_from_timestamp


@dataclass(slots=True, frozen=True)
class PriceTick:
    price: float
    ts: float
    source: str

    def age_seconds(self, now: float | None = None) -> float:
        return max(0.0, float(now if now is not None else time.time()) - float(self.ts))


class FeedCache:
    def __init__(self):
        self._ticks: dict[str, PriceTick] = {}

    @staticmethod
    def _key(symbol: str) -> str:
        return str(symbol).upper()

    def set(self, symbol: str, price: float, source: str, ts: float | None = None) -> PriceTick:
        tick = PriceTick(float(price), ts if ts is not None else time.time(), source)
        self._ticks[self._key(symbol)] = tick
        return tick

    def get(self, symbol: str) -> PriceTick | None:
        return self._ticks.get(self._key(symbol))

    def freshness(self, symbol: str, max_age_seconds: float, *, now: float | None = None) -> FeedFreshness:
        tick = self.get(symbol)
        return freshness_from_timestamp(
            tick.ts if tick else None,
            max_age_seconds,
            now=now if now is not None else time.time(),
            source=tick.source if tick else "missing",
        )

    def snapshot(self) -> dict[str, PriceTick]:
        """Return a shallow read-only-by-convention snapshot of cached ticks."""
        return dict(self._ticks)

    def symbols(self) -> Iterable[str]:
        return tuple(self._ticks.keys())
