"""
Quote engine for Polymarket 15-minute binary crypto markets.

STRATEGY: Rebate-first market making.
  - Quote at or near top-of-book to maximize fill rate
  - Spreads should match market reality (1-2 cent tick)
  - Combined Up+Down cost < $1.00 for guaranteed pair profit
  - Inventory skew adjusts WHICH side gets the tighter price

This replaces the Avellaneda-Stoikov model which produced
spreads 10-25x wider than actual market spreads.
"""

import math
import time

from collections import deque
from dataclasses import dataclass
from typing import Optional

from src.monitoring.logger import get_logger

log = get_logger("quote_engine")

# Polymarket tick size for crypto binary markets
TICK_SIZE = 0.01


@dataclass
class QuoteResult:
    """Generated quotes for a single market."""
    yes_buy_price: Optional[float] = None
    no_buy_price: Optional[float] = None
    yes_buy_size: int = 0
    no_buy_size: int = 0
    reservation_price: float = 0.0
    spread: float = 0.0
    combined_cost: float = 0.0   # yes_buy + no_buy (must be < 1.0)
    edge_per_pair: float = 0.0   # 1.0 - combined_cost
    t_normalized: float = 0.0
    phase: str = "ACTIVE"


class KappaEstimator:
    """Estimates order arrival rate from live fill data."""
    def __init__(self, window_seconds: int = 300):
        self.fills: deque = deque()
        self.window = window_seconds

    def record_fill(self, spread: float = 0.0):
        now = time.time()
        self.fills.append((now, spread))
        while self.fills and self.fills[0][0] < now - self.window:
            self.fills.popleft()

    def kappa(self) -> float:
        now = time.time()
        while self.fills and self.fills[0][0] < now - self.window:
            self.fills.popleft()
        if len(self.fills) < 2:
            return 1.5
        elapsed = self.fills[-1][0] - self.fills[0][0]
        return len(self.fills) / elapsed if elapsed > 0 else 1.5


