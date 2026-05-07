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

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from src.monitoring.logger import get_logger

log = get_logger("inventory")


@dataclass
class FillEntry:
    """A single fill with remaining unmatched shares for FIFO pair matching."""
    price: float
    size: float
    remaining: float
    timestamp: float = 0.0


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
    # FIFO pair matching: track individual fills for accurate per-pair P&L.
    # Without this, averaged entry prices inflate P&L when FV swings between
    # the YES fill and the NO fill (e.g., YES@$0.09 + NO@$0.51 = $0.40 "edge"
    # when in reality combined cost at quote time was ~$0.98).
    _yes_fills: list = field(default_factory=list)   # list[FillEntry]
    _no_fills: list = field(default_factory=list)     # list[FillEntry]
    _realized_pair_pnl: float = 0.0   # accumulated P&L from FIFO-matched pairs
    _realized_pairs: float = 0.0      # total FIFO-matched pair count
    _settled_pair_pnl: float = 0.0    # already reported to PnL tracker

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

    def locked_capital(self) -> float:
        """Capital locked in matched pairs (cannot be used until merge)."""
        pairs = self.matched_pairs()
        if pairs <= 0:
            return 0.0
        return pairs * (self.yes_avg_entry + self.no_avg_entry)

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

    def add_fill(self, side: str, price: float, size: float, ts: float = 0.0):
        """Record a fill and FIFO-match against the opposite side.

        This is the core of accurate per-pair P&L: each YES fill is matched
        against the oldest unmatched NO fill (and vice versa) instead of
        computing profit from time-averaged entry prices.
        """
        ts = ts or time.time()
        if side in ("yes", "up"):
            self.yes_total_cost += price * size
            self.yes_shares += size
            self.yes_fill_count += 1
            self._yes_fills.append(FillEntry(
                price=price, size=size, remaining=size, timestamp=ts))
        elif side in ("no", "down"):
            self.no_total_cost += price * size
            self.no_shares += size
            self.no_fill_count += 1
            self._no_fills.append(FillEntry(
                price=price, size=size, remaining=size, timestamp=ts))
        self._match_pairs()

    def _match_pairs(self):
        """FIFO drain: match oldest YES fill vs oldest NO fill."""
        while self._yes_fills and self._no_fills:
            yes_fill = self._yes_fills[0]
            no_fill = self._no_fills[0]

            if yes_fill.remaining <= 0:
                self._yes_fills.pop(0)
                continue
            if no_fill.remaining <= 0:
                self._no_fills.pop(0)
                continue

            match_size = min(yes_fill.remaining, no_fill.remaining)
            pair_pnl = match_size * (1.0 - (yes_fill.price + no_fill.price))
            self._realized_pair_pnl += pair_pnl
            self._realized_pairs += match_size

            yes_fill.remaining -= match_size
            no_fill.remaining -= match_size

            if yes_fill.remaining <= 0:
                self._yes_fills.pop(0)
            if no_fill.remaining <= 0:
                self._no_fills.pop(0)

    def matched_pair_profit(self) -> float:
        """Profit from FIFO-matched pairs, excluding already-settled amounts.

        This replaces the old averaged calculation:
            OLD: pairs * (1.0 - (yes_avg_entry + no_avg_entry))
            NEW: sum of per-pair (1.0 - (yes_fill_price + no_fill_price))
        """
        return self._realized_pair_pnl - self._settled_pair_pnl

    def acknowledge_settlement(self):
        """Mark current realized pair P&L as settled.

        Call after recording settlement to prevent double-counting
        on subsequent mid-market merges.
        """
        self._settled_pair_pnl = self._realized_pair_pnl

    def unmatched_exposure(self) -> float:
        """Capital at risk from unmatched tokens (could expire worthless)."""
        pairs = self.matched_pairs()
        unmatched_yes = self.yes_shares - pairs
        unmatched_no = self.no_shares - pairs
        yes_risk = unmatched_yes * self.yes_avg_entry if unmatched_yes > 0 else 0.0
        no_risk = unmatched_no * self.no_avg_entry if unmatched_no > 0 else 0.0
        return yes_risk + no_risk


