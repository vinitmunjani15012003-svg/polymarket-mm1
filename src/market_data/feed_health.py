"""Feed health/freshness models and helpers.

These helpers are deliberately read-only and side-effect free. They provide the
new market_data boundary with one place to describe feed age semantics while the
legacy ``PriceFeed`` remains the live implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(slots=True, frozen=True)
class FeedFreshness:
    """Normalized freshness snapshot for a market-data source."""

    age_ms: float
    healthy: bool
    source: str = "unknown"

    @property
    def age_seconds(self) -> float:
        """Return age in seconds for compatibility with existing risk checks."""
        return self.age_ms / 1000.0

    def as_dict(self) -> dict[str, Any]:
        return {"age_ms": self.age_ms, "age_seconds": self.age_seconds, "healthy": self.healthy, "source": self.source}


def _coerce_age_seconds(age_seconds: float | int | None) -> float:
    if age_seconds is None:
        return float("inf")
    try:
        age = float(age_seconds)
    except (TypeError, ValueError):
        return float("inf")
    if math.isnan(age):
        return float("inf")
    return max(0.0, age)


def freshness(age_seconds: float, max_age_seconds: float, source: str = "unknown") -> FeedFreshness:
    """Build a normalized freshness object.

    ``healthy`` intentionally uses ``<=`` to preserve the existing fail-closed
    stale threshold used by pre-trade risk checks.
    """
    age = _coerce_age_seconds(age_seconds)
    try:
        max_age = float(max_age_seconds)
    except (TypeError, ValueError):
        max_age = 0.0
    healthy = math.isfinite(age) and age <= max(0.0, max_age)
    age_ms = age * 1000.0 if math.isfinite(age) else float("inf")
    return FeedFreshness(age_ms=age_ms, healthy=healthy, source=source)


def freshness_from_timestamp(ts: float | None, max_age_seconds: float, *, now: float, source: str = "unknown") -> FeedFreshness:
    """Read-only helper for converting a last-update timestamp into freshness."""
    if ts is None:
        return freshness(float("inf"), max_age_seconds, source=source)
    return freshness(float(now) - float(ts), max_age_seconds, source=source)
