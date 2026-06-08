"""Pure helpers for CLOB open-order context and SDK order shapes."""

from __future__ import annotations

import time
from typing import Any

CLOSED_ORDER_STATUSES = {"cancelled", "canceled", "filled", "matched", "closed"}


def cache_open_order_context(
    open_orders: dict[str, dict],
    recent_order_context: dict[str, dict],
    order_id: str,
    *,
    now: float | None = None,
    max_entries: int = 500,
    ttl_seconds: float = 900,
) -> dict[str, dict]:
    """Copy an open order into the recent-context cache and prune stale entries."""
    ctx = open_orders.get(order_id)
    if not ctx:
        return recent_order_context
    ts = time.time() if now is None else now
    recent_order_context[order_id] = {**ctx, "closed_at": ts}
    cutoff = ts - ttl_seconds
    if len(recent_order_context) > max_entries:
        for oid, info in list(recent_order_context.items()):
            if float(info.get("closed_at", 0) or 0) < cutoff:
                recent_order_context.pop(oid, None)
    return recent_order_context


def get_order_context(
    open_orders: dict[str, dict],
    recent_order_context: dict[str, dict],
    order_id: str,
) -> dict:
    """Return current or recently closed order context for delayed fill events."""
    return open_orders.get(order_id) or recent_order_context.get(order_id, {})


def normalize_orders_response(response: Any) -> list[dict]:
    """Normalize CLOB get_orders/get_trades list-or-dict response shapes."""
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        data = response.get("data", [])
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    return []


def order_id_from_record(order: dict) -> str:
    """Extract an order id across SDK field variants."""
    return str(order.get("id") or order.get("orderID") or order.get("order_id") or "")


def order_remaining_size(order: dict) -> float:
    """Compute remaining size from common CLOB open-order fields."""
    original = float(order.get("original_size") or order.get("size") or 0)
    matched = float(order.get("size_matched") or order.get("matched_size") or 0)
    return max(0.0, original - matched)


def order_is_closed(order: dict) -> bool:
    """Return True when a CLOB order record indicates the order is no longer open."""
    status = str(order.get("status") or order.get("state") or "").lower()
    if status in CLOSED_ORDER_STATUSES:
        return True
    original = float(order.get("original_size") or order.get("size") or 0)
    matched = float(order.get("size_matched") or order.get("matched_size") or 0)
    return original > 0 and matched >= original


def token_side_from_outcome(outcome: Any) -> str | None:
    """Map Polymarket outcome labels into the bot's yes/no side names."""
    normalized = str(outcome or "").strip().lower()
    if normalized in ("yes", "up"):
        return "yes"
    if normalized in ("no", "down"):
        return "no"
    return None


def normalize_open_order_record(order: dict, *, now: float | None = None) -> tuple[str, dict] | None:
    """Convert a CLOB open-order record into ClobClientWrapper.open_orders shape."""
    order_id = order_id_from_record(order)
    if not order_id:
        return None
    execution_side = str(order.get("side", "BUY") or "BUY").upper()
    return order_id, {
        "token_id": str(order.get("asset_id") or order.get("token_id") or ""),
        "price": float(order.get("price") or 0),
        "size": order_remaining_size(order),
        "side": execution_side,
        "execution_side": execution_side,
        "close_only": execution_side == "SELL",
        "token_side": token_side_from_outcome(order.get("outcome")),
        "placed_at": float(order.get("created_at") or (time.time() if now is None else now)),
    }


def normalize_open_orders(records: list[dict], *, now: float | None = None) -> dict[str, dict]:
    """Build local open_orders mapping from CLOB open-order records."""
    normalized: dict[str, dict] = {}
    for order in records:
        item = normalize_open_order_record(order, now=now)
        if item is not None:
            order_id, ctx = item
            normalized[order_id] = ctx
    return normalized
