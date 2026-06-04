"""Inventory risk decisions."""

from __future__ import annotations

from src.core.models.decision import RiskDecision


def imbalance_decision(imbalance: float, hard_limit: float) -> RiskDecision:
    if abs(float(imbalance or 0.0)) >= float(hard_limit or 0.0):
        return RiskDecision("REPAIR", "HARD_INVENTORY_LIMIT", "warning", {"imbalance": imbalance, "hard_limit": hard_limit})
    return RiskDecision("ALLOW", "OK", "info", {"imbalance": imbalance})
