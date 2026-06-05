"""Fill idempotency-key helpers for CLOB trade processing."""

from __future__ import annotations

import hashlib
import json


def fill_dedupe_key(fill: dict, market_id: str = "") -> str:
    """Build a robust idempotency key for CLOB fills/trades.

    Prefer provider IDs when available. If the SDK omits IDs, include enough
    stable fields to distinguish partial fills on the same order.
    """
    for key in ("id", "trade_id", "transaction_hash", "tx_hash", "hash"):
        value = fill.get(key)
        if value:
            return f"{key}:{value}"

    material = {
        "market": market_id,
        "order_id": fill.get("order_id") or fill.get("orderID") or fill.get("maker_order_id") or "",
        "asset_id": fill.get("asset_id") or fill.get("token_id") or fill.get("assetId") or "",
        "price": str(fill.get("price", "")),
        "size": str(fill.get("size", "")),
        "side": str(fill.get("side", "")),
        "timestamp": str(
            fill.get("timestamp")
            or fill.get("created_at")
            or fill.get("match_time")
            or fill.get("time")
            or ""
        ),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "synthetic:" + hashlib.sha256(encoded.encode()).hexdigest()
