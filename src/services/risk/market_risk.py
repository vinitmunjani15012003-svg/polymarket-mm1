"""Market-level risk decisions."""

from __future__ import annotations

from src.core.models.decision import RiskDecision


def basis_gap_decision(basis_gap: float | None, threshold: float) -> RiskDecision:
    if basis_gap is not None and float(basis_gap) >= float(threshold):
        return RiskDecision("CANCEL", "BASIS_GAP", "critical", {"basis_gap": basis_gap, "threshold": threshold})
    return RiskDecision("ALLOW", "OK", "info", {"basis_gap": basis_gap})
