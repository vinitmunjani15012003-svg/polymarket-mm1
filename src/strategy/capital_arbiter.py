"""
Capital arbiter — coordinates capital allocation across multiple assets.

Dynamic allocation based on edge: assets with better edge (higher
matched-pair profit rate) get a larger share of capital. Assets
performing poorly get throttled to preserve capital for better markets.

Design:
  - Total capital is shared across all assets
  - Each asset has a dynamic allocation that adjusts based on performance
  - A global reserve (10%) is always maintained for safety
  - Edge is measured as matched_pair_profit / total_cost (profit per $ deployed)
"""

import time
from src.monitoring.logger import get_logger

log = get_logger("capital_arbiter")


class CapitalArbiter:
    """Coordinates capital allocation across multiple assets.
    
    Instead of fixed per-asset caps, allocation is dynamic:
    - Base allocation: equal share (e.g., 25% each for 4 assets)
    - Edge bonus: assets with higher profit-per-dollar get up to 1.5x base
    - Edge penalty: assets with negative edge get down to 0.5x base
    - Global reserve: 10% of total capital is always held back
    """

    def __init__(self, total_capital: float, asset_names: list[str] = None,
                 max_per_asset_pct: float = 0.50, reserve_pct: float = 0.10):
        """
        Args:
            total_capital: Total capital across all assets.
            asset_names: List of asset names to track.
            max_per_asset_pct: Maximum allocation per asset (hard cap).
            reserve_pct: Fraction of capital to always keep in reserve.
        """
        self.total_capital = total_capital
        self.asset_names = asset_names or []
        self.max_per_asset_pct = max_per_asset_pct
        self.reserve_pct = reserve_pct
        self._deployable = total_capital * (1.0 - reserve_pct)

        # Tracking
        self._deployed: dict[str, float] = {}  # asset -> deployed $
        self._edge_scores: dict[str, float] = {}  # asset -> profit per $ deployed
        self._last_edge_update: dict[str, float] = {}

    def register_asset(self, asset_name: str):
        """Register an asset for capital tracking."""
        if asset_name not in self.asset_names:
            self.asset_names.append(asset_name)
        self._deployed.setdefault(asset_name, 0.0)
        self._edge_scores.setdefault(asset_name, 0.0)

    def update_edge(self, asset: str, total_profit: float, total_deployed: float):
        """Update the edge score for an asset.
        
        Called periodically (e.g., after each market settles) to update
        the profit-per-dollar-deployed metric.
        
        Args:
            asset: Asset name.
            total_profit: Cumulative P&L from this asset (including rebates).
            total_deployed: Cumulative dollars deployed for this asset.
        """
        if total_deployed > 0:
            self._edge_scores[asset] = total_profit / total_deployed
        else:
            self._edge_scores[asset] = 0.0
        self._last_edge_update[asset] = time.time()

    def allocation_for(self, asset: str) -> float:
        """Get the current dollar allocation limit for an asset.
        
        Uses dynamic edge-based allocation:
        - Base = deployable_capital / num_assets
        - Adjust by edge score relative to peers
        - Cap at max_per_asset_pct of total
        """
        n_assets = max(1, len(self.asset_names))
        base_alloc = self._deployable / n_assets

        # If we have edge scores, adjust allocation
        if self._edge_scores and len(self._edge_scores) > 1:
            scores = list(self._edge_scores.values())
            avg_edge = sum(scores) / len(scores)
            asset_edge = self._edge_scores.get(asset, 0.0)

            # Compute relative performance multiplier
            # Better-than-average → up to 1.5x, worse → down to 0.5x
            if avg_edge != 0:
                relative = asset_edge / max(0.001, abs(avg_edge))
                # Clamp to [0.5, 1.5]
                multiplier = max(0.5, min(1.5, 0.5 + relative * 0.5))
            else:
                multiplier = 1.0

            alloc = base_alloc * multiplier
        else:
            alloc = base_alloc

        # Hard cap
        hard_cap = self.total_capital * self.max_per_asset_pct
        return min(alloc, hard_cap)

    def can_deploy(self, asset: str, amount: float) -> bool:
        """Check if deploying `amount` for `asset` is within limits."""
        current = self._deployed.get(asset, 0.0)
        total_deployed = sum(self._deployed.values())
        alloc = self.allocation_for(asset)

        # Per-asset limit (dynamic)
        if current + amount > alloc:
            return False

        # Global limit (respect reserve)
        if total_deployed + amount > self._deployable:
            return False

        return True

    def record_deployment(self, asset: str, amount: float):
        """Record capital deployed for an asset."""
        self._deployed[asset] = self._deployed.get(asset, 0.0) + amount

    def record_recovery(self, asset: str, amount: float):
        """Record capital recovered (merge, settlement, etc.)."""
        self._deployed[asset] = max(0.0, self._deployed.get(asset, 0.0) - amount)

    def utilization(self) -> dict:
        """Get current capital utilization metrics."""
        total = sum(self._deployed.values())
        per_asset = {}
        for asset in self.asset_names:
            deployed = self._deployed.get(asset, 0.0)
            alloc = self.allocation_for(asset)
            per_asset[asset] = {
                "deployed": round(deployed, 2),
                "allocation": round(alloc, 2),
                "pct_used": round(deployed / max(0.01, alloc) * 100, 1),
                "edge_score": round(self._edge_scores.get(asset, 0.0), 6),
            }
        return {
            "total_deployed": round(total, 2),
            "total_capital": self.total_capital,
            "pct_utilized": round(total / max(0.01, self.total_capital) * 100, 1),
            "reserve": round(self.total_capital * self.reserve_pct, 2),
            "per_asset": per_asset,
        }
