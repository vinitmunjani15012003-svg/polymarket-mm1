"""RiskCoordinator returns explicit risk decisions instead of loose booleans."""

from __future__ import annotations

from collections.abc import Iterable

from src.core.models.decision import RiskDecision


class RiskCoordinator:
    """Aggregate risk service outputs into a single explainable decision.

    Individual services own their domain-specific checks (data freshness,
    market/basis, inventory, capital).  The coordinator is deliberately small:
    it only ranks already-explicit :class:`RiskDecision` objects and preserves
    each child decision in metadata for logging/audit callers.
    """

    ACTION_PRIORITY = {
        "ALLOW": 0,
        "REDUCE_SIZE": 10,
        "REPAIR": 20,
        "CANCEL_SIDE": 30,
        "CANCEL": 40,
        "HALT": 50,
        "STOP": 50,
    }
    SEVERITY_PRIORITY = {"info": 0, "warning": 10, "error": 20, "critical": 30}

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

    def aggregate(self, decisions: Iterable[RiskDecision | None]) -> RiskDecision:
        """Return the strongest decision while retaining all domain outputs."""
        items = [d for d in decisions if d is not None]
        if not items:
            return RiskDecision("ALLOW", "NO_RISK_INPUTS", "info", {"decisions": []})

        winner = max(items, key=self._rank)
        metadata = dict(winner.metadata or {})
        metadata["decisions"] = [self._serialize(d) for d in items]
        metadata["blocking_reasons"] = [d.reason for d in items if d.action != "ALLOW"]
        return RiskDecision(winner.action, winner.reason, winner.severity, metadata)

    def evaluate(self, *, data: RiskDecision | None = None,
                 market: RiskDecision | None = None,
                 inventory: RiskDecision | None = None,
                 capital: RiskDecision | None = None,
                 stops: RiskDecision | None = None) -> RiskDecision:
        """Aggregate named data/market/inventory/capital/stop decisions."""
        return self.aggregate((data, market, inventory, capital, stops))

    @classmethod
    def _rank(cls, decision: RiskDecision) -> tuple[int, int]:
        return (
            cls.ACTION_PRIORITY.get(str(decision.action), 0),
            cls.SEVERITY_PRIORITY.get(str(decision.severity), 0),
        )

    @staticmethod
    def _serialize(decision: RiskDecision) -> dict:
        return {
            "action": decision.action,
            "reason": decision.reason,
            "severity": decision.severity,
            "metadata": dict(decision.metadata or {}),
        }