class QuoteEngine:
    """
    Generates BUY-ONLY quotes for both Up and Down tokens.

    Uses a market-appropriate spread model:
    - Base spread = edge_ticks × tick_size (typically 1-2 cents)
    - Inventory skew adjusts which side gets tighter/wider
    - Near-expiry widens spread for adverse selection protection
    - Combined cost always < $1.00

    This is NOT Avellaneda-Stoikov. A-S produces 15-25 cent spreads
    which are unusable in markets with 1-cent native spread.
    """

    def __init__(self, gamma: float = 0.15, min_spread: float = 0.01,
                 max_spread: float = 0.10, max_order_size: int = 30,
                 edge_ticks: float = 0.5):
        """
        Args:
            gamma: Inventory skew intensity (0 = no skew, 1 = max skew).
            min_spread: Minimum half-spread per side (in dollars).
            max_spread: Maximum half-spread per side.
            max_order_size: Maximum order size in shares.
            edge_ticks: Number of ticks below fair value to quote.
                        0.5 = quote at fair - $0.005 (0.99 combined cost)
                        1 = quote at fair - $0.01 (0.98 combined cost)
        """
        self.gamma = gamma
        self.base_gamma = gamma
        self.min_spread = min_spread
        self.max_spread = max_spread
        self.max_order_size = max_order_size
        self.base_order_size = max_order_size
        self.edge_ticks = edge_ticks
        self.spread_multiplier = 1.0
        self.kappa_estimator = KappaEstimator()

    def generate_quotes(self, fair_value: float, t_normalized: float,
                        sigma: float, share_imbalance: float,
                        max_imbalance: float,
                        yes_size: int, no_size: int,
                        best_ask_yes: Optional[float] = None,
                        best_ask_no: Optional[float] = None,
                        best_bid_yes: Optional[float] = None,
                        best_bid_no: Optional[float] = None) -> QuoteResult:
        """
        Generate BUY quotes for Up and Down tokens.

        The approach:
        1. Start with fair_value as the center
        2. Apply inventory skew based on SHARE COUNT imbalance
        3. Place buy orders edge_ticks below the center
        4. Anchor to live orderbook (never fall behind best_bid)
        5. Tighten light side when imbalanced (complementary pricing)
        6. Ensure combined cost < $1.00 (symmetric enforcement)
        7. Widen near expiry

        Args:
            fair_value: P(Up) ∈ [0.01, 0.99].
            t_normalized: Time remaining [0,1]. 1=just started, 0=expiring.
            sigma: Annualized volatility.
            share_imbalance: Up_shares - Down_shares. Positive = too many Up.
            max_imbalance: Maximum share imbalance for ratio calculation.
            yes_size: Adjusted buy size for Up token.
            no_size: Adjusted buy size for Down token.
            best_bid_yes: Live orderbook best bid for YES token.
            best_bid_no: Live orderbook best bid for NO token.
        """
        result = QuoteResult(t_normalized=t_normalized)

        # 1. Compute inventory skew from SHARE COUNT imbalance
        #    Positive imbalance = too many Up → push Up buy lower, Down buy higher
        imb_ratio = share_imbalance / max(1.0, max_imbalance)
        imb_ratio = max(-1.0, min(1.0, imb_ratio))

        # Time-escalating skew: stronger adjustment as window progresses
        # At t=1.0 (fresh): use base gamma
        # At t=0.33 (5min left): ~2x gamma for faster pair completion
        time_urgency = 1.0 + max(0.0, 1.0 - t_normalized / 0.5)
        effective_gamma = self.gamma * min(2.5, time_urgency)
        skew = imb_ratio * effective_gamma * 0.5

        # Reservation price = inventory-adjusted center
        reservation = fair_value - skew
        reservation = max(0.005, min(0.995, reservation))
        result.reservation_price = round(reservation, 4)


        # 2. Compute base spread (per side)
        base_half_spread = self.edge_ticks * TICK_SIZE * self.spread_multiplier

        # 3. Time-based spread widening near expiry
        time_spread = self._time_spread_adjustment(t_normalized)
        half_spread = max(base_half_spread, time_spread)
        half_spread = max(self.min_spread, min(self.max_spread, half_spread))

        result.spread = round(half_spread * 2, 4)

        # 4. Compute buy prices with ASYMMETRIC spread
        #    Light side (needs fills) gets tighter spread
        #    Heavy side (too much inventory) gets wider spread
        if abs(imb_ratio) > 0.15:
            # Scale: at imb_ratio=1.0, light gets 40% tighter, heavy 40% wider
            tighten = min(0.4, abs(imb_ratio) * 0.4)
            light_spread = half_spread * (1.0 - tighten)
            heavy_spread = half_spread * (1.0 + tighten)
        else:
            light_spread = half_spread
            heavy_spread = half_spread

        if imb_ratio > 0:
            # Too many YES → tighten NO (light), widen YES (heavy)
            yes_buy = reservation - heavy_spread
            no_buy = (1.0 - reservation) - light_spread
        elif imb_ratio < 0:
            # Too many NO → tighten YES (light), widen NO (heavy)
            yes_buy = reservation - light_spread
            no_buy = (1.0 - reservation) - heavy_spread
        else:
            yes_buy = reservation - half_spread
            no_buy = (1.0 - reservation) - half_spread

        # 5. Clamp to valid range
        # If our calculated price is practically zero, we shouldn't bid at all
        if yes_buy < 0.005:
            yes_size = 0
        if no_buy < 0.005:
            no_size = 0

        yes_buy = max(0.01, min(0.99, yes_buy))
        no_buy = max(0.01, min(0.99, no_buy))


        # 6. Orderbook clamping: prevent crossing the book
        orig_yes = yes_buy
        orig_no = no_buy

        if best_ask_yes is not None and yes_buy >= best_ask_yes:
            yes_buy = best_ask_yes - 0.01
        if best_ask_no is not None and no_buy >= best_ask_no:
            no_buy = best_ask_no - 0.01

        # 6.5. Spread Re-Centering (Orderbook Shadowing)
        #    When one side gets clamped by the book, nudge the OTHER side
        #    up by the same amount to keep spreads tight.
        #    CRITICAL: Cap the adjustment to MAX_RECENTER (3 cents).
        #    At extreme FVs (e.g. 0.04) the model price diverges hugely
        #    from the book, creating clamp drops of 90+ cents which would
        #    otherwise catapult the opposite side to a nonsensical price.
        MAX_RECENTER = 0.03
        yes_clamp_drop = orig_yes - yes_buy
        no_clamp_drop = orig_no - no_buy

        if yes_clamp_drop > 0 and no_clamp_drop <= 0:
            adjustment = min(yes_clamp_drop, MAX_RECENTER)
            no_buy += adjustment
            if best_ask_no is not None and no_buy >= best_ask_no:
                no_buy = best_ask_no - 0.01

        elif no_clamp_drop > 0 and yes_clamp_drop <= 0:
            adjustment = min(no_clamp_drop, MAX_RECENTER)
            yes_buy += adjustment
            if best_ask_yes is not None and yes_buy >= best_ask_yes:
                yes_buy = best_ask_yes - 0.01

        # 7. Orderbook anchoring: light side should never fall behind best_bid
        #    If our bid is more than 1 tick below the book's best bid, join the queue.
        #    This prevents the light side from going stale while the market moves.
        if abs(imb_ratio) > 0.1:
            if imb_ratio > 0 and best_bid_no is not None:
                # NO is light side — anchor to book
                if no_buy < best_bid_no and no_size > 0:
                    no_buy = best_bid_no
                    # Safety: don't cross the book
                    if best_ask_no is not None and no_buy >= best_ask_no:
                        no_buy = best_ask_no - 0.01
            elif imb_ratio < 0 and best_bid_yes is not None:
                # YES is light side — anchor to book
                if yes_buy < best_bid_yes and yes_size > 0:
                    yes_buy = best_bid_yes
                    if best_ask_yes is not None and yes_buy >= best_ask_yes:
                        yes_buy = best_ask_yes - 0.01

        yes_buy = max(0.01, yes_buy)
        no_buy = max(0.01, no_buy)

        # 8. CRITICAL: combined cost must be < $1.00
        #    Symmetric enforcement: drop the HEAVY side (not always YES)
        yes_buy = round(yes_buy, 2)
        no_buy = round(no_buy, 2)

        combined = yes_buy + no_buy

        if combined >= 1.0:
            overshoot = combined - 0.99
            cents_to_drop = max(1, round(overshoot * 100))
            if imb_ratio > 0:
                # YES is heavy → drop YES price (preserve NO for fill attraction)
                yes_buy -= cents_to_drop * 0.01
            elif imb_ratio < 0:
                # NO is heavy → drop NO price (preserve YES for fill attraction)
                no_buy -= cents_to_drop * 0.01
            else:
                # Balanced → split the drop evenly
                yes_buy -= 0.01
                if yes_buy + no_buy >= 1.0:
                    no_buy -= 0.01

        yes_buy = max(0.01, yes_buy)
        no_buy = max(0.01, no_buy)

        # 9. Directional sanity guard.
        #
        # Orderbook clamping + re-centering can otherwise create quotes that
        # contradict our own fair value when inventory is flat. Example from
        # the 35-window run: FV≈0.46, NO ask clamps to 0.48, then re-centering
        # pushes YES to 0.48 while NO is 0.47. That is not an inventory repair;
        # it is the bot chasing a book that disagrees with its model.
        #
        # Allow the inversion only when it is explicitly the inventory repair
        # side: too many NO -> bid YES harder, or too many YES -> bid NO harder.
        repair_allows_yes_over_no = fair_value < 0.50 and imb_ratio < -0.1
        repair_allows_no_over_yes = fair_value > 0.50 and imb_ratio > 0.1
        if (
            fair_value < 0.50
            and yes_size > 0
            and no_size > 0
            and yes_buy > no_buy
            and not repair_allows_yes_over_no
        ):
            yes_buy = no_buy
        elif (
            fair_value > 0.50
            and yes_size > 0
            and no_size > 0
            and no_buy > yes_buy
            and not repair_allows_no_over_yes
        ):
            no_buy = yes_buy

        combined = round(yes_buy + no_buy, 4)

        result.yes_buy_price = round(yes_buy, 2)
        result.no_buy_price = round(no_buy, 2)
        result.yes_buy_size = yes_size
        result.no_buy_size = no_size
        result.combined_cost = combined
        result.edge_per_pair = round(1.0 - combined, 4)

        return result

    def _time_spread_adjustment(self, t_normalized: float) -> float:
        """
        Widen spread near expiry to protect against adverse selection.

        As t → 0 (expiry), the binary outcome becomes more certain
        and adverse selection risk increases sharply.
        """
        if t_normalized < 0.05:
            # Last ~45 seconds: 5 ticks wide
            return 5 * TICK_SIZE
        elif t_normalized < 0.15:
            # Last ~2 minutes: 3 ticks wide
            return 3 * TICK_SIZE
        elif t_normalized < 0.30:
            # Last ~4 minutes: 2 ticks wide
            return 2 * TICK_SIZE
        # Normal: 1 tick
        return self.edge_ticks * TICK_SIZE

    def reset_params(self):
        """Reset dynamic parameters to base values."""
        self.gamma = self.base_gamma
        self.max_order_size = self.base_order_size
        self.spread_multiplier = 1.0
