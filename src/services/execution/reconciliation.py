"""Execution reconciliation helpers."""

from __future__ import annotations

from src.execution.order_state import order_token_id


def order_is_unknown(order_id: str, open_orders: dict) -> bool:
    return bool(order_id) and order_id not in open_orders


def tracked_order_ids(active_quotes) -> set[str]:
    return {
        oid
        for oid in (
            getattr(active_quotes, "yes_order_id", None),
            getattr(active_quotes, "no_order_id", None),
        )
        if oid
    }


def find_stray_order_ids(active_quotes, open_orders: dict, token_ids: set[str]) -> list[str]:
    """Return locally-known open orders on market tokens not tracked as active."""
    tracked = tracked_order_ids(active_quotes)
    normalized_tokens = {str(token_id) for token_id in token_ids}
    stray_ids: list[str] = []
    for order_id, info in list(open_orders.items()):
        if order_id in tracked:
            continue
        if order_token_id(info) in normalized_tokens:
            stray_ids.append(order_id)
    return stray_ids
