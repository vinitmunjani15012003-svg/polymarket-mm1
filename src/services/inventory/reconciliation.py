"""Inventory reconciliation helpers."""

from __future__ import annotations

from src.core.models.inventory import InventorySnapshot


def snapshot_from_wallet(market_id: str, wallet_snapshot: dict, asset: str = "") -> InventorySnapshot:
    yes = float(wallet_snapshot.get("yes_shares", 0) or 0)
    no = float(wallet_snapshot.get("no_shares", 0) or 0)
    return InventorySnapshot(
        market_id=market_id,
        asset=asset,
        yes_shares=yes,
        no_shares=no,
        source=str(wallet_snapshot.get("source") or "wallet"),
        metadata={"matched_pairs": min(yes, no), "share_imbalance": yes - no},
    )


def inventory_diverged(local_imbalance: float, wallet_imbalance: float, tolerance: float = 0.5) -> bool:
    return abs(float(wallet_imbalance or 0) - float(local_imbalance or 0)) >= tolerance
