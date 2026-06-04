"""Matched-pair accounting helpers."""

from __future__ import annotations


def has_negative_matched_pair_edge(pos, tolerance: float = 0.005) -> bool:
    """True when FIFO-matched pairs have locked in negative edge."""
    try:
        return float(pos.matched_pairs() or 0) > 0 and float(pos.matched_pair_profit()) < -float(tolerance)
    except Exception:
        return False
