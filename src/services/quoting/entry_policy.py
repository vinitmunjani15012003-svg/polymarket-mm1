"""Entry mode policy for flat inventory."""

from __future__ import annotations

from typing import Optional

MIN_LIVE_PAIR_EDGE = 0.02
FV_FAVORED_ENTRY_THRESHOLD = 0.50
FV_FAVORED_ENTRY_MIN_EDGE = 0.0
FV_FAVORED_ENTRY_MAX_SIZE = 5
FV_FAVORED_ENTRY_STOP_SECONDS = 600


def apply_fv_favored_entry_mode(quotes, fair_value: float, share_imbalance: float,
                                min_order_size: int,
                                threshold: float = FV_FAVORED_ENTRY_THRESHOLD,
                                best_ask_yes: Optional[float] = None,
                                best_ask_no: Optional[float] = None,
                                best_bid_yes: Optional[float] = None,
                                best_bid_no: Optional[float] = None,
                                min_pair_edge: float = MIN_LIVE_PAIR_EDGE,
                                min_entry_edge: float = FV_FAVORED_ENTRY_MIN_EDGE,
                                max_entry_size: int = FV_FAVORED_ENTRY_MAX_SIZE) -> str | None:
    """Quote only the best FV-edge side while flat, if it is repairable."""
    if abs(share_imbalance) >= min_order_size:
        return None
    if quotes.yes_buy_size <= 0 and quotes.no_buy_size <= 0:
        return None

    yes_price = float(quotes.yes_buy_price or 0)
    no_price = float(quotes.no_buy_price or 0)
    if yes_price <= 0 or no_price <= 0:
        return None

    fv = max(0.0, min(1.0, float(fair_value or 0.5)))
    yes_edge = fv - yes_price
    no_edge = (1.0 - fv) - no_price

    side = None
    edge_epsilon = 1e-9
    if (fv > threshold + edge_epsilon
            and yes_edge >= min_entry_edge
            and quotes.yes_buy_size > 0):
        side = "yes"
    elif (fv < (1.0 - threshold) - edge_epsilon
            and no_edge >= min_entry_edge
            and quotes.no_buy_size > 0):
        side = "no"

    if not side:
        quotes.yes_buy_size = 0
        quotes.no_buy_size = 0
        return "blocked"

    max_repair_bid_lag = 0.02
    if side == "yes":
        repair_cap = 1.0 - yes_price - min_pair_edge
        if best_bid_no is not None:
            repair_too_far = repair_cap < float(best_bid_no) - max_repair_bid_lag
        else:
            repair_too_far = best_ask_no is not None and (float(best_ask_no) - 0.01) > repair_cap
        if repair_too_far:
            quotes.yes_buy_size = 0
            quotes.no_buy_size = 0
            return "blocked"
        quotes.yes_buy_size = min(int(quotes.yes_buy_size), max(min_order_size, int(max_entry_size)))
        quotes.no_buy_size = 0
        return "yes"

    repair_cap = 1.0 - no_price - min_pair_edge
    if best_bid_yes is not None:
        repair_too_far = repair_cap < float(best_bid_yes) - max_repair_bid_lag
    else:
        repair_too_far = best_ask_yes is not None and (float(best_ask_yes) - 0.01) > repair_cap
    if repair_too_far:
        quotes.yes_buy_size = 0
        quotes.no_buy_size = 0
        return "blocked"
    quotes.no_buy_size = min(int(quotes.no_buy_size), max(min_order_size, int(max_entry_size)))
    quotes.yes_buy_size = 0
    return "no"
