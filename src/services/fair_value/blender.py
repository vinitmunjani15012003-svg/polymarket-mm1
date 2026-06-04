"""Fair-value blending and probability helpers."""

from __future__ import annotations

from typing import Optional

from .calibration import (
    FV_HARD_DISAGREEMENT,
    FV_TAIL_BLEND_GUARD,
    FV_TAIL_MAX_MARKET_PULL,
    MAX_TRADING_FV_MARKET_DEVIATION,
)


def clamp_probability(value: float, lo: float = 0.01, hi: float = 0.99) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return 0.50


def spot_from_binary_probability(start_price: float,
                                 p_up: float,
                                 sigma: float,
                                 time_remaining: float) -> Optional[float]:
    """Invert binary P(Up) into the live spot implied by market probability."""
    if not start_price or p_up is None or not sigma or time_remaining <= 0:
        return None
    try:
        from scipy.stats import norm
        import math
        p = max(0.02, min(0.98, float(p_up)))
        t_years = max(1.0, float(time_remaining)) / (365.25 * 86400)
        return float(start_price) * math.exp(norm.ppf(p) * float(sigma) * math.sqrt(t_years))
    except Exception:
        return None


def polymarket_implied_up_mid(book_up, book_down) -> Optional[float]:
    """Estimate Polymarket-implied P(Up) from YES and NO order books."""
    mids = []
    if book_up and getattr(book_up, "best_bid", 0) > 0 and getattr(book_up, "best_ask", 0) > 0:
        mids.append((float(book_up.best_bid) + float(book_up.best_ask)) / 2.0)
    if book_down and getattr(book_down, "best_bid", 0) > 0 and getattr(book_down, "best_ask", 0) > 0:
        down_mid = (float(book_down.best_bid) + float(book_down.best_ask)) / 2.0
        mids.append(1.0 - down_mid)
    if not mids:
        return None
    return max(0.0, min(1.0, sum(mids) / len(mids)))


def blended_fair_value(model_fv: float,
                       market_fv: Optional[float],
                       confidence: float) -> float:
    model = clamp_probability(model_fv)
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    if market_fv is None:
        return clamp_probability(0.5 + (model - 0.5) * conf)
    market = clamp_probability(market_fv)
    if model <= FV_TAIL_BLEND_GUARD and market > model and (market - model) <= FV_HARD_DISAGREEMENT:
        return clamp_probability(model + min(FV_TAIL_MAX_MARKET_PULL, (market - model) * (1.0 - conf)))
    if model >= 1.0 - FV_TAIL_BLEND_GUARD and market < model and (model - market) <= FV_HARD_DISAGREEMENT:
        return clamp_probability(model - min(FV_TAIL_MAX_MARKET_PULL, (model - market) * (1.0 - conf)))
    return clamp_probability(conf * model + (1.0 - conf) * market)


def weighted_fair_value(model_fv: float, market_fv: float | None, confidence: float) -> float:
    """Alias for callers that prefer explicit weighted terminology."""
    return blended_fair_value(model_fv, market_fv, confidence)


def cap_fair_value_to_market(fair_value: float,
                             market_fv: Optional[float],
                             max_deviation: float = MAX_TRADING_FV_MARKET_DEVIATION) -> float:
    fv = clamp_probability(fair_value)
    if market_fv is None:
        return fv
    market = clamp_probability(market_fv)
    max_dev = max(0.0, float(max_deviation or 0.0))
    return clamp_probability(max(market - max_dev, min(market + max_dev, fv)))
