"""Inventory risk decisions."""

from __future__ import annotations

from src.core.models.decision import RiskDecision
from src.services.inventory.pair_tracker import matched_pair_edge_status


def imbalance_decision(imbalance: float, hard_limit: float) -> RiskDecision:
    if abs(float(imbalance or 0.0)) >= float(hard_limit or 0.0):
        return RiskDecision("REPAIR", "HARD_INVENTORY_LIMIT", "warning", {"imbalance": imbalance, "hard_limit": hard_limit})
    return RiskDecision("ALLOW", "OK", "info", {"imbalance": imbalance})


def negative_pair_edge_decision(pos, tolerance: float = 0.005) -> RiskDecision:
    """Fail closed when matched FIFO pairs have locked in negative edge."""
    status = matched_pair_edge_status(pos, tolerance=tolerance)
    if status.triggered:
        return RiskDecision(
            "HALT",
            "NEGATIVE_PAIR_EDGE",
            "critical",
            {
                "matched_pairs": status.matched_pairs,
                "pair_pnl": status.pair_pnl,
                "tolerance": status.tolerance,
                "source": "pair_tracker",
            },
        )
    return RiskDecision(
        "ALLOW",
        status.reason,
        "info",
        {
            "matched_pairs": status.matched_pairs,
            "pair_pnl": status.pair_pnl,
            "tolerance": status.tolerance,
            "source": "pair_tracker",
            **dict(status.metadata or {}),
        },
    )
