"""FairValueEngine: one service-owned path from raw inputs to tradable FV."""

from __future__ import annotations

from dataclasses import dataclass

from .basis_protection import basis_check
from .blender import blended_fair_value, cap_fair_value_to_market, clamp_probability
from .confidence import apply_fast_feed_confidence_floor, fv_model_confidence
from .model import FairValueResult, UpDownFairValue


@dataclass(slots=True)
class FairValueInputs:
    spot: float
    sigma: float
    now_ts: float
    elapsed_fraction: float
    standardized_move: float
    market_fv: float | None = None
    price_source: str = "unknown"


class FairValueEngine:
    """Compute raw, blended, capped, and explainable fair value.

    The engine centralizes what used to be scattered blending/guard logic in
    market_cycler. It deliberately accepts an injected raw model so lifecycle
    code can keep owning market start/resolve times.
    """

    def __init__(self, model: UpDownFairValue):
        self.model = model

    def compute(self, inputs: FairValueInputs, update_state: bool = False) -> FairValueResult:
        raw = self.model.fair_value(inputs.spot, inputs.sigma, inputs.now_ts, update_state=update_state)
        confidence = fv_model_confidence(
            raw,
            inputs.elapsed_fraction,
            inputs.standardized_move,
            inputs.market_fv,
        )
        confidence = apply_fast_feed_confidence_floor(
            confidence,
            inputs.price_source,
            inputs.standardized_move,
        )
        blended = blended_fair_value(raw, inputs.market_fv, confidence)
        tradable = cap_fair_value_to_market(blended, inputs.market_fv)
        basis = basis_check(raw, inputs.market_fv)
        return FairValueResult(
            raw_fv=clamp_probability(raw),
            market_fv=inputs.market_fv,
            blended_fv=clamp_probability(blended),
            tradable_fv=clamp_probability(tradable),
            confidence=confidence,
            basis_gap=basis.get("basis_gap"),
            source=inputs.price_source,
            reason=basis.get("reason", "OK"),
        )
