"""RiskCoordinator returns explicit risk decisions instead of loose booleans."""

from __future__ import annotations

from src.core.models.decision import RiskDecision


class RiskCoordinator:
    def __init__(self, risk_engine=None):
        self.risk_engine = risk_engine

    def evaluate_stops(self, current_pnl: float) -> RiskDecision:
        if self.risk_engine is None:
            return RiskDecision("ALLOW", "NO_RISK_ENGINE", "info")
        if getattr(self.risk_engine, "halted", False):
            return RiskDecision("HALT", getattr(self.risk_engine, "halt_reason", "HALTED"), "critical")
        ok = self.risk_engine.check_stops(current_pnl)
        if ok:
            return RiskDecision("ALLOW", "OK", "info")
        return RiskDecision("HALT", getattr(self.risk_engine, "halt_reason", "STOP_TRIGGERED") or "STOP_TRIGGERED", "critical")
