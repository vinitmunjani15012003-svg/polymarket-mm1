"""Execution order state value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ActiveQuotes:
    """Currently active quotes for a market."""
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    yes_size: int = 0
    no_size: int = 0
    last_update: float = 0.0


def order_token_id(info) -> str:
    """Return an order token id from live dicts or dry-run order objects."""
    if info is None:
        return ""
    if isinstance(info, dict):
        return str(info.get("token_id") or info.get("asset_id") or "")
    return str(getattr(info, "token_id", "") or getattr(info, "asset_id", "") or "")
