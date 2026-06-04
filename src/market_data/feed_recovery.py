"""Feed reconnect/recovery policy."""

from __future__ import annotations


def next_backoff(current: float, max_backoff: float = 30.0) -> float:
    return min(max_backoff, max(1.0, float(current or 1.0) * 2.0))


def should_reconnect(age_seconds: float, reconnect_stale_seconds: float) -> bool:
    return float(age_seconds or 0.0) >= float(reconnect_stale_seconds or 0.0)
