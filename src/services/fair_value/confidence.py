"""Fair-value confidence scoring."""

from __future__ import annotations

from .blender import clamp_probability
from .calibration import (
    FAST_FEED_CONFIDENCE_FLOOR,
    FAST_FEED_CONFIDENCE_MOVE_THRESHOLD,
    FAST_FEED_CONFIDENCE_STRONG_FLOOR,
    FAST_FEED_CONFIDENCE_STRONG_MOVE_THRESHOLD,
    FV_DISAGREEMENT_CONFIDENCE_CAP,
    FV_HARD_DISAGREEMENT,
    FV_MAX_MODEL_CONFIDENCE,
    FV_MIN_MODEL_CONFIDENCE,
)


def fv_model_confidence(model_fv: float,
                        elapsed_fraction: float,
                        standardized_move: float,
                        market_fv: float | None = None,
                        min_confidence: float = FV_MIN_MODEL_CONFIDENCE,
                        max_confidence: float = FV_MAX_MODEL_CONFIDENCE) -> float:
    """Confidence weight for raw model FV in a 15m binary window."""
    elapsed = max(0.0, min(1.0, float(elapsed_fraction or 0.0)))
    move = max(0.0, float(standardized_move or 0.0))
    time_component = 0.60 * (elapsed ** 0.75)
    move_component = 0.25 * min(1.0, move / 1.5)
    confidence = min_confidence + time_component + move_component
    confidence = max(min_confidence, min(max_confidence, confidence))

    if market_fv is not None and abs(clamp_probability(model_fv) - clamp_probability(market_fv)) >= FV_HARD_DISAGREEMENT:
        confidence = min(confidence, FV_DISAGREEMENT_CONFIDENCE_CAP)
    return max(0.0, min(1.0, confidence))


def compute_confidence_score(*args, **kwargs) -> float:
    """Compatibility alias for the roadmap naming."""
    return fv_model_confidence(*args, **kwargs)


def confidence_decay(confidence: float, age_seconds: float, half_life_seconds: float = 2.0) -> float:
    """Decay confidence as market data ages."""
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    age = max(0.0, float(age_seconds or 0.0))
    half_life = max(0.001, float(half_life_seconds or 0.001))
    return conf * (0.5 ** (age / half_life))


def apply_fast_feed_confidence_floor(confidence: float,
                                     price_source: str,
                                     standardized_move: float) -> float:
    """Trust Exness/MT5 more during real moves so FV does not lag the book."""
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    if price_source != "exness_mt5":
        return conf
    move = max(0.0, float(standardized_move or 0.0))
    if move >= FAST_FEED_CONFIDENCE_STRONG_MOVE_THRESHOLD:
        return max(conf, FAST_FEED_CONFIDENCE_STRONG_FLOOR)
    if move >= FAST_FEED_CONFIDENCE_MOVE_THRESHOLD:
        return max(conf, FAST_FEED_CONFIDENCE_FLOOR)
    return conf
