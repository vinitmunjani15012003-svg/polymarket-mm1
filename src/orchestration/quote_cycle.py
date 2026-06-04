"""Quote-cycle lifecycle seams for MarketCycler.

The quote loop is still intentionally hosted in ``MarketCycler``; this module
provides a small typed boundary for the mutable inputs used by one iteration so
future lifecycle extraction can happen without changing trading semantics.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from src.data.market_discovery import MarketInfo


@dataclass(frozen=True)
class QuoteCycleContext:
    """Immutable inputs captured at the start of one quote-cycle iteration."""

    market: "MarketInfo"
    now: float
    remaining: float

    @classmethod
    def from_market(cls, market: "MarketInfo", now: float) -> "QuoteCycleContext":
        return cls(market=market, now=now, remaining=market.time_remaining)
