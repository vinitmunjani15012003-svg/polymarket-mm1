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


def reconciliation_delta(local: InventorySnapshot, wallet: InventorySnapshot,
                         tolerance: float = 0.5) -> dict:
    """Summarize local-vs-wallet divergence without mutating either source."""
    yes_delta = float(wallet.yes_shares or 0.0) - float(local.yes_shares or 0.0)
    no_delta = float(wallet.no_shares or 0.0) - float(local.no_shares or 0.0)
    imbalance_delta = float(wallet.share_imbalance) - float(local.share_imbalance)
    return {
        "diverged": abs(imbalance_delta) >= float(tolerance),
        "yes_delta": yes_delta,
        "no_delta": no_delta,
        "imbalance_delta": imbalance_delta,
        "local_source": local.source,
        "wallet_source": wallet.source,
        "tolerance": tolerance,
    }
