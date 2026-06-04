"""Inventory-domain value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class InventorySnapshot:
    market_id: str
    asset: str = ""
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_avg: float = 0.0
    no_avg: float = 0.0
    source: str = "local"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def share_imbalance(self) -> float:
        return float(self.yes_shares or 0.0) - float(self.no_shares or 0.0)

    @property
    def matched_pairs(self) -> float:
        return min(float(self.yes_shares or 0.0), float(self.no_shares or 0.0))


@dataclass(slots=True)
class RepairPlan:
    yes_size: int = 0
    no_size: int = 0
    mode: str = "flat"
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
