"""Fair-value service package."""

from .basis_protection import basis_check, basis_guard_triggered, start_price_disagrees_with_market
from .blender import (
    blended_fair_value,
    cap_fair_value_to_market,
    clamp_probability,
    polymarket_implied_up_mid,
    spot_from_binary_probability,
    weighted_fair_value,
)
from .calibration import (
    BASIS_GUARD_MAX_FV_DEVIATION,
    FAST_ADVERSE_CANCEL_MIN_EDGE,
    FAST_FEED_CONFIDENCE_FLOOR,
    FAST_FEED_CONFIDENCE_MOVE_THRESHOLD,
    FAST_FEED_CONFIDENCE_STRONG_FLOOR,
    FAST_FEED_CONFIDENCE_STRONG_MOVE_THRESHOLD,
    FV_DISAGREEMENT_CONFIDENCE_CAP,
    FV_HARD_DISAGREEMENT,
    FV_MAX_MODEL_CONFIDENCE,
    FV_MIN_MODEL_CONFIDENCE,
    FV_TAIL_BLEND_GUARD,
    FV_TAIL_MAX_MARKET_PULL,
    MAX_EXNESS_PRICE_AGE_SECONDS,
    MAX_SPOT_PRICE_AGE_SECONDS,
    MAX_TRADING_FV_MARKET_DEVIATION,
)
from .confidence import (
    apply_fast_feed_confidence_floor,
    compute_confidence_score,
    confidence_decay,
    fv_model_confidence,
)
from .engine import FairValueEngine, FairValueInputs
from .model import FairValueResult, UpDownFairValue

__all__ = [
    "BASIS_GUARD_MAX_FV_DEVIATION",
    "FAST_ADVERSE_CANCEL_MIN_EDGE",
    "FAST_FEED_CONFIDENCE_FLOOR",
    "FAST_FEED_CONFIDENCE_MOVE_THRESHOLD",
    "FAST_FEED_CONFIDENCE_STRONG_FLOOR",
    "FAST_FEED_CONFIDENCE_STRONG_MOVE_THRESHOLD",
    "FV_DISAGREEMENT_CONFIDENCE_CAP",
    "FV_HARD_DISAGREEMENT",
    "FV_MAX_MODEL_CONFIDENCE",
    "FV_MIN_MODEL_CONFIDENCE",
    "FV_TAIL_BLEND_GUARD",
    "FV_TAIL_MAX_MARKET_PULL",
    "MAX_EXNESS_PRICE_AGE_SECONDS",
    "MAX_SPOT_PRICE_AGE_SECONDS",
    "MAX_TRADING_FV_MARKET_DEVIATION",
    "basis_check",
    "basis_guard_triggered",
    "start_price_disagrees_with_market",
    "blended_fair_value",
    "cap_fair_value_to_market",
    "clamp_probability",
    "polymarket_implied_up_mid",
    "spot_from_binary_probability",
    "weighted_fair_value",
    "apply_fast_feed_confidence_floor",
    "compute_confidence_score",
    "confidence_decay",
    "fv_model_confidence",
    "FairValueEngine",
    "FairValueInputs",
    "FairValueResult",
    "UpDownFairValue",
]
