"""Feed health/freshness models and helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FeedFreshness:
    age_ms: float
    healthy: bool
    source: str = "unknown"


def freshness(age_seconds: float, max_age_seconds: float, source: str = "unknown") -> FeedFreshness:
    age_ms = max(0.0, float(age_seconds or 0.0)) * 1000.0
    return FeedFreshness(age_ms=age_ms, healthy=age_seconds <= max_age_seconds, source=source)