class FillRateTracker:
    """Tracks per-side fill rates to detect asymmetry early.
    
    Used to skew quotes PREDICTIVELY before inventory imbalance
    reaches threshold limits. If Down fills are arriving 3x faster
    than Up fills, we start reducing Down size and boosting Up
    immediately instead of waiting for the imbalance to build.
    """

    def __init__(self, window_seconds: int = 120):
        self.window = window_seconds
        self.yes_fills: deque = deque()  # (timestamp, size)
        self.no_fills: deque = deque()

    def record(self, side: str, size: float, ts: float = None):
        ts = ts or time.time()
        if side.lower() in ("yes", "up"):
            self.yes_fills.append((ts, size))
        elif side.lower() in ("no", "down"):
            self.no_fills.append((ts, size))
        self._prune(ts)

    def _prune(self, now: float = None):
        now = now or time.time()
        cutoff = now - self.window
        while self.yes_fills and self.yes_fills[0][0] < cutoff:
            self.yes_fills.popleft()
        while self.no_fills and self.no_fills[0][0] < cutoff:
            self.no_fills.popleft()

    def fill_ratio(self) -> float:
        """Returns ratio of yes_volume / no_volume.
        >1 = filling more Up, <1 = filling more Down.
        Returns 1.0 if insufficient data."""
        self._prune()
        yes_vol = sum(s for _, s in self.yes_fills) or 0.001
        no_vol = sum(s for _, s in self.no_fills) or 0.001
        return yes_vol / no_vol

    def asymmetry_factor(self) -> float:
        """Returns a skew adjustment [-1, 1] based on fill asymmetry.
        Positive = too many Up fills → need more Down.
        Negative = too many Down fills → need more Up."""
        # Need at least 3 fills total to have meaningful signal
        if len(self.yes_fills) + len(self.no_fills) < 3:
            return 0.0
        ratio = self.fill_ratio()
        if ratio > 1:
            return min(1.0, (ratio - 1) / 3)  # cap at 3x ratio
        else:
            inv_ratio = 1.0 / max(0.01, ratio)
            return max(-1.0, -(inv_ratio - 1) / 3)

    def total_fill_rate(self) -> float:
        """Fills per minute across both sides."""
        self._prune()
        total = len(self.yes_fills) + len(self.no_fills)
        return total / (self.window / 60.0)


