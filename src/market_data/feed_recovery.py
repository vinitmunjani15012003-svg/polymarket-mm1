"""Feed reconnect/recovery policy helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class RecoveryDecision:
    reconnect: bool
    backoff_seconds: float
    reason: str


def next_backoff(current: float, max_backoff: float = 30.0) -> float:
    return min(float(max_backoff), max(1.0, float(current or 1.0) * 2.0))


def should_reconnect(age_seconds: float, reconnect_stale_seconds: float) -> bool:
    return float(age_seconds or 0.0) >= float(reconnect_stale_seconds or 0.0)


def recovery_decision(
    *,
    age_seconds: float,
    reconnect_stale_seconds: float,
    current_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> RecoveryDecision:
    """Describe the existing reconnect decision without performing IO."""
    reconnect = should_reconnect(age_seconds, reconnect_stale_seconds)
    return RecoveryDecision(
        reconnect=reconnect,
        backoff_seconds=next_backoff(current_backoff, max_backoff) if reconnect else float(current_backoff or 1.0),
        reason="stale" if reconnect else "fresh",
    )
