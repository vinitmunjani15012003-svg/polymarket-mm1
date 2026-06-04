"""Directional and pair-cost quote policy helpers."""

from __future__ import annotations

from typing import Literal

DirectionalGuardAction = Literal["block_cheap_side", "halve_cheap_side"]


def apply_directional_market_guard(quotes, fair_value: float, repair_mode: str) -> DirectionalGuardAction | None:
    """Reduce adverse-selection exposure in directional normal markets.

    Mutates the quote-like object in the same way the historical MarketCycler
    inline logic did. Returns the action applied so orchestration can log with
    market/asset context.
    """
    if repair_mode != "normal":
        return None

    fv = float(fair_value or 0.5)
    if fv >= 0.80 or fv <= 0.20:
        if fv >= 0.80:
            quotes.no_buy_size = 0
        else:
            quotes.yes_buy_size = 0
        return "block_cheap_side"

    if fv >= 0.65 or fv <= 0.35:
        if fv >= 0.65:
            quotes.no_buy_size = max(0, int(quotes.no_buy_size * 0.5))
        else:
            quotes.yes_buy_size = max(0, int(quotes.yes_buy_size * 0.5))
        return "halve_cheap_side"

    return None


def apply_pair_cost_precheck(quotes, fair_value: float, repair_mode: str, max_combined_cost: float) -> bool:
    """Block the likely-to-fill cheap side when a normal pair would exceed cost.

    Returns True when a side was blocked. Mutates the quote-like object to
    preserve the existing quote-generation behavior.
    """
    if not (
        repair_mode == "normal"
        and getattr(quotes, "yes_buy_size", 0) > 0
        and getattr(quotes, "no_buy_size", 0) > 0
    ):
        return False

    proposed_combined = float(getattr(quotes, "yes_buy_price", 0) or 0) + float(getattr(quotes, "no_buy_price", 0) or 0)
    if proposed_combined <= max_combined_cost:
        return False

    if float(fair_value or 0.5) >= 0.50:
        quotes.no_buy_size = 0
    else:
        quotes.yes_buy_size = 0
    return True
