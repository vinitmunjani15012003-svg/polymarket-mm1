"""InventoryBook facade.

This is the future single source of truth boundary. For this refactor branch it
wraps the existing InventoryManager so behavior stays unchanged while callers
can move to an explicit book interface.
"""

from __future__ import annotations

from src.core.models.inventory import InventorySnapshot
from .reconciliation import inventory_diverged
from .repair_planner import plan_inventory_repair, repair_price_cap


class InventoryBook:
    def __init__(self, inventory_manager):
        self.inventory_manager = inventory_manager

    def get_snapshot(self, market_id: str, asset: str = "") -> InventorySnapshot:
        pos = self.get_position(market_id, asset)
        return InventorySnapshot(
            market_id=market_id,
            asset=asset,
            yes_shares=float(getattr(pos, "yes_shares", 0.0) or 0.0),
            no_shares=float(getattr(pos, "no_shares", 0.0) or 0.0),
            yes_avg=float(getattr(pos, "yes_avg_price", 0.0) or 0.0),
            no_avg=float(getattr(pos, "no_avg_price", 0.0) or 0.0),
            source="local",
        )

    def get_position(self, market_id: str, asset: str = ""):
        return self.inventory_manager.get_or_create(market_id, asset)

    def record_fill(self, market_id: str, side: str, size: float, price: float, asset: str = ""):
        return self.inventory_manager.record_fill(market_id, side, size, price, asset)

    def plan_repair(self, market_id: str, asset: str = "", *,
                    min_order_size: int, max_order_size: int,
                    fair_value: float | None = None, fv_aware: bool = False):
        snapshot = self.get_snapshot(market_id, asset)
        return plan_inventory_repair(
            snapshot.share_imbalance,
            min_order_size,
            max_order_size,
            fair_value=fair_value,
            fv_aware=fv_aware,
        )

    def repair_price_cap(self, market_id: str, side: str, size: float,
                         fair_value: float, asset: str = "",
                         min_edge: float = 0.01) -> tuple[float, str]:
        return repair_price_cap(
            self.get_position(market_id, asset),
            side,
            size,
            fair_value,
            min_edge=min_edge,
        )

    def reconciliation_needed(self, market_id: str, wallet_snapshot: InventorySnapshot,
                              asset: str = "", tolerance: float = 0.5) -> bool:
        local = self.get_snapshot(market_id, asset)
        return inventory_diverged(local.share_imbalance, wallet_snapshot.share_imbalance, tolerance=tolerance)
