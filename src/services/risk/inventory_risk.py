"""Inventory risk decisions."""

from __future__ import annotations

from src.core.models.decision import RiskDecision
from src.services.inventory.pair_tracker import has_negative_matched_pair_edge


def imbalance_decision(imbalance: float, hard_limit: float) -> RiskDecision:
    if abs(float(imbalance or 0.0)) >= float(hard_limit or 0.0):
        return RiskDecision("REPAIR", "HARD_INVENTORY_LIMIT", "warning", {"imbalance": imbalance, "hard_limit": hard_limit})
    return RiskDecision("ALLOW", "OK", "info", {"imbalance": imbalance})


def negative_pair_edge_decision(pos, tolerance: float = 0.005) -> RiskDecision:
    """Fail closed when matched FIFO pairs have locked in negative edge."""
    if has_negative_matched_pair_edge(pos, tolerance=tolerance):
        matched_pairs = 0.0
        pair_pnl = 0.0
        try:
            matched_pairs = float(pos.matched_pairs() or 0.0)
            pair_pnl = float(pos.matched_pair_profit() or 0.0)
        except Exception:
            pass
        return RiskDecision(
            "HALT",
            "NEGATIVE_PAIR_EDGE",
            "critical",
            {"matched_pairs": matched_pairs, "pair_pnl": pair_pnl, "tolerance": tolerance},
        )
    return RiskDecision("ALLOW", "OK", "info", {"tolerance": tolerance})
