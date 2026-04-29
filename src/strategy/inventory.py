"""
Inventory manager — tracks Up and Down tokens separately.

KEY DESIGN PRINCIPLE:
  This bot is a PAIR-MATCHING market maker. It ONLY BUYS.
  1 Up + 1 Down = guaranteed $1.00 at settlement.
  
  Therefore, inventory balance is measured by SHARE COUNT imbalance,
  NOT dollar delta. Dollar delta is misleading because when BTC moves
  decisively (e.g., P(Up)=0.01), it reports massive delta even if
  share counts are balanced.

  The goal: keep Up shares ≈ Down shares to maximize matched pairs.
"""

import time
from dataclasses import dataclass
from enum import Enum
from src.monitoring.logger import get_logger

log = get_logger("inventory")


class InventoryState(Enum):
    NORMAL = "NORMAL"
    SKEWED = "SKEWED"
    ONE_SIDED = "ONE_SIDED"
    EMERGENCY = "EMERGENCY"


@dataclass
class InventoryPosition:
    market_id: str
    asset: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_total_cost: float = 0.0
    no_total_cost: float = 0.0
    yes_fill_count: int = 0
    no_fill_count: int = 0

    def to_dict(self):
        return {
            "market_id": self.market_id,
            "asset": self.asset,
            "yes_shares": self.yes_shares,
            "no_shares": self.no_shares,
            "yes_total_cost": self.yes_total_cost,
            "no_total_cost": self.no_total_cost,
            "yes_fill_count": self.yes_fill_count,
            "no_fill_count": self.no_fill_count
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            market_id=data["market_id"],
            asset=data.get("asset", ""),
            yes_shares=data.get("yes_shares", 0.0),
            no_shares=data.get("no_shares", 0.0),
            yes_total_cost=data.get("yes_total_cost", 0.0),
            no_total_cost=data.get("no_total_cost", 0.0),
            yes_fill_count=data.get("yes_fill_count", 0),
            no_fill_count=data.get("no_fill_count", 0)
        )

    @property
    def yes_avg_entry(self) -> float:
        return self.yes_total_cost / self.yes_shares if self.yes_shares > 0 else 0.0

    @property
    def no_avg_entry(self) -> float:
        return self.no_total_cost / self.no_shares if self.no_shares > 0 else 0.0

    def share_imbalance(self) -> float:
        """
        Share-count imbalance: positive = more Up than Down.
        This is what matters for pair matching.
        
        Example: 270 Up, 143 Down → +127 (need more Down)
        """
        return self.yes_shares - self.no_shares

    def dollar_delta(self, current_mid: float) -> float:
        """Dollar-weighted delta (for display/logging only)."""
        return self.yes_shares * current_mid - self.no_shares * (1 - current_mid)

    def total_exposure(self, current_mid: float) -> float:
        return self.yes_shares * current_mid + self.no_shares * (1 - current_mid)

    def total_cost(self) -> float:
        """Total capital deployed."""
        return self.yes_total_cost + self.no_total_cost

    def mark_to_market(self, current_mid: float) -> float:
        yes_pnl = self.yes_shares * (current_mid - self.yes_avg_entry) if self.yes_shares > 0 else 0.0
        no_pnl = self.no_shares * ((1 - current_mid) - self.no_avg_entry) if self.no_shares > 0 else 0.0
        return yes_pnl + no_pnl

    def settlement_pnl(self, outcome_yes: bool) -> float:
        if outcome_yes:
            return self.yes_shares * 1.0 - self.yes_total_cost - self.no_total_cost
        else:
            return self.no_shares * 1.0 - self.no_total_cost - self.yes_total_cost

    def matched_pairs(self) -> float:
        return min(self.yes_shares, self.no_shares)

    def matched_pair_profit(self) -> float:
        pairs = self.matched_pairs()
        if pairs <= 0:
            return 0.0
        pair_cost = self.yes_avg_entry + self.no_avg_entry
        return pairs * (1.0 - pair_cost)


