"""Compatibility helpers for py-clob-client SDK variants.

These helpers are intentionally pure/small so ``ClobClientWrapper`` can stay as
SDK lifecycle owner while behavior remains easy to test across SDK response
shapes.
"""

from __future__ import annotations

from typing import Any


def ensure_builder_code(order_args: Any) -> Any:
    """Ensure OrderArgs has the builder_code attribute expected by some SDKs."""
    if not hasattr(order_args, "builder_code"):
        try:
            setattr(order_args, "builder_code", "")
        except Exception:
            try:
                object.__setattr__(order_args, "builder_code", "")
            except Exception:
                pass
    return order_args


def normalize_post_orders_response(response: Any, expected_count: int) -> list[dict]:
    """Normalize py-clob-client post_orders responses across SDK versions."""
    if isinstance(response, list):
        return [item if isinstance(item, dict) else {} for item in response[:expected_count]]
    if isinstance(response, dict):
        raw = (
            response.get("orders")
            or response.get("data")
            or response.get("results")
            or response.get("responses")
        )
        if isinstance(raw, list):
            return [item if isinstance(item, dict) else {} for item in raw[:expected_count]]
        # Some SDKs return a single-order response dict when len==1.
        if expected_count == 1 and (response.get("orderID") or response.get("id")):
            return [response]
        if response.get("error") or response.get("status") in ("error", "failed", "rejected"):
            return [response for _ in range(expected_count)]
    return [{} for _ in range(expected_count)]
