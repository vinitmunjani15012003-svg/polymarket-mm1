"""Execution reconciliation helpers."""

from __future__ import annotations


def order_is_unknown(order_id: str, open_orders: dict) -> bool:
    return bool(order_id) and order_id not in open_orders


def tracked_order_ids(active_quotes) -> set[str]:
    return {oid for oid in (getattr(active_quotes, "yes_order_id", None), getattr(active_quotes, "no_order_id", None)) if oid}
