"""InventoryBook facade.

This is the future single source of truth boundary. For this refactor branch it
wraps the existing InventoryManager so behavior stays unchanged while callers
can move to an explicit book interface.
"""

from __future__ import annotations

from src.core.models.inventory import InventorySnapshot


class InventoryBook:
    def __init__(self, inventory_manager):
        self.inventory_manager = inventory_manager

    def get_snapshot(self, market_id: str, asset: str = "") -> InventorySnapshot:
        pos = self.inventory_manager.get_or_create(market_id, asset)
        return InventorySnapshot(
            market_id=market_id,
            asset=asset,
            yes_shares=float(getattr(pos, "yes_shares", 0.0) or 0.0),
            no_shares=float(getattr(pos, "no_shares", 0.0) or 0.0),
            yes_avg=float(getattr(pos, "yes_avg_price", 0.0) or 0.0),
            no_avg=float(getattr(pos, "no_avg_price", 0.0) or 0.0),
            source="local",
        )

    def record_fill(self, market_id: str, side: str, size: float, price: float, asset: str = ""):
        return self.inventory_manager.record_fill(market_id, side, size, price, asset)
