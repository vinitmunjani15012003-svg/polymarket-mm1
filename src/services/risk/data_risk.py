"""Market data risk decisions."""

from __future__ import annotations

from src.core.models.decision import RiskDecision


def feed_freshness_decision(age_seconds: float, max_age_seconds: float, source: str = "unknown") -> RiskDecision:
    if age_seconds > max_age_seconds:
        return RiskDecision(
            action="CANCEL",
            reason="STALE_SPOT",
            severity="critical",
            metadata={"age_seconds": age_seconds, "max_age_seconds": max_age_seconds, "source": source},
        )
    return RiskDecision("ALLOW", "OK", "info", {"age_seconds": age_seconds, "source": source})
