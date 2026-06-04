"""Inventory repair sizing and repair price policy."""

from __future__ import annotations

from typing import Optional

from src.core.models.inventory import RepairPlan

MIN_LIVE_PAIR_EDGE = 0.02


def compute_inventory_repair_sizes(imbalance: float,
                                   min_order_size: int,
                                   max_order_size: int) -> tuple[int, int, str]:
    min_order_size = max(1, int(min_order_size or 1))
    max_order_size = max(min_order_size, int(max_order_size or min_order_size))
    tail = abs(float(imbalance or 0))

    if tail <= 0:
        return 0, 0, "flat"
    if tail < min_order_size:
        if imbalance > 0:
            return 0, min_order_size, "repair_down"
        return min_order_size, 0, "repair_up"

    repair_size = min(max_order_size, int(round(tail)))
    if imbalance > 0:
        return 0, repair_size, "repair_down"
    return repair_size, 0, "repair_up"


def plan_inventory_repair(imbalance: float,
                          min_order_size: int,
                          max_order_size: int,
                          *,
                          fair_value: float | None = None,
                          fv_aware: bool = False) -> RepairPlan:
    """Return an explicit repair plan for callers that need metadata.

    This wraps the legacy tuple helpers without changing their sizing semantics.
    """
    if fv_aware and fair_value is not None:
        yes_size, no_size, mode = compute_fv_aware_dust_repair_sizes(
            imbalance, fair_value, min_order_size, max_order_size
        )
    else:
        yes_size, no_size, mode = compute_inventory_repair_sizes(
            imbalance, min_order_size, max_order_size
        )
    if mode == "flat":
        reason = "FLAT"
    elif mode.startswith("dust_hold"):
        reason = "DUST_HOLD"
    elif abs(float(imbalance or 0.0)) < max(1, int(min_order_size or 1)):
        reason = "SUB_MINIMUM_TAIL"
    else:
        reason = "IMBALANCE_REPAIR"
    return RepairPlan(
        yes_size=yes_size,
        no_size=no_size,
        mode=mode,
        reason=reason,
        metadata={
            "imbalance": float(imbalance or 0.0),
            "min_order_size": max(1, int(min_order_size or 1)),
            "max_order_size": max(max(1, int(min_order_size or 1)), int(max_order_size or min_order_size or 1)),
            "fair_value": fair_value,
            "fv_aware": fv_aware,
        },
    )


def compute_fv_aware_dust_repair_sizes(imbalance: float,
                                       fair_value: float,
                                       min_order_size: int,
                                       max_order_size: int,
                                       neutral_band: float = 0.02) -> tuple[int, int, str]:
    min_order_size = max(1, int(min_order_size or 1))
    max_order_size = max(min_order_size, int(max_order_size or min_order_size))
    tail = abs(float(imbalance or 0))
    if tail <= 0:
        return 0, 0, "flat"
    if tail >= min_order_size:
        return compute_inventory_repair_sizes(imbalance, min_order_size, max_order_size)

    fv = max(0.0, min(1.0, float(fair_value or 0.5)))
    ladder_threshold = max(3, int((min_order_size + 1) // 2))
    if tail >= ladder_threshold:
        ladder_size = min(max_order_size, int(round(tail)) + min_order_size)
        if imbalance > 0:
            return 0, ladder_size, "repair_down"
        return ladder_size, 0, "repair_up"

    if imbalance > 0:
        if fv >= 0.5 - neutral_band:
            return 0, 0, "dust_hold_up"
        return 0, min_order_size, "repair_down"

    if fv <= 0.5 + neutral_band:
        return 0, 0, "dust_hold_down"
    return min_order_size, 0, "repair_up"


def apply_dust_price_guardrails(quotes, mode: str,
                                best_ask_yes: Optional[float] = None,
                                best_ask_no: Optional[float] = None):
    if mode not in ("dust_up", "dust_down"):
        return quotes

    yes = float(quotes.yes_buy_price or 0)
    no = float(quotes.no_buy_price or 0)
    if yes <= 0 or no <= 0:
        return quotes

    if mode == "dust_up":
        yes -= 0.01
        no += 0.01
    else:
        yes += 0.01
        no -= 0.01

    if best_ask_yes is not None and yes >= best_ask_yes:
        yes = best_ask_yes - 0.01
    if best_ask_no is not None and no >= best_ask_no:
        no = best_ask_no - 0.01

    yes = max(0.01, min(0.99, round(yes, 2)))
    no = max(0.01, min(0.99, round(no, 2)))

    if yes + no >= 1.0:
        if mode == "dust_up":
            yes = max(0.01, round(0.99 - no, 2))
        else:
            no = max(0.01, round(0.99 - yes, 2))

    quotes.yes_buy_price = yes
    quotes.no_buy_price = no
    quotes.combined_cost = round(yes + no, 4)
    quotes.edge_per_pair = round(1.0 - quotes.combined_cost, 4)
    return quotes


def repair_min_edge_for_remaining(remaining: float, repair_mode: str) -> float:
    if repair_mode not in ("repair_up", "repair_down"):
        return MIN_LIVE_PAIR_EDGE
    if remaining <= 90:
        return 0.0
    if remaining <= 240:
        return 0.005
    if remaining <= 480:
        return 0.01
    return MIN_LIVE_PAIR_EDGE


def repair_price_cap(pos, side: str, size: float, fair_value: float,
                     min_edge: float = 0.01,
                     adverse_buffer: float = 0.02) -> tuple[float, str]:
    side = (side or "").lower()
    profitable_cap = float(pos.max_profitable_repair_price(side, size, min_edge=min_edge))
    return profitable_cap, "pair_edge"


def aggressive_repair_price(current_price: float | None,
                            cap: float,
                            best_ask: Optional[float] = None,
                            best_bid: Optional[float] = None) -> float | None:
    if cap < 0.01:
        return None

    price = float(current_price or 0.01)
    target = float(cap)

    if best_ask is not None and float(best_ask or 0) > 0:
        target = min(target, float(best_ask) - 0.01)
    if best_bid is not None and float(best_bid or 0) > 0:
        target = max(target, min(float(best_bid), float(cap)))

    target = max(0.01, min(0.99, target))
    if price > float(cap):
        return round(target, 2)
    if target <= price:
        return round(price, 2)
    return round(target, 2)
