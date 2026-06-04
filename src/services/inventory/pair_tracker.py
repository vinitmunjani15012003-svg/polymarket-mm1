"""Matched-pair accounting helpers."""

from __future__ import annotations

from src.core.models.inventory import MatchedPairEdgeStatus


def matched_pair_edge_status(pos, tolerance: float = 0.005) -> MatchedPairEdgeStatus:
    """Return service-owned FIFO pair edge metadata for risk/orchestration callers."""
    try:
        matched_pairs = float(pos.matched_pairs() or 0.0)
        pair_pnl = float(pos.matched_pair_profit() or 0.0)
    except Exception as exc:
        return MatchedPairEdgeStatus(
            triggered=False,
            tolerance=float(tolerance),
            reason="PAIR_EDGE_UNAVAILABLE",
            metadata={"error": str(exc)},
        )

    triggered = matched_pairs > 0 and pair_pnl < -float(tolerance)
    return MatchedPairEdgeStatus(
        triggered=triggered,
        matched_pairs=matched_pairs,
        pair_pnl=pair_pnl,
        tolerance=float(tolerance),
        reason="NEGATIVE_PAIR_EDGE" if triggered else "OK",
    )


def has_negative_matched_pair_edge(pos, tolerance: float = 0.005) -> bool:
    """True when FIFO-matched pairs have locked in negative edge."""
    return matched_pair_edge_status(pos, tolerance=tolerance).triggered
