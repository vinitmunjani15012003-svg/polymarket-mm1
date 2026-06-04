"""Capital risk decisions."""

from __future__ import annotations

from src.core.models.decision import RiskDecision


def capital_available_decision(available: float, required: float) -> RiskDecision:
    if float(available or 0.0) < float(required or 0.0):
        return RiskDecision("REDUCE_SIZE", "INSUFFICIENT_CAPITAL", "warning", {"available": available, "required": required})
    return RiskDecision("ALLOW", "OK", "info", {"available": available, "required": required})
