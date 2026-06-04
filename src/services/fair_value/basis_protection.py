"""Basis and start-price protection for fair value."""

from __future__ import annotations

from typing import Optional

from .blender import clamp_probability
from .calibration import BASIS_GUARD_MAX_FV_DEVIATION
from .model import UpDownFairValue


def basis_guard_triggered(fair_value: float,
                          polymarket_mid_up: Optional[float],
                          threshold: float = BASIS_GUARD_MAX_FV_DEVIATION) -> bool:
    if polymarket_mid_up is None:
        return False
    fv = clamp_probability(fair_value)
    return abs(fv - polymarket_mid_up) >= threshold


def start_price_disagrees_with_market(start_price: float,
                                      current_spot: float,
                                      sigma: float,
                                      event_start_ts: float,
                                      resolve_ts: float,
                                      market_fv: Optional[float],
                                      threshold: float = 0.25,
                                      now_ts: Optional[float] = None) -> bool:
    """Return True when a candidate price-to-beat is implausible vs live books."""
    if market_fv is None or not start_price or not current_spot:
        return False
    model_fv = UpDownFairValue(
        event_start_ts=event_start_ts,
        resolve_ts=resolve_ts,
        start_price=start_price,
    ).fair_value(current_spot, sigma, now_ts=now_ts, update_state=False)
    return abs(clamp_probability(model_fv) - clamp_probability(market_fv)) >= threshold


def basis_check(fair_value: float,
                market_fv: float | None,
                threshold: float = BASIS_GUARD_MAX_FV_DEVIATION) -> dict:
    """Structured basis check for DecisionResult metadata."""
    if market_fv is None:
        return {"triggered": False, "basis_gap": None, "reason": "NO_MARKET_FV"}
    gap = abs(clamp_probability(fair_value) - clamp_probability(market_fv))
    return {
        "triggered": gap >= threshold,
        "basis_gap": gap,
        "threshold": threshold,
        "reason": "BASIS_GAP" if gap >= threshold else "OK",
    }
