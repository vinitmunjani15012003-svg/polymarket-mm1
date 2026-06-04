"""Pure helpers for CLOB fill/trade normalization."""

from __future__ import annotations


def maker_order_id(maker_order: dict) -> str:
    """Return a maker-order id across CLOB SDK field-name variants."""
    return (
        maker_order.get("order_id")
        or maker_order.get("orderID")
        or maker_order.get("orderId")
        or maker_order.get("id")
        or ""
    )


def maker_orders_for_fill(fill: dict) -> list:
    """Return maker order legs from either v1/v2 response spelling."""
    maker_orders = fill.get("maker_orders") or fill.get("makerOrders") or []
    return maker_orders if isinstance(maker_orders, list) else []
