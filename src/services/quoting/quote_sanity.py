"""Quote sanity and boundary validation."""

from __future__ import annotations

from src.core.models.decision import DecisionResult


def validate_tick_price(price: float | None, min_price: float = 0.01, max_price: float = 0.99) -> bool:
    if price is None:
        return True
    try:
        p = float(price)
    except Exception:
        return False
    return min_price <= p <= max_price


def would_cross_bid(price: float | None, best_ask: float | None) -> bool:
    return price is not None and best_ask is not None and float(price) >= float(best_ask)


def validate_quote_pair(yes_price: float | None, no_price: float | None,
                        max_combined_cost: float = 0.99) -> DecisionResult:
    if not validate_tick_price(yes_price) or not validate_tick_price(no_price):
        return DecisionResult.block("HOLD", "INVALID_TICK_PRICE")
    if yes_price and no_price and float(yes_price) + float(no_price) > max_combined_cost:
        return DecisionResult.block(
            "HOLD",
            "PAIR_COST_TOO_HIGH",
            combined_cost=float(yes_price) + float(no_price),
            max_combined_cost=max_combined_cost,
        )
    return DecisionResult.allow("QUOTE", "OK")


def validate_quote_pair_for_active_sides(
    *,
    yes_price: float | None,
    yes_size: int | float,
    no_price: float | None,
    no_size: int | float,
    max_combined_cost: float = 0.99,
) -> DecisionResult:
    """Validate final executable quote prices for active sides only.

    Quote engines commonly keep stale/placeholder prices on a side whose size
    has been zeroed. Final validation therefore mirrors order construction: an
    inactive side is ignored, while two active sides must satisfy the pair-cost
    bound.
    """
    yes_active = float(yes_size or 0) > 0
    no_active = float(no_size or 0) > 0
    if yes_active and not validate_tick_price(yes_price):
        return DecisionResult.block("HOLD", "INVALID_YES_TICK_PRICE", side="yes", price=yes_price)
    if no_active and not validate_tick_price(no_price):
        return DecisionResult.block("HOLD", "INVALID_NO_TICK_PRICE", side="no", price=no_price)
    if yes_active and no_active and float(yes_price or 0) + float(no_price or 0) > max_combined_cost:
        combined = float(yes_price or 0) + float(no_price or 0)
        return DecisionResult.block(
            "HOLD",
            "PAIR_COST_TOO_HIGH",
            combined_cost=combined,
            max_combined_cost=max_combined_cost,
        )
    return DecisionResult.allow("QUOTE", "OK")