class InventoryManager:
    """
    Manages inventory using SHARE-COUNT imbalance, not dollar delta.
    
    Thresholds are in shares (not dollars):
      soft_limit:  Start reducing heavy side size
      hard_limit:  Heavily reduce heavy side
      emergency:   Stop heavy side completely, full size on light side
    
    Features:
      - Continuous exponential size decay (no blunt tier jumps)
      - Time-aware dynamic thresholds
      - Fill-rate asymmetry detection for predictive skew
      - Dollar-based auto-merge trigger
      - Capital arbiter integration for cross-asset coordination
    """
    def __init__(self, soft_limit=75.0, hard_limit=120.0, emergency=140.0,
                 max_imbalance=150.0, max_capital_per_market=500.0,
                 auto_merge_dollar_threshold=15.0):
        """
        Args:
            soft_limit: Share imbalance to start skewing sizes.
            hard_limit: Share imbalance for aggressive skewing.
            emergency: Share imbalance to stop heavy side entirely.
            max_imbalance: Maximum imbalance for ratio calculation.
            max_capital_per_market: Max total cost deployed per market.
            auto_merge_dollar_threshold: Merge when locked capital exceeds this ($).
        """
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.emergency = emergency
        self.max_imbalance = max_imbalance
        self.max_capital_per_market = max_capital_per_market
        self.auto_merge_dollar_threshold = auto_merge_dollar_threshold
        self.positions: dict[str, InventoryPosition] = {}
        self.state_manager = None
        self.capital_arbiter = None  # Optional cross-asset coordinator
        self.fill_tracker = FillRateTracker(window_seconds=120)

    def set_state_manager(self, state_manager):
        self.state_manager = state_manager
        # Load state if exists
        inv_state = self.state_manager.state.get("inventory", {})
        for mid, data in inv_state.items():
            self.positions[mid] = InventoryPosition.from_dict(data)

    def set_capital_arbiter(self, arbiter):
        """Set the shared cross-asset capital coordinator."""
        self.capital_arbiter = arbiter

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

        # Delegate to position's FIFO-matching fill recorder
        pos.add_fill(side, price, size)

        # Track fill rate for asymmetry detection
        self.fill_tracker.record(side, size)

        # Update capital arbiter if present
        if self.capital_arbiter and asset:
            self.capital_arbiter.record_deployment(asset, price * size)

        self._save_state()
        log.info("fill_recorded", market=market_id[:8], side=side, size=size, price=price,
                 up_shares=pos.yes_shares, down_shares=pos.no_shares,
                 pair_pnl=round(pos._realized_pair_pnl, 4),
                 imbalance=pos.share_imbalance())

    def get_state(self, market_id: str, current_mid: float = 0.5,
                  t_normalized: float = 1.0) -> InventoryState:
        """
        Determine inventory state based on SHARE COUNT imbalance,
        with time-aware dynamic thresholds.
        
        As expiry approaches, thresholds tighten because there's
        less time to accumulate the offsetting side.
        """
        pos = self.positions.get(market_id)
        if not pos:
            return InventoryState.NORMAL

        soft, hard, emerg = self._dynamic_thresholds(t_normalized)
        imbalance = abs(pos.share_imbalance())

        if imbalance >= emerg:
            return InventoryState.EMERGENCY
        elif imbalance >= hard:
            return InventoryState.ONE_SIDED
        elif imbalance >= soft:
            return InventoryState.SKEWED
        return InventoryState.NORMAL

    def _dynamic_thresholds(self, t_normalized: float) -> tuple:
        """Compute time-aware inventory limits.
        
        As expiry approaches, tighten limits because there's
        less time to accumulate the offsetting side.
        
        t_normalized: 1.0 = window just opened, 0.0 = expiring
        """
        # Time factor: tighten limits as expiry approaches
        # At t=1.0 (fresh): use config values (factor=1.0)
        # At t=0.33 (5min left): factor=0.47 → roughly halve limits
        # At t=0.11 (1.5min left): factor=0.25 → quarter limits
        time_factor = max(0.25, min(1.0, t_normalized / 0.7))

        return (
            self.soft_limit * time_factor,
            self.hard_limit * time_factor,
            self.emergency * time_factor,
        )

    def compute_size_adjustment(self, market_id: str, current_mid: float,
                                base_size: int,
                                t_normalized: float = 1.0) -> tuple[int, int]:
        """
        Adjust buy sizes using CONTINUOUS exponential decay.
        
        Replaces the old blunt 4-tier system with smooth scaling:
        - Heavy side decays exponentially as imbalance grows
        - Light side gets a proportional boost
        - Fill-rate asymmetry provides predictive skew
        - Time-aware: taper new positions as expiry approaches
        
        Uses share_imbalance (Up - Down) not dollar delta.
        Positive imbalance = more Up shares → reduce Up, increase Down.
        Negative imbalance = more Down shares → reduce Down, increase Up.
        """
        pos = self.positions.get(market_id)
        if not pos:
            return base_size, base_size

        imbalance = pos.share_imbalance()  # positive = too many Up
        abs_imbalance = abs(imbalance)

        # Use dynamic thresholds
        _, _, emerg = self._dynamic_thresholds(t_normalized)

        # Hard emergency behavior: stop heavy side entirely, keep light side at
        # base_size (no boosts). This is relied on by invariants/tests.
        if abs_imbalance >= emerg:
            if imbalance > 0:
                return 0, base_size
            elif imbalance < 0:
                return base_size, 0
            return 0, 0

        # --- Continuous exponential decay ---
        # k controls how aggressively we cut the heavy side
        # At emergency threshold → ~5% of base size
        k = 3.0 / max(1.0, emerg)
        heavy_factor = math.exp(-k * abs_imbalance)

        # Light side gets a boost proportional to imbalance (up to 2x)
        light_boost = min(2.0, 1.0 + abs_imbalance / max(1.0, self.soft_limit) * 0.5)

        # --- Fill-rate asymmetry: predictive adjustment ---
        # Blend fill asymmetry with inventory imbalance (more aggressive in last 5 min)
        fill_asym = self.fill_tracker.asymmetry_factor()
        if t_normalized < 0.33:
            # Last ~5 minutes: fill asymmetry is 2x more aggressive
            fill_weight = 0.4
        else:
            fill_weight = 0.2

        # Combine: positive combined_signal means too many Up / too many Up fills
        inv_signal = imbalance / max(1.0, self.max_imbalance)
        combined_signal = inv_signal * (1 - fill_weight) + fill_asym * fill_weight

        if combined_signal > 0:
            # Too many Up (or Up filling faster) → reduce Up, boost Down
            yes_size = max(0, int(base_size * heavy_factor))
            no_size = min(base_size * 2, int(base_size * light_boost))
        elif combined_signal < 0:
            # Too many Down (or Down filling faster) → reduce Down, boost Up
            yes_size = min(base_size * 2, int(base_size * light_boost))
            no_size = max(0, int(base_size * heavy_factor))
        else:
            yes_size, no_size = base_size, base_size

        # --- Time-aware tapering ---
        # In the second half of the window, taper new positions on both sides
        # Exception: light side keeps size for pair completion
        if t_normalized < 0.5:
            taper = max(0.3, t_normalized / 0.5)
            if combined_signal > 0:
                # Up is heavy → only taper Up (heavy) side further
                yes_size = max(0, int(yes_size * taper))
            elif combined_signal < 0:
                # Down is heavy → only taper Down (heavy) side further
                no_size = max(0, int(no_size * taper))
            else:
                # Balanced → taper both
                yes_size = max(0, int(yes_size * taper))
                no_size = max(0, int(no_size * taper))

        return yes_size, no_size

    def check_capital_limit(self, market_id: str, current_mid: float,
                            asset: str = "") -> dict:
        """
        Block the side with more shares if total cost exceeds limit.
        Uses total_cost (actual capital spent) not mark-to-market.
        Also checks the cross-asset capital arbiter if available.
        """
        pos = self.positions.get(market_id)
        if not pos:
            return {}

        blocks = {}

        # Per-market limit
        total_cost = pos.total_cost()
        if total_cost > self.max_capital_per_market:
            if pos.yes_shares > pos.no_shares:
                blocks["block_yes"] = True
            else:
                blocks["block_no"] = True

        # Cross-asset capital arbiter check
        if self.capital_arbiter and asset:
            # Estimate next order cost (use 0.50 as typical price)
            est_cost = 0.50 * 10  # base_size * typical price
            if not self.capital_arbiter.can_deploy(asset, est_cost):
                # Block the side with more shares to preserve capital
                if pos.yes_shares >= pos.no_shares:
                    blocks["block_yes"] = True
                else:
                    blocks["block_no"] = True

        return blocks

    def should_merge(self, market_id: str) -> bool:
        """Check if a market has enough locked capital to warrant a mid-market merge.
        
        Uses dollar-based threshold: merge when locked capital in matched
        pairs exceeds auto_merge_dollar_threshold (default $15).
        """
        pos = self.positions.get(market_id)
        if not pos:
            return False
        locked = pos.locked_capital()
        return locked >= self.auto_merge_dollar_threshold

    def settle_market(self, market_id: str, outcome_yes: Optional[bool] = None) -> float:
        pos = self.positions.pop(market_id, None)
        if not pos:
            return 0.0
        pnl = pos.settlement_pnl(outcome_yes) if outcome_yes is not None else 0.0
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
            # Return deployed capital to arbiter
            if self.capital_arbiter and pos.asset:
                self.capital_arbiter.record_recovery(pos.asset, pos.total_cost())
            self._save_state()
            log.info("market_cleared", market=market_id[:8],
                     up_shares=pos.yes_shares, down_shares=pos.no_shares)
