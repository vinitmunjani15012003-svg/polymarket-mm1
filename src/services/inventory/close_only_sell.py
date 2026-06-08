"""Close-only sell planner for unmatched inventory exits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CloseOnlySellPlan:
    active: bool = False
    side: str = ""
    price: float | None = None
    size: int = 0
    reason: str = "DISABLED"
    metadata: dict[str, Any] = field(default_factory=dict)


def _cfg(config: Any, name: str, default: Any) -> Any:
    if config is None:
        return default
    return getattr(config, name, default)


def _side_token_value(side: str, fair_value: float) -> float:
    return float(fair_value if side == "yes" else 1.0 - fair_value)


def _round_price(price: float) -> float:
    return max(0.01, min(0.99, round(float(price or 0.0) + 1e-9, 2)))


def plan_close_only_sell(
    pos,
    *,
    fair_value: float,
    wallet_snapshot: dict | None,
    yes_book,
    no_book,
    min_order_size: int,
    max_order_size: int,
    config=None,
    close_only_context: bool = False,
    small_cap_emergency: bool = False,
    balanced_repair_active: bool = False,
    has_resting_opening_quote: bool = False,
    remaining_seconds: float = 900.0,
) -> CloseOnlySellPlan:
    """Plan a post-only SELL of owned unmatched heavy-side shares.

    Phase 1 only sells in explicit close-only/risk/emergency contexts.  It uses
    wallet truth as a hard cap and local FIFO unmatched lots as the cost-basis
    cap; if either is missing, it blocks.
    """
    enabled = bool(_cfg(config, "enabled", False))
    min_order_size = max(1, int(min_order_size or 1))
    max_order_size = int(_cfg(config, "max_order_size", 0) or 0) or int(max_order_size or min_order_size)
    max_order_size = max(min_order_size, max_order_size)
    min_edge = max(0.0, float(_cfg(config, "min_edge", 0.01) or 0.0))
    max_loss_per_share = max(0.0, float(_cfg(config, "max_loss_per_share", 0.20) or 0.0))
    min_seconds_remaining = max(0.0, float(_cfg(config, "min_seconds_remaining", 30.0) or 0.0))
    metadata = {
        "enabled": enabled,
        "close_only_context": close_only_context,
        "small_cap_emergency": small_cap_emergency,
        "balanced_repair_active": balanced_repair_active,
        "has_wallet_truth": bool(wallet_snapshot),
        "min_order_size": min_order_size,
        "max_order_size": max_order_size,
        "min_edge": min_edge,
        "max_loss_per_share": max_loss_per_share,
    }

    if not enabled:
        return CloseOnlySellPlan(reason="DISABLED", metadata=metadata)
    if balanced_repair_active:
        return CloseOnlySellPlan(reason="BALANCED_REPAIR_ACTIVE", metadata=metadata)
    if has_resting_opening_quote:
        return CloseOnlySellPlan(reason="OPENING_QUOTE_RESTING", metadata=metadata)
    if not (close_only_context or small_cap_emergency):
        return CloseOnlySellPlan(reason="NOT_CLOSE_ONLY_CONTEXT", metadata=metadata)
    if float(remaining_seconds or 0.0) < min_seconds_remaining and not small_cap_emergency:
        return CloseOnlySellPlan(reason="TOO_LATE_FOR_PASSIVE_SELL", metadata=metadata)
    if not wallet_snapshot:
        return CloseOnlySellPlan(reason="NO_WALLET_TRUTH", metadata=metadata)

    wallet_yes = float(wallet_snapshot.get("yes_shares", 0) or 0)
    wallet_no = float(wallet_snapshot.get("no_shares", 0) or 0)
    local_imbalance = float(pos.share_imbalance() or 0.0)
    wallet_imbalance = wallet_yes - wallet_no
    heavy_imbalance = wallet_imbalance if abs(wallet_imbalance) >= 0.5 else local_imbalance
    metadata.update({
        "local_imbalance": round(local_imbalance, 6),
        "wallet_imbalance": round(wallet_imbalance, 6),
    })

    if abs(heavy_imbalance) < min_order_size:
        return CloseOnlySellPlan(reason="IMBALANCE_BELOW_MIN", metadata=metadata)

    side = "yes" if heavy_imbalance > 0 else "no"
    book = yes_book if side == "yes" else no_book
    best_bid = float(getattr(book, "best_bid", 0.0) or 0.0)
    best_ask = float(getattr(book, "best_ask", 0.0) or 0.0)
    token_value = _side_token_value(side, float(fair_value or 0.5))
    metadata.update({
        "side": side,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "token_value": round(token_value, 6),
    })

    if best_bid <= 0:
        return CloseOnlySellPlan(reason="NO_BID", metadata=metadata)
    if best_bid < token_value + min_edge and not small_cap_emergency:
        return CloseOnlySellPlan(reason="SELL_EDGE_TOO_SMALL", metadata=metadata)

    wallet_unmatched = max(0.0, wallet_yes - wallet_no) if side == "yes" else max(0.0, wallet_no - wallet_yes)
    local_unmatched = float(pos.unmatched_shares(side) or 0.0) if hasattr(pos, "unmatched_shares") else 0.0
    sellable = min(abs(heavy_imbalance), wallet_unmatched, local_unmatched, float(max_order_size))
    metadata.update({
        "wallet_unmatched": round(wallet_unmatched, 6),
        "local_unmatched": round(local_unmatched, 6),
        "sellable": round(sellable, 6),
    })
    if sellable < min_order_size:
        return CloseOnlySellPlan(reason="SELLABLE_BELOW_MIN", metadata=metadata)

    cost_info = pos.unmatched_cost_basis(side, sellable) if hasattr(pos, "unmatched_cost_basis") else {}
    avg_entry = float(cost_info.get("avg_entry", 0.0) or 0.0) if isinstance(cost_info, dict) else 0.0
    cost_size = float(cost_info.get("size", 0.0) or 0.0) if isinstance(cost_info, dict) else 0.0
    max_loss_price = max(0.01, avg_entry - max_loss_per_share)
    metadata.update({
        "avg_entry": round(avg_entry, 6),
        "cost_basis_size": round(cost_size, 6),
        "max_loss_price": round(max_loss_price, 6),
    })
    if cost_size + 1e-9 < sellable:
        return CloseOnlySellPlan(reason="INSUFFICIENT_COST_BASIS", metadata=metadata)

    # Maker/post-only sell: price must be above best bid. Use the most
    # fillable passive price that still respects FV edge (outside emergency)
    # and the configured FIFO loss cap.
    edge_price = token_value + min_edge if not small_cap_emergency else 0.01
    price = _round_price(max(best_bid + 0.01, edge_price, max_loss_price if avg_entry > 0 else 0.01))
    if price <= best_bid:
        price = _round_price(best_bid + 0.01)
    if price <= best_bid:
        return CloseOnlySellPlan(reason="NO_PASSIVE_PRICE", metadata=metadata)
    if price <= 0 or price >= 1:
        return CloseOnlySellPlan(reason="INVALID_PRICE", metadata=metadata)
    if avg_entry > 0 and price + 1e-9 < max_loss_price:
        return CloseOnlySellPlan(reason="LOSS_CAP_EXCEEDED", metadata=metadata)

    return CloseOnlySellPlan(
        active=True,
        side=side,
        price=price,
        size=int(sellable),
        reason="CLOSE_ONLY_SELL",
        metadata=metadata,
    )