class InventoryManager:
    """
    Manages inventory using SHARE-COUNT imbalance, not dollar delta.
    
    Thresholds are in shares (not dollars):
      soft_limit:  Start reducing heavy side size
      hard_limit:  Heavily reduce heavy side (10% size)
      emergency:   Stop heavy side completely, full size on light side
    """
    def __init__(self, soft_limit=75.0, hard_limit=120.0, emergency=140.0,
                 max_imbalance=150.0, max_capital_per_market=500.0):
        """
        Args:
            soft_limit: Share imbalance to start skewing sizes.
            hard_limit: Share imbalance for aggressive skewing.
            emergency: Share imbalance to stop heavy side entirely.
            max_imbalance: Maximum imbalance for ratio calculation.
            max_capital_per_market: Max total cost deployed per market.
        """
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.emergency = emergency
        self.max_imbalance = max_imbalance
        self.max_capital_per_market = max_capital_per_market
        self.positions: dict[str, InventoryPosition] = {}
        self.state_manager = None

    def set_state_manager(self, state_manager):
        self.state_manager = state_manager
        # Load state if exists
        inv_state = self.state_manager.state.get("inventory", {})
        for mid, data in inv_state.items():
            self.positions[mid] = InventoryPosition.from_dict(data)

    def _save_state(self):
        if self.state_manager:
            data = {mid: pos.to_dict() for mid, pos in self.positions.items()}
            self.state_manager.update_inventory(data)

    def save_state(self):
        """Persist current inventory state."""
        self._save_state()

    def get_or_create(self, market_id: str, asset: str = "") -> InventoryPosition:
        if market_id not in self.positions:
            self.positions[market_id] = InventoryPosition(market_id=market_id, asset=asset)
        return self.positions[market_id]

    def record_fill(self, market_id: str, side: str, size: float, price: float, asset: str = ""):
        pos = self.get_or_create(market_id, asset)
        side = side.lower()
        if side in ("yes", "up"):
            pos.yes_total_cost += price * size
            pos.yes_shares += size
            pos.yes_fill_count += 1
        elif side in ("no", "down"):
            pos.no_total_cost += price * size
            pos.no_shares += size
            pos.no_fill_count += 1
        self._save_state()
        log.info("fill_recorded", market=market_id[:8], side=side, size=size, price=price,
                 up_shares=pos.yes_shares, down_shares=pos.no_shares,
                 imbalance=pos.share_imbalance())

    def get_state(self, market_id: str, current_mid: float = 0.5) -> InventoryState:
        """
        Determine inventory state based on SHARE COUNT imbalance.
        
        current_mid is accepted for API compatibility but NOT used
        for state determination. Only share counts matter.
        """
        pos = self.positions.get(market_id)
        if not pos:
            return InventoryState.NORMAL
        imbalance = abs(pos.share_imbalance())
        if imbalance >= self.emergency:
            return InventoryState.EMERGENCY
        elif imbalance >= self.hard_limit:
            return InventoryState.ONE_SIDED
        elif imbalance >= self.soft_limit:
            return InventoryState.SKEWED
        return InventoryState.NORMAL

    def compute_size_adjustment(self, market_id: str, current_mid: float,
                                base_size: int) -> tuple[int, int]:
        """
        Adjust buy sizes based on SHARE COUNT imbalance.
        
        Uses share_imbalance (Up - Down) not dollar delta.
        Positive imbalance = more Up shares → reduce Up, increase Down.
        Negative imbalance = more Down shares → reduce Down, increase Up.
        
        This ensures the bot always works toward building matched pairs
        regardless of where the market price is.
        """
        pos = self.positions.get(market_id)
        if not pos:
            return base_size, base_size

        imbalance = pos.share_imbalance()  # positive = too many Up
        abs_imbalance = abs(imbalance)
        imbalance_ratio = max(-1.0, min(1.0, imbalance / self.max_imbalance))
        state = self.get_state(market_id)

        if state == InventoryState.EMERGENCY:
            # STOP buying the heavy side, FULL SIZE on the light side
            if imbalance > 0:
                # Too many Up shares → stop Up, buy Down
                return 0, base_size
            else:
                # Too many Down shares → stop Down, buy Up
                return base_size, 0

        if state == InventoryState.ONE_SIDED:
            # Heavily reduce heavy side (10%), full size on light side
            if imbalance > 0:
                return max(1, int(base_size * 0.1)), base_size
            else:
                return base_size, max(1, int(base_size * 0.1))

        # SKEWED or NORMAL: proportional adjustment
        if imbalance_ratio > 0:
            # More Up → reduce Up buying, keep Down at full
            yes_size = int(base_size * max(0.2, 1 - imbalance_ratio * 0.8))
            no_size = base_size
        elif imbalance_ratio < 0:
            # More Down → reduce Down buying, keep Up at full
            yes_size = base_size
            no_size = int(base_size * max(0.2, 1 + imbalance_ratio * 0.8))
        else:
            yes_size, no_size = base_size, base_size

        return max(1, yes_size), max(1, no_size)

    def check_capital_limit(self, market_id: str, current_mid: float) -> dict:
        """
        Block the side with more shares if total cost exceeds limit.
        Uses total_cost (actual capital spent) not mark-to-market.
        """
        pos = self.positions.get(market_id)
        if not pos:
            return {}
        total_cost = pos.total_cost()
        if total_cost > self.max_capital_per_market:
            # Block the side with more shares (share-count based)
            if pos.yes_shares > pos.no_shares:
                return {"block_yes": True}
            else:
                return {"block_no": True}
        return {}

    def settle_market(self, market_id: str, outcome_yes: bool) -> float:
        pos = self.positions.pop(market_id, None)
        if not pos:
            return 0.0
        pnl = pos.settlement_pnl(outcome_yes)
        self._save_state()
        log.info("market_settled", market=market_id[:8],
                 outcome="YES" if outcome_yes else "NO",
                 pnl=round(pnl, 4), pairs=pos.matched_pairs(),
                 up_shares=pos.yes_shares, down_shares=pos.no_shares)
        return pnl

    def clear_market(self, market_id: str):
        """Remove a market position from memory/state without computing settlement PnL."""
        pos = self.positions.pop(market_id, None)
        if pos:
            self._save_state()
            log.info("market_cleared", market=market_id[:8],
                     up_shares=pos.yes_shares, down_shares=pos.no_shares)
