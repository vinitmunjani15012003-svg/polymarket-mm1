"""
Market cycler — manages the continuous 15-minute market lifecycle.

For each asset: discover market → quote → wind down → settle → repeat.

NOTE: These are "Up or Down" markets (directional), not strike-based.
Fair value = P(price goes UP from window start to window end).
"""

import asyncio
import traceback
import time as _time
from typing import Optional

from src.config import AssetConfig, GlobalConfig
from src.data.market_discovery import MarketDiscovery, MarketInfo
from src.data.orderbook import OrderBookReader
from src.strategy.volatility import VolatilityEstimator
from src.strategy.quote_engine import QuoteEngine, MAX_COMBINED_COST
from src.strategy.inventory import InventoryManager
from src.execution.order_manager import OrderManager
from src.execution.ctf_ops import (
    CTFOperations, GaslessMerger, BalanceMonitor,
    infer_collateral_token_for_market,
)
from src.risk.regime_filter import RegimeFilter
from src.risk.toxicity import FillEdgeTracker, ToxicityMonitor
from src.risk.risk_engine import (RiskEngine, determine_phase,
                                   apply_phase_params, pre_trade_checks)
from src.monitoring.pnl_tracker import PnLTracker
from src.monitoring.logger import get_logger

log = get_logger("market_cycler")

# Live repair must leave a real buffer. One cent was too thin after tick
# rounding, CLOB/API price normalization, and partial fill sequencing; live
# pairs repeatedly landed at/above 1.00. Two cents is still tight, but stops the
# bot from recycling capital into guaranteed-loss pairs.
MIN_LIVE_PAIR_EDGE = 0.02

# When flat and the model has a meaningful directional lean, enter on the side
# favored by fair value first, then let the existing inventory-repair path quote
# only the opposite side after a fill. This avoids opening with the adverse/cheap
# side just because both sides are atomically quotable.
# In live flat entry, never rest both sides at once. Pick the side with
# stronger model edge first, then let close-only repair quote the complement
# after that leg fills. A neutral tie is held flat instead of opening inventory.
FV_FAVORED_ENTRY_THRESHOLD = 0.50
FV_FAVORED_ENTRY_MIN_EDGE = 0.0
FV_FAVORED_ENTRY_MAX_SIZE = 5
FV_FAVORED_ENTRY_STOP_SECONDS = 600

# Live FV safety. If the external spot feed is stale, all model prices are
# suspect; remove active quotes rather than trading on frozen FV. The basis guard
# compares model P(Up) against Polymarket's current implied P(Up); a large gap
# means the Binance→Chainlink fixed-spread assumption is probably drifting.
MAX_SPOT_PRICE_AGE_SECONDS = 3.0
BASIS_GUARD_MAX_FV_DEVIATION = 0.12
# FV blending: the raw sigma model is useful but too jumpy by itself early in
# 15m windows. Blend it with Polymarket-implied probability when books are
# available, and otherwise temper model confidence by elapsed time + move size.
FV_MIN_MODEL_CONFIDENCE = 0.10
FV_MAX_MODEL_CONFIDENCE = 0.85
FV_DISAGREEMENT_CONFIDENCE_CAP = 0.05
FV_HARD_DISAGREEMENT = 0.15


def clamp_probability(value: float, lo: float = 0.01, hi: float = 0.99) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return 0.50


def fv_model_confidence(model_fv: float,
                        elapsed_fraction: float,
                        standardized_move: float,
                        market_fv: Optional[float] = None,
                        min_confidence: float = FV_MIN_MODEL_CONFIDENCE,
                        max_confidence: float = FV_MAX_MODEL_CONFIDENCE) -> float:
    """Confidence weight for raw model FV in a 15m binary window.

    Edge cases:
    - first seconds: stay near market/neutral
    - large standardized move: trust model more
    - hard model-vs-market disagreement: cap confidence, don't blindly follow it
    """
    elapsed = max(0.0, min(1.0, float(elapsed_fraction or 0.0)))
    move = max(0.0, float(standardized_move or 0.0))
    time_component = 0.60 * (elapsed ** 0.75)
    move_component = 0.25 * min(1.0, move / 1.5)
    confidence = min_confidence + time_component + move_component
    confidence = max(min_confidence, min(max_confidence, confidence))

    if market_fv is not None and abs(clamp_probability(model_fv) - clamp_probability(market_fv)) >= FV_HARD_DISAGREEMENT:
        confidence = min(confidence, FV_DISAGREEMENT_CONFIDENCE_CAP)
    return max(0.0, min(1.0, confidence))


def spot_from_binary_probability(start_price: float,
                                 p_up: float,
                                 sigma: float,
                                 time_remaining: float) -> Optional[float]:
    """Invert binary P(Up) into the live spot implied by market probability."""
    if not start_price or p_up is None or not sigma or time_remaining <= 0:
        return None
    try:
        from scipy.stats import norm
        import math
        p = max(0.02, min(0.98, float(p_up)))
        t_years = max(1.0, float(time_remaining)) / (365.25 * 86400)
        return float(start_price) * math.exp(norm.ppf(p) * float(sigma) * math.sqrt(t_years))
    except Exception:
        return None


def blended_fair_value(model_fv: float,
                       market_fv: Optional[float],
                       confidence: float) -> float:
    model = clamp_probability(model_fv)
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    if market_fv is None:
        # No book: temper raw model toward neutral using the same confidence.
        return clamp_probability(0.5 + (model - 0.5) * conf)
    market = clamp_probability(market_fv)
    return clamp_probability(conf * model + (1.0 - conf) * market)


def polymarket_implied_up_mid(book_up, book_down) -> Optional[float]:
    """Estimate Polymarket-implied P(Up) from YES and NO order books."""
    mids = []
    if book_up and getattr(book_up, "best_bid", 0) > 0 and getattr(book_up, "best_ask", 0) > 0:
        mids.append((float(book_up.best_bid) + float(book_up.best_ask)) / 2.0)
    if book_down and getattr(book_down, "best_bid", 0) > 0 and getattr(book_down, "best_ask", 0) > 0:
        down_mid = (float(book_down.best_bid) + float(book_down.best_ask)) / 2.0
        mids.append(1.0 - down_mid)
    if not mids:
        return None
    return max(0.0, min(1.0, sum(mids) / len(mids)))


def basis_guard_triggered(fair_value: float,
                          polymarket_mid_up: Optional[float],
                          threshold: float = BASIS_GUARD_MAX_FV_DEVIATION) -> bool:
    if polymarket_mid_up is None:
        return False
    fv = max(0.0, min(1.0, float(fair_value or 0.5)))
    return abs(fv - polymarket_mid_up) >= threshold


def start_price_disagrees_with_market(start_price: float,
                                      current_spot: float,
                                      sigma: float,
                                      event_start_ts: float,
                                      resolve_ts: float,
                                      market_fv: Optional[float],
                                      threshold: float = 0.25,
                                      now_ts: Optional[float] = None) -> bool:
    """Return True when a candidate price-to-beat is implausible vs live books."""
    if market_fv is None or not start_price or not current_spot:
        return False
    model_fv = UpDownFairValue(
        event_start_ts=event_start_ts,
        resolve_ts=resolve_ts,
        start_price=start_price,
    ).fair_value(current_spot, sigma, now_ts=now_ts, update_state=False)
    return abs(clamp_probability(model_fv) - clamp_probability(market_fv)) >= threshold


def apply_fv_favored_entry_mode(quotes, fair_value: float, share_imbalance: float,
                                min_order_size: int,
                                threshold: float = FV_FAVORED_ENTRY_THRESHOLD,
                                best_ask_yes: Optional[float] = None,
                                best_ask_no: Optional[float] = None,
                                best_bid_yes: Optional[float] = None,
                                best_bid_no: Optional[float] = None,
                                min_pair_edge: float = MIN_LIVE_PAIR_EDGE,
                                min_entry_edge: float = FV_FAVORED_ENTRY_MIN_EDGE,
                                max_entry_size: int = FV_FAVORED_ENTRY_MAX_SIZE) -> str | None:
    """Quote only the best FV-edge side while flat, if it is repairable.

    Returns "yes" or "no" when it converted normal flat quoting to one-sided
    FV entry, "blocked" when opening a one-sided leg is too risky, otherwise
    returns None.
    """
    if abs(share_imbalance) >= min_order_size:
        return None
    if quotes.yes_buy_size <= 0 or quotes.no_buy_size <= 0:
        return None

    yes_price = float(quotes.yes_buy_price or 0)
    no_price = float(quotes.no_buy_price or 0)
    if yes_price <= 0 or no_price <= 0:
        return None

    fv = max(0.0, min(1.0, float(fair_value or 0.5)))
    yes_edge = fv - yes_price
    no_edge = (1.0 - fv) - no_price

    side = None
    edge_epsilon = 1e-9
    if fv > threshold + edge_epsilon and yes_edge >= min_entry_edge:
        side = "yes"
    elif fv < (1.0 - threshold) - edge_epsilon and no_edge >= min_entry_edge:
        side = "no"

    if not side:
        quotes.yes_buy_size = 0
        quotes.no_buy_size = 0
        return "blocked"

    # Before opening a one-sided leg, require that the complementary repair leg
    # is not far behind the current maker queue while preserving pair edge.
    # Checking against best_ask was too strict and stopped quoting entirely in
    # normal 59/40 style books; repair is a BUY bid, so best_bid proximity is the
    # right maker-feasibility check.
    max_repair_bid_lag = 0.02
    if side == "yes":
        repair_cap = 1.0 - yes_price - min_pair_edge
        if best_bid_no is not None:
            repair_too_far = repair_cap < float(best_bid_no) - max_repair_bid_lag
        else:
            repair_too_far = best_ask_no is not None and (float(best_ask_no) - 0.01) > repair_cap
        if repair_too_far:
            quotes.yes_buy_size = 0
            quotes.no_buy_size = 0
            return "blocked"
        quotes.yes_buy_size = min(int(quotes.yes_buy_size), max(min_order_size, int(max_entry_size)))
        quotes.no_buy_size = 0
        return "yes"

    repair_cap = 1.0 - no_price - min_pair_edge
    if best_bid_yes is not None:
        repair_too_far = repair_cap < float(best_bid_yes) - max_repair_bid_lag
    else:
        repair_too_far = best_ask_yes is not None and (float(best_ask_yes) - 0.01) > repair_cap
    if repair_too_far:
        quotes.yes_buy_size = 0
        quotes.no_buy_size = 0
        return "blocked"
    quotes.no_buy_size = min(int(quotes.no_buy_size), max(min_order_size, int(max_entry_size)))
    quotes.yes_buy_size = 0
    return "no"


def repair_min_edge_for_remaining(remaining: float, repair_mode: str) -> float:
    """Relax pair edge only for close-only repair as expiry approaches.

    Carrying a naked wrong-side tail is often worse than completing a scratch
    pair. Normal quoting keeps the full live buffer; repair-only quotes can use
    a smaller buffer near expiry.
    """
    if repair_mode not in ("repair_up", "repair_down"):
        return MIN_LIVE_PAIR_EDGE
    if remaining <= 90:
        return 0.0
    if remaining <= 240:
        return 0.005
    if remaining <= 480:
        return 0.01
    return MIN_LIVE_PAIR_EDGE


class UpDownFairValue:
    """
    Fair value for "Up or Down" markets.

    P(Up) = P(price at end >= price at start)
    Uses drift + vol from CEX data. At-the-money ~ 0.50.

    For short horizons with no drift: P(Up) ≈ 0.50
    With observed drift: P(Up) = Φ(drift / (sigma * sqrt(T)))
    """

    def __init__(self, event_start_ts: float, resolve_ts: float,
                 start_price: float = None):
        self.event_start_ts = event_start_ts
        self.resolve_ts = resolve_ts
        self.start_price = start_price  # Price at window open
        self._last_fair_value = 0.50
        self._last_update_ts = 0.0

    def fair_value(self, current_price: float, sigma_annualized: float,
                   now_ts: float = None, update_state: bool = True) -> float:
        """
        Compute P(Up) = P(price_end >= price_start).

        If we know the start price: uses log(current/start) as drift signal.
        If we don't: defaults to 0.50 (no edge from price level).
        """
        from scipy.stats import norm
        import math

        now_ts = now_ts or _time.time()
        t_remaining = max(1, self.resolve_ts - now_ts)
        t_years = t_remaining / (365.25 * 86400)

        if self.start_price and self.start_price > 0 and current_price > 0:
            # We know the start price — compute drift-based fair value
            log_return_so_far = math.log(current_price / self.start_price)

            vol_sqrt_t = sigma_annualized * math.sqrt(t_years)
            if vol_sqrt_t < 1e-10:
                # Near zero vol remaining: deterministic
                return 0.99 if log_return_so_far >= 0 else 0.01

            # If price is currently above start, it's more likely to end above
            # d = drift_so_far / remaining_vol
            d = log_return_so_far / vol_sqrt_t
            prob = norm.cdf(d)
        else:
            # No start price: assume 50/50
            prob = 0.50

        prob = max(0.01, min(0.99, prob))
        if update_state:
            self._last_fair_value = prob
            self._last_update_ts = now_ts
        return prob

    def set_start_price(self, price: float):
        """Set the opening price once known."""
        if self.start_price is None and price > 0:
            self.start_price = price
            log.info("start_price_set", price=price)

    def time_remaining_seconds(self, now_ts: float = None) -> float:
        now_ts = now_ts or _time.time()
        return max(0, self.resolve_ts - now_ts)

    def normalized_time(self, now_ts: float = None) -> float:
        now_ts = now_ts or _time.time()
        total = self.resolve_ts - self.event_start_ts
        if total <= 0:
            return 0.0
        remaining = self.resolve_ts - now_ts
        return max(0.0, min(1.0, remaining / total))

    @property
    def last_fair_value(self) -> float:
        return self._last_fair_value

    @property
    def is_stale(self) -> bool:
        return (_time.time() - self._last_update_ts) > 5.0


def compute_inventory_repair_sizes(imbalance: float,
                                   min_order_size: int,
                                   max_order_size: int) -> tuple[int, int, str]:
    """Return (up_size, down_size, mode) for guarded repair quoting.

    Repair quotes only the light side. A sub-minimum tail cannot be repaired
    exactly on Polymarket because live orders must be at least min_order_size
    shares, so quote the light side at the minimum valid order size. That may
    overshoot by a few shares, but it never tops up the already-heavy side.

    Example: Down 3 / Up 0 => imbalance=-3. Quote Up 5 only; do not quote more
    Down just to make the arithmetic pretty. Pretty arithmetic leaks money.
    """
    min_order_size = max(1, int(min_order_size or 1))
    max_order_size = max(min_order_size, int(max_order_size or min_order_size))
    tail = abs(float(imbalance or 0))

    if tail <= 0:
        return 0, 0, "flat"

    if tail < min_order_size:
        # Live invariant: never top up the already-filled/heavy side. Even for
        # sub-minimum partial tails, quote only the light side at the minimum
        # valid order size. This may overshoot by a few shares, but it avoids
        # digging the imbalance deeper.
        if imbalance > 0:
            # Too many Up: quote Down only.
            return 0, min_order_size, "repair_down"
        # Too many Down: quote Up only.
        return min_order_size, 0, "repair_up"

    repair_size = min(max_order_size, int(round(tail)))
    if imbalance > 0:
        return 0, repair_size, "repair_down"
    return repair_size, 0, "repair_up"


def compute_fv_aware_dust_repair_sizes(imbalance: float,
                                       fair_value: float,
                                       min_order_size: int,
                                       max_order_size: int,
                                       neutral_band: float = 0.02) -> tuple[int, int, str]:
    """Handle sub-minimum tails with a two-step dust ladder.

    Polymarket minimum order size means a 4-share tail cannot be repaired by
    buying exactly 4 shares. For larger dust tails, quote ``tail + min_size`` on
    the light side. Example: +4 UP → buy 9 DOWN → leaves -5 DOWN, then normal
    repair buys 5 UP and lands exactly flat.

    For tiny 1-2 share dust, avoid creating a much larger temporary residual;
    hold if the tail side is not clearly disfavored by fair value.
    """
    min_order_size = max(1, int(min_order_size or 1))
    max_order_size = max(min_order_size, int(max_order_size or min_order_size))
    tail = abs(float(imbalance or 0))
    if tail <= 0:
        return 0, 0, "flat"
    if tail >= min_order_size:
        return compute_inventory_repair_sizes(imbalance, min_order_size, max_order_size)

    fv = max(0.0, min(1.0, float(fair_value or 0.5)))
    ladder_threshold = max(3, int((min_order_size + 1) // 2))
    if tail >= ladder_threshold:
        ladder_size = min(max_order_size, int(round(tail)) + min_order_size)
        if imbalance > 0:
            return 0, ladder_size, "repair_down"
        return ladder_size, 0, "repair_up"

    # Positive imbalance = extra UP. For 1-2 share dust, hold if UP is still at
    # least roughly favored; otherwise flip with the minimum DOWN clip.
    if imbalance > 0:
        if fv >= 0.5 - neutral_band:
            return 0, 0, "dust_hold_up"
        return 0, min_order_size, "repair_down"

    # Negative imbalance = extra DOWN. For 1-2 share dust, hold if DOWN is still
    # at least roughly favored; otherwise flip with the minimum UP clip.
    if fv <= 0.5 + neutral_band:
        return 0, 0, "dust_hold_down"
    return min_order_size, 0, "repair_up"


def apply_dust_price_guardrails(quotes, mode: str,
                                best_ask_yes: Optional[float] = None,
                                best_ask_no: Optional[float] = None):
    """Favor the repair side and make the dust top-up side less aggressive.

    Dust normalization is not risk-free: if only the heavy-side top-up fills,
    the bot makes the tail worse. Biasing prices makes the desired opposite-side
    fill more likely while preserving the combined-cost invariant.
    """
    if mode not in ("dust_up", "dust_down"):
        return quotes

    yes = float(quotes.yes_buy_price or 0)
    no = float(quotes.no_buy_price or 0)
    if yes <= 0 or no <= 0:
        return quotes

    if mode == "dust_up":
        # Too many Up: Down is the repair side. Pay up for Down, shade Up down.
        yes -= 0.01
        no += 0.01
    else:
        # Too many Down: Up is the repair side. Pay up for Up, shade Down down.
        yes += 0.01
        no -= 0.01

    if best_ask_yes is not None and yes >= best_ask_yes:
        yes = best_ask_yes - 0.01
    if best_ask_no is not None and no >= best_ask_no:
        no = best_ask_no - 0.01

    yes = max(0.01, min(0.99, round(yes, 2)))
    no = max(0.01, min(0.99, round(no, 2)))

    # Keep the pair edge. If the repair-side bump pushed combined cost too high,
    # lower the dust/top-up side first, because that is the dangerous fill.
    if yes + no >= 1.0:
        if mode == "dust_up":
            yes = max(0.01, round(0.99 - no, 2))
        else:
            no = max(0.01, round(0.99 - yes, 2))

    quotes.yes_buy_price = yes
    quotes.no_buy_price = no
    quotes.combined_cost = round(yes + no, 4)
    quotes.edge_per_pair = round(1.0 - quotes.combined_cost, 4)
    return quotes


def repair_price_cap(pos, side: str, size: float, fair_value: float,
                     min_edge: float = 0.01,
                     adverse_buffer: float = 0.02) -> tuple[float, str]:
    """Return the max repair bid that preserves positive pair edge.

    Repair mode must not manufacture guaranteed-loss pairs. Earlier live
    hardening allowed a wrong-way tail to be hedged up to expected value; that
    reduced naked inventory but created the exact failure Vinit observed: pair
    average cost > 1. For this strategy, if the missing side cannot be bought
    under ``1 - opposite_fill_price - min_edge``, we wait/cancel instead of
    buying a locked-in loser.
    """
    side = (side or "").lower()
    profitable_cap = float(pos.max_profitable_repair_price(side, size, min_edge=min_edge))
    return profitable_cap, "pair_edge"


def aggressive_repair_price(current_price: float | None,
                            cap: float,
                            best_ask: Optional[float] = None,
                            best_bid: Optional[float] = None) -> float | None:
    """Move repair bids as high as safely possible without crossing.

    Repair mode is not normal rebate farming. If we can complete a pair with
    combined cost < 1, sitting 5c below the market is just choosing to keep the
    naked tail. For post-only orders, the most aggressive safe bid is one tick
    below best ask, capped by the pair/risk cap.
    """
    if cap < 0.01:
        return None

    price = float(current_price or 0.01)
    target = float(cap)

    if best_ask is not None and float(best_ask or 0) > 0:
        target = min(target, float(best_ask) - 0.01)
    if best_bid is not None and float(best_bid or 0) > 0:
        # At least join the best bid when cap allows. This preserves queue
        # competitiveness if the book is wide or best_ask is unavailable.
        target = max(target, min(float(best_bid), float(cap)))

    target = max(0.01, min(0.99, target))

    # This function is also the final pair-edge safety clamp for repair mode.
    # If the current quote is above the profitable cap, keeping it resting can
    # fill a guaranteed-loss pair before the next loop. Lower it immediately.
    if price > float(cap):
        return round(target, 2)

    if target <= price:
        return round(price, 2)
    return round(target, 2)


def has_negative_matched_pair_edge(pos, tolerance: float = 0.005) -> bool:
    """True when FIFO-matched pairs have locked in negative edge.

    This is a circuit breaker, not a quoting input. A pair-matching maker whose
    matched pairs are already negative is no longer market-making; it is paying
    to recycle volume. Stop the market instead of compounding.
    """
    try:
        return float(pos.matched_pairs() or 0) > 0 and float(pos.matched_pair_profit()) < -float(tolerance)
    except Exception:
        return False


class MarketCycler:
    """
    Runs the quote loop for a single asset's 15-minute markets.
    Automatically cycles to the next market when one resolves.
    """

    def __init__(self, asset: str, asset_config: AssetConfig,
                 global_config: GlobalConfig, price_feed,
                 order_manager: OrderManager,
                 market_discovery: MarketDiscovery,
                 book_reader: OrderBookReader,
                 inventory_manager: InventoryManager,
                 risk_engine: RiskEngine,
                 pnl_tracker: PnLTracker,
                 regime_config=None,
                 toxicity_config=None,
                 portfolio_pnl_getter=None,
                 dashboard_callback=None,
                 ctf_ops: Optional[CTFOperations] = None,
                 gasless_merger: Optional[GaslessMerger] = None,
                 balance_monitor: Optional[BalanceMonitor] = None):

        self.asset = asset
        self.ac = asset_config
        self.gc = global_config
        self.price_feed = price_feed
        self.order_mgr = order_manager
        self.discovery = market_discovery
        self.book_reader = book_reader
        self.inventory = inventory_manager
        self.risk_engine = risk_engine
        self.pnl = pnl_tracker
        self.regime_config = regime_config
        self.toxicity_config = toxicity_config
        self.portfolio_pnl_getter = portfolio_pnl_getter
        self._dashboard_cb = dashboard_callback
        self.ctf: Optional[CTFOperations] = ctf_ops
        self.gasless_merger: Optional[GaslessMerger] = gasless_merger
        self.balance_monitor: Optional[BalanceMonitor] = balance_monitor
        
        # Merge threshold: auto-merge when locked capital exceeds this
        self._merge_dollar_threshold = 15.0  # dollars

        # Per-market components (recreated each cycle)
        self.current_market: Optional[MarketInfo] = None
        self.fair_value_model: Optional[UpDownFairValue] = None
        self.vol_estimator = VolatilityEstimator(
            lookback_seconds=global_config.vol_lookback_seconds,
            default_sigma=asset_config.default_sigma,
        )
        self.quote_engine = QuoteEngine(
            gamma=asset_config.gamma,
            min_spread=asset_config.min_spread,
            max_spread=asset_config.max_spread,
            max_order_size=asset_config.max_order_size,
        )
        regime_lookback = getattr(regime_config, "lookback", 30)
        regime_trend = getattr(regime_config, "trend_threshold", 0.08)
        regime_spike = getattr(regime_config, "spike_threshold", 0.20)
        tox_edge_window = getattr(toxicity_config, "edge_window", 30)
        tox_window = getattr(toxicity_config, "window_seconds", 300)
        tox_threshold = getattr(toxicity_config, "threshold", 0.002)
        tox_edge_adverse_rate = getattr(toxicity_config, "edge_adverse_rate", 0.85)
        tox_edge_mean_threshold = getattr(toxicity_config, "edge_mean_threshold", 0.015)
        tox_min_fills_for_halt = getattr(toxicity_config, "min_fills_for_halt", 8)
        tox_one_sided_fill_limit = getattr(toxicity_config, "one_sided_fill_limit", 8)
        tox_immediate_drift_threshold = getattr(toxicity_config, "immediate_drift_threshold", 0.02)
        tox_halt_cooldown = getattr(toxicity_config, "halt_cooldown", 90)
        self.regime_filter = RegimeFilter(
            lookback=regime_lookback,
            trend_threshold=regime_trend,
            spike_threshold=regime_spike,
        )
        self.edge_tracker = FillEdgeTracker(window=tox_edge_window)
        self.toxicity_monitor = ToxicityMonitor(
            window_seconds=tox_window,
            threshold=tox_threshold,
            halt_cooldown=tox_halt_cooldown,
            edge_adverse_rate=tox_edge_adverse_rate,
            edge_mean_threshold=tox_edge_mean_threshold,
            min_fills_for_halt=tox_min_fills_for_halt,
            one_sided_fill_limit=tox_one_sided_fill_limit,
            immediate_drift_threshold=tox_immediate_drift_threshold,
        )
        self.last_fair_value: Optional[float] = None
        self.start_price_source: str = "unknown"
        self._last_vatic_retry_ts: float = 0.0
        self.stop_reason: str | None = None
        self._last_close_only_repair_mode: str | None = None
        self._last_toxicity_repair_override_log: float = 0.0
        self._merge_unavailable_until: float = 0.0

        self._running = False
        self._last_market_slug = None  # Track to detect new market
        self._quote_event = asyncio.Event()

    def notify_price_update(self):
        """Wake the quote loop on a fresh price tick, with rate limit in loop."""
        if self._running and self.current_market:
            self._quote_event.set()

    async def run(self):
        """Main loop: cycle through markets continuously."""
        self._running = True
        self.stop_reason = None
        log.info("cycler_started", asset=self.asset)

        # Dry-run: if the previous process stopped before Gamma recorded resolution,
        # we may have unresolved windows persisted in state. Resume those checks.
        try:
            sm = getattr(self.inventory, "state_manager", None)
            pending = (sm.state.get("pending_resolutions", []) if sm and hasattr(sm, "state") else [])
            for e in pending:
                if e.get("asset") != self.asset:
                    continue
                slug = e.get("slug")
                window_start_ts = e.get("window_start_ts")
                market_id = e.get("market_id")
                if slug and window_start_ts and market_id:
                    asyncio.create_task(self._wait_and_settle_unmatched_by_fields(
                        asset=e.get("asset"),
                        slug=slug,
                        window_start_ts=int(window_start_ts),
                        market_id=market_id,
                        pos_snapshot={
                            "yes_avg_entry": float(e.get("yes_avg_entry", 0.0) or 0.0),
                            "no_avg_entry": float(e.get("no_avg_entry", 0.0) or 0.0),
                            "unmatched_up": float(e.get("unmatched_up", 0.0) or 0.0),
                            "unmatched_down": float(e.get("unmatched_down", 0.0) or 0.0),
                        },
                    ))
        except Exception as ex:
            # Never block the cycler on pending-resolution bookkeeping.
            log.debug("pending_resolution_bootstrap_failed", error=str(ex))

        while self._running:
            try:
                # 1. Discover next market
                market = await self._find_next_market()
                if not market:
                    self._update_dashboard_waiting()
                    await asyncio.sleep(5)
                    continue

                # Skip if same market as before (already being traded)
                if market.slug == self._last_market_slug:
                    log.warning("resuming_market_after_error", asset=self.asset, slug=market.slug)
                    await self._run_market(market)
                    continue

                # 2. New market found — settle old, prepare new
                if self._last_market_slug:
                    await self._settle_market()

                self._last_market_slug = market.slug
                self.current_market = market
                self.pnl.markets_traded += 1

                log.info("market_started",
                         asset=self.asset,
                         slug=market.slug,
                         question=market.question,
                         remaining=f"{market.time_remaining:.0f}s")

                # 3. Run the quote loop for this market
                await self._run_market(market)

                # 4. Market ended — settle. If the bot is being stopped before
                # expiry (Ctrl+C/timeout/SIGTERM), do NOT treat the current
                # inventory as final resolution tail. Just cancel quotes and
                # leave accounting untouched; otherwise test harness timeouts
                # pollute outcome/PnL with mid-window inventory.
                if market.time_remaining > 0:
                    log.info("market_interrupted_before_expiry",
                             asset=self.asset,
                             slug=market.slug,
                             remaining_s=round(market.time_remaining, 1))
                    await self.order_mgr.cancel_market_quotes(market.market_id)
                    break

                # Market actually expired — settle
                await self._settle_market()

                # 5. Wait for current window to expire, then look for next
                wait_time = max(0, market.resolve_ts - _time.time()) + 2
                if wait_time > 0:
                    log.info("waiting_for_next_window",
                             asset=self.asset, wait_s=round(wait_time, 1))
                    self._update_dashboard_waiting()
                    await asyncio.sleep(wait_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("cycler_error", asset=self.asset, error=str(e))
                await self.order_mgr.cancel_market_quotes(
                    self.current_market.market_id if self.current_market else "")
                await asyncio.sleep(5)

        log.info("cycler_stopped", asset=self.asset)

    async def _settle_market(self):
        """Clean up after a market expires, merge pairs and redeem winnings."""
        if self.current_market:
            market = self.current_market
            await self.order_mgr.cancel_market_quotes(market.market_id)

            # Get final position
            pos = self.inventory.get_or_create(market.market_id, self.asset)
            pairs = pos.matched_pairs()

            log.info("market_settling",
                     asset=self.asset,
                     slug=market.slug,
                     up_shares=pos.yes_shares,
                     down_shares=pos.no_shares,
                     matched_pairs=pairs)

            # --- CTF Operations ---
            if pairs > 0:
                # Use gasless merger if available, else on-chain
                condition_id = getattr(market, 'condition_id', None)
                if condition_id:
                    amount = int(pairs * 1e6)  # Convert to USDC base units
                    tx = None
                    collateral_token = getattr(self.gasless_merger, "_collateral_token", "")
                    if self.balance_monitor and getattr(self.balance_monitor, "_ctf", None):
                        collateral_token = infer_collateral_token_for_market(
                            self.balance_monitor._w3,
                            self.balance_monitor._ctf,
                            condition_id,
                            getattr(market, "token_id_up", ""),
                            getattr(market, "token_id_down", ""),
                            collateral_token,
                        )

                    # Prefer gasless merge
                    if self.gasless_merger and self.gasless_merger.is_available:
                        tx = await self.gasless_merger.merge_positions(
                            condition_id, amount, collateral_token=collateral_token
                        )

                    # Fallback to on-chain
                    if not tx and self.ctf:
                        tx = await self.ctf.merge_positions(
                            condition_id, amount, collateral_token=collateral_token
                        )

                    if tx:
                        # ERC20 approvals are permanent (MAX_UINT256) and set at
                        # startup. Post-merge we only need to sync the CLOB's
                        # indexed view so the returned USDC.e is credited as cash.
                        sync_balance = getattr(self.order_mgr.executor, "sync_balance_allowance", None)
                        if callable(sync_balance):
                            # Wait for CLOB indexer to catch up with on-chain
                            # merge state before syncing balance/allowance.
                            await asyncio.sleep(3)
                            sync_ok = False
                            for attempt in range(1, 6):
                                try:
                                    sync_ok = bool(await sync_balance())
                                except Exception as e:
                                    log.warning("post_settle_merge_balance_sync_error",
                                                asset=self.asset, attempt=attempt, error=str(e))
                                    sync_ok = False
                                if sync_ok:
                                    break
                                await asyncio.sleep(min(2 * attempt, 8))
                            if sync_ok:
                                log.info("post_settle_merge_balance_allowance_synced", asset=self.asset)
                            else:
                                log.warning("post_settle_merge_balance_allowance_sync_failed", asset=self.asset)
                        pair_profit = pos.matched_pair_profit()
                        self.pnl.record_settlement(pair_profit, market.market_id)
                        self.pnl.record_capital_recovery(pairs * 1.0)
                        pos.acknowledge_settlement()
                        log.info("pairs_merged",
                                 pairs=pairs,
                                 profit=f"${pair_profit:.4f}",
                                 tx=str(tx)[:16] if tx else "none")

            # Try to redeem any remaining tokens (if market resolved)
            if self.ctf or self.gasless_merger:
                condition_id = getattr(market, 'condition_id', None)
                if condition_id:
                    resolved = await self.ctf.is_market_resolved(condition_id) if self.ctf else False
                    if resolved:
                        tx = None
                        if self.gasless_merger and self.gasless_merger.is_available:
                            tx = await self.gasless_merger.redeem_positions(condition_id)
                        elif self.ctf:
                            log.error("gasless_redeem_unavailable",
                                      msg="Gasless redeem unavailable; on-chain fallback disabled by policy")
                        if tx:
                            # Calculate redemption value for unmatched tokens
                            unmatched_up = pos.yes_shares - pairs
                            unmatched_down = pos.no_shares - pairs
                            log.info("tokens_redeemed",
                                     unmatched_up=unmatched_up,
                                     unmatched_down=unmatched_down,
                                     tx=tx[:16] if tx else "none")

            # Simulate redemption of unmatched tokens in Dry-Run
            elif not self.ctf and not self.gasless_merger:
                if pairs > 0:
                    pair_profit = pos.matched_pair_profit()
                    self.pnl.record_settlement(pair_profit, market.market_id)
                    self.pnl.record_capital_recovery(pairs * 1.0)
                    pos.acknowledge_settlement()
                    log.info("dry_run_pairs_merged",
                             pairs=pairs,
                             profit=f"${pair_profit:.4f}")

                unmatched_up = pos.yes_shares - pairs
                unmatched_down = pos.no_shares - pairs

                # Always track/record the real outcome (even if flat).
                # This is useful for analyzing market behavior and verifying the model.
                pos_snapshot = {
                    "yes_avg_entry": pos.yes_avg_entry,
                    "no_avg_entry": pos.no_avg_entry,
                    "unmatched_up": unmatched_up,
                    "unmatched_down": unmatched_down,
                }

                # Persist a pending resolution record so the next run can finish it even if
                # this process exits (timeout/restart).
                sm = getattr(self.inventory, "state_manager", None)
                if sm:
                    try:
                        sm.add_pending_resolution({
                            "slug": market.slug,
                            "asset": market.asset,
                            "window_start_ts": int(market.window_start_ts),
                            "market_id": market.market_id,
                            "yes_avg_entry": pos_snapshot["yes_avg_entry"],
                            "no_avg_entry": pos_snapshot["no_avg_entry"],
                            "unmatched_up": pos_snapshot["unmatched_up"],
                            "unmatched_down": pos_snapshot["unmatched_down"],
                            "created_ts": _time.time(),
                        })
                    except Exception as ex:
                        log.debug("pending_resolution_persist_failed", slug=market.slug, error=str(ex))

                # Kick off background task to wait for actual resolution from Gamma API
                asyncio.create_task(self._wait_and_settle_unmatched(market, pos_snapshot))

            # Clear position from inventory state
            self.inventory.clear_market(market.market_id)
            
            self.current_market = None

        # Reset per-market state for next cycle
        self.quote_engine.reset_params()
        if not self.portfolio_pnl_getter:
            self.risk_engine.reset_for_new_market(self.pnl.net_pnl)

    async def _wait_and_settle_unmatched(self, market: MarketInfo, pos_snapshot: dict):
        """Background task to poll Gamma API and wait for actual market resolution.
        
        Args:
            market: MarketInfo for the expired market.
            pos_snapshot: Frozen dict with keys: yes_avg_entry, no_avg_entry,
                          unmatched_up, unmatched_down.
        """
        unmatched_up = pos_snapshot["unmatched_up"]
        unmatched_down = pos_snapshot["unmatched_down"]
        yes_avg = pos_snapshot["yes_avg_entry"]
        no_avg = pos_snapshot["no_avg_entry"]
        
        await self._wait_and_settle_unmatched_by_fields(
            asset=market.asset,
            slug=market.slug,
            window_start_ts=int(market.window_start_ts),
            market_id=market.market_id,
            pos_snapshot=pos_snapshot,
        )

    async def _wait_and_settle_unmatched_by_fields(self, asset: str, slug: str,
                                                   window_start_ts: int,
                                                   market_id: str,
                                                   pos_snapshot: dict):
        """Poll Gamma until the market is inactive, then record outcome.

        NOTE: We require m.active == False to avoid false positives.
        """
        unmatched_up = pos_snapshot["unmatched_up"]
        unmatched_down = pos_snapshot["unmatched_down"]
        yes_avg = pos_snapshot["yes_avg_entry"]
        no_avg = pos_snapshot["no_avg_entry"]

        log.info("waiting_for_actual_resolution", slug=slug)

        while self._running:
            await asyncio.sleep(30)
            try:
                m = await self.discovery._fetch_market(asset, int(window_start_ts))
                if not m:
                    continue

                # Require actual Gamma closed/inactive/archived status to prevent
                # volatility false positives while still supporting markets that
                # remain active=True after close.
                resolved = bool(getattr(m, "closed", False) or getattr(m, "archived", False) or not m.active)
                if not resolved:
                    continue

                up = m.up_price
                down = m.down_price
                won_up = up >= down

                winning_shares = unmatched_up if won_up else unmatched_down
                losing_shares = unmatched_down if won_up else unmatched_up
                winner_str = "UP" if won_up else "DOWN"

                cost_of_winning = winning_shares * (yes_avg if won_up else no_avg)
                cost_of_losing = losing_shares * (no_avg if won_up else yes_avg)

                revenue = winning_shares * 1.0
                net_profit = revenue - cost_of_winning - cost_of_losing

                self.pnl.record_outcome_resolution(net_profit, market_id)
                self.pnl.record_capital_recovery(revenue)

                log.info(
                    "dry_run_actual_resolution",
                    slug=slug,
                    winner=winner_str,
                    winning_shares=winning_shares,
                    losing_shares=losing_shares,
                    outcome_pnl=round(net_profit, 4),
                    pnl=f"${net_profit:.4f}",
                )

                sm = getattr(self.inventory, "state_manager", None)
                if sm:
                    try:
                        sm.remove_pending_resolution(slug)
                    except Exception as ex:
                        log.debug("pending_resolution_remove_failed", slug=slug, error=str(ex))
                break
            except Exception as e:
                log.error("wait_and_settle_error", slug=slug, error=str(e))

    async def _find_next_market(self) -> Optional[MarketInfo]:
        """Find the next eligible market for this asset."""
        # Start/resume the actual current window until the configured dead zone.
        # The previous +30s buffer made mid-window restarts silently skip to the
        # next 15m market while the user was looking at the still-live current
        # Polymarket UI, making FV appear wildly wrong.
        market = await self.discovery.discover_single(
            self.asset,
            min_remaining=self.gc.stop_quoting_seconds
        )
        if not market:
            # Try with lower threshold — maybe market just opened
            market = await self.discovery.discover_single(
                self.asset, min_remaining=60
            )
        return market

    def _calibrate_strike_from_market(self, market: MarketInfo,
                                       current_spot: float,
                                       sigma: float,
                                       p_up_override: float = None) -> Optional[float]:
        """
        Reverse-engineer the 'price to beat' from Polymarket's order book.

        The market participants (including professional MMs) are pricing off
        the REAL Chainlink Data Streams price. Their Up/Down prices encode
        the correct strike. We invert our Black-Scholes model to extract it:

          P(Up) = Φ(log(S/K) / (σ√T))
          K = S / exp(Φ⁻¹(P_up) * σ√T)

        This gives us the exact price to beat without needing Chainlink
        Data Streams access ($20-30 more accurate than Binance candle).
        """
        from scipy.stats import norm
        import math

        # Use override (from fresh CLOB book) or fallback to Gamma API
        p_up = p_up_override if p_up_override is not None else market.market_mid_up

        # Sanity: if the market is at extreme prices or illiquid, skip
        if p_up < 0.03 or p_up > 0.97:
            log.warning("market_calibration_skip",
                        reason="extreme_price", p_up=p_up)
            return None

        if current_spot is None or current_spot <= 0:
            return None

        t_remaining = market.time_remaining
        t_years = t_remaining / (365.25 * 86400)
        vol_sqrt_t = sigma * math.sqrt(t_years)

        if vol_sqrt_t < 1e-10:
            return current_spot  # Near expiry, can't distinguish

        # Invert: K = S / exp(Φ⁻¹(P_up) * σ√T)
        z = norm.ppf(p_up)
        K = current_spot / math.exp(z * vol_sqrt_t)

        # Sanity: K should be within ~1% of spot for 15-min markets
        pct_diff = abs(K - current_spot) / current_spot
        if pct_diff > 0.01:
            log.warning("market_calibration_suspicious",
                        K=round(K, 2), spot=round(current_spot, 2),
                        pct_diff=f"{pct_diff:.4%}", p_up=p_up)
            # Still return it — the market knows the price to beat,
            # even if spot has moved a lot since window open

        return round(K, 2)

    async def _run_market(self, market: MarketInfo):
        """Run the quote loop for a single 15-minute market."""
        self._has_done_30s_merge = False
        self._repair_mode_started_at = None
        start_price = None
        start_price_source = "unknown"
        self.start_price_source = "unknown"
        self._last_vatic_retry_ts = 0.0
        binance_start_price = None

        log.info("initializing_new_market", asset=self.asset, slug=market.slug)
        spot = getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0)
        self._update_dashboard(market, spot, 0, 0, "INITIALIZING", market.time_remaining)

        # 1. PRIMARY: Exact price-to-beat from Vatic API (Chainlink)
        start_price = await self.price_feed.fetch_vatic_strike(
            self.ac.symbol, market.event_start_ts
        )
        if start_price:
            start_price_source = "vatic"
            log.info("start_price_from_vatic",
                     asset=self.asset, price=start_price)

        # 2. Always fetch Binance kline close at window start to calculate the spread
        binance_start_price = await self.price_feed.fetch_historical_price(
            self.ac.symbol, market.event_start_ts
        )
        
        # 3. If Vatic failed, try to calibrate from the Polymarket Orderbook
        if not start_price and binance_start_price:
            raw_spot = self.price_feed.get_price(self.ac.symbol)
            if raw_spot:
                self.vol_estimator.update(raw_spot, _time.time())
                sigma = self.vol_estimator.sigma_for_model()
                calibrated = self._calibrate_strike_from_market(market, raw_spot, sigma)
                if calibrated:
                    start_price = calibrated
                    start_price_source = "market_calibration"
                    log.info("start_price_from_calibration",
                             asset=self.asset, price=start_price)

        # 4. Fallback: Chainlink on-chain aggregator before Binance. Binance is
        # not the Polymarket price-to-beat source; only use it as a last resort.
        if not start_price:
            start_price = await self.price_feed.fetch_chainlink_price(
                self.ac.symbol, market.event_start_ts
            )
            if start_price:
                start_price_source = "chainlink"
                log.info("start_price_from_chainlink",
                         asset=self.asset, price=start_price)

        # 5. Last-resort fallback: Binance. This is non-authoritative and will
        # be replaced by Vatic in quote cycles as soon as Vatic is available.
        if binance_start_price and not start_price:
            start_price = binance_start_price
            start_price_source = "binance"
            log.warning("start_price_from_binance_non_authoritative",
                        asset=self.asset, price=start_price)
            
        # Do NOT apply a fixed start-time Vatic/Chainlink-vs-Binance basis to
        # live Binance spot. The oracle target can differ sharply from Binance's
        # exact window-open print because of provider timing/lag, and carrying
        # that one-time basis forward inverted FV in live windows (e.g. target
        # 73376, Binance open 73488, live Binance 73374 became false live spot
        # 73263). Use Vatic/Chainlink only as the price-to-beat; use raw live
        # Binance as the best estimate of the final price path.
        self.chainlink_spread = 0
        if start_price and binance_start_price and start_price != binance_start_price:
            log.info("start_price_basis_observed_not_applied",
                     asset=self.asset,
                     start_price=round(start_price, 4),
                     binance_start_price=round(binance_start_price, 4),
                     basis=round(start_price - binance_start_price, 4))

        raw_binance_spot = self.price_feed.get_price(self.ac.symbol)
        current_spot = raw_binance_spot if raw_binance_spot else None
        if not current_spot and hasattr(self.price_feed, "fetch_price_rest"):
            current_spot = await self.price_feed.fetch_price_rest(
                self.ac.symbol,
                getattr(self.price_feed, "rest_url", "https://api.binance.com/api/v3"),
            )

        # Validate only fallback/calibrated strikes against live Polymarket
        # books. Vatic is the strike/price-to-beat source of truth for the
        # dashboard; do not replace it with a market-calibrated value, or the
        # displayed price-to-beat can drift away from the actual strike.
        if start_price and current_spot and start_price_source != "vatic":
            try:
                books = await self.book_reader.get_books([market.token_id_up, market.token_id_down])
                market_mid = polymarket_implied_up_mid(
                    books.get(market.token_id_up),
                    books.get(market.token_id_down),
                )
                self.vol_estimator.update(current_spot, _time.time())
                sigma = self.vol_estimator.sigma_for_model()
                if start_price_disagrees_with_market(
                    start_price,
                    current_spot,
                    sigma,
                    market.event_start_ts,
                    market.resolve_ts,
                    market_mid,
                    now_ts=_time.time(),
                ):
                    calibrated = self._calibrate_strike_from_market(
                        market, current_spot, sigma, p_up_override=market_mid)
                    if calibrated:
                        log.warning(
                            "start_price_replaced_by_market_calibration",
                            asset=self.asset,
                            old_start=round(start_price, 4),
                            calibrated=round(calibrated, 4),
                            current_spot=round(current_spot, 4),
                            market_fv=(round(market_mid, 4) if market_mid is not None else None),
                        )
                        start_price = calibrated
            except Exception as e:
                log.warning("start_price_validation_failed", asset=self.asset, error=str(e))

        # 6. Last resort: Current spot
        if not start_price:
            elapsed = _time.time() - market.event_start_ts
            start_price = current_spot
            start_price_source = "spot"
            if elapsed < 30:
                log.info("start_price_from_spot",
                         asset=self.asset, reason="window_just_opened")
            else:
                log.warning("start_price_from_spot",
                            asset=self.asset,
                            reason="all_sources_failed",
                            elapsed_s=round(elapsed))

        self.start_price_source = start_price_source
        log.info("market_start_price",
                 asset=self.asset,
                 start_price=start_price,
                 current_spot=current_spot,
                 window_start_ts=market.event_start_ts,
                 source=start_price_source)

        self.fair_value_model = UpDownFairValue(
            event_start_ts=market.event_start_ts,
            resolve_ts=market.resolve_ts,
            start_price=start_price,
        )

        # Reset per-market state
        regime_lookback = getattr(self.regime_config, "lookback", 30)
        regime_trend = getattr(self.regime_config, "trend_threshold", 0.08)
        regime_spike = getattr(self.regime_config, "spike_threshold", 0.20)
        tox_edge_window = getattr(self.toxicity_config, "edge_window", 30)
        tox_window = getattr(self.toxicity_config, "window_seconds", 300)
        tox_threshold = getattr(self.toxicity_config, "threshold", 0.002)
        tox_edge_adverse_rate = getattr(self.toxicity_config, "edge_adverse_rate", 0.85)
        tox_edge_mean_threshold = getattr(self.toxicity_config, "edge_mean_threshold", 0.015)
        tox_min_fills_for_halt = getattr(self.toxicity_config, "min_fills_for_halt", 8)
        tox_one_sided_fill_limit = getattr(self.toxicity_config, "one_sided_fill_limit", 8)
        tox_immediate_drift_threshold = getattr(self.toxicity_config, "immediate_drift_threshold", 0.02)
        tox_halt_cooldown = getattr(self.toxicity_config, "halt_cooldown", 90)
        self.regime_filter = RegimeFilter(
            lookback=regime_lookback,
            trend_threshold=regime_trend,
            spike_threshold=regime_spike,
        )
        self.edge_tracker = FillEdgeTracker(window=tox_edge_window)
        self.toxicity_monitor = ToxicityMonitor(
            window_seconds=tox_window,
            threshold=tox_threshold,
            halt_cooldown=tox_halt_cooldown,
            edge_adverse_rate=tox_edge_adverse_rate,
            edge_mean_threshold=tox_edge_mean_threshold,
            min_fills_for_halt=tox_min_fills_for_halt,
            one_sided_fill_limit=tox_one_sided_fill_limit,
            immediate_drift_threshold=tox_immediate_drift_threshold,
        )
        self.quote_engine.reset_params()

        while self._running:
            remaining = market.time_remaining
            if remaining <= 0:
                log.info("market_expired", asset=self.asset, slug=market.slug)
                break  # Market resolved

            # Check if cooldown expired for risk halts
            if self.risk_engine.halted:
                self.risk_engine.check_stops(self.pnl.net_pnl)

            cycle_start = _time.time()
            try:
                await self._quote_cycle(market)
            except Exception as e:
                log.error("quote_cycle_error", error=str(e),
                          traceback=traceback.format_exc(),
                          asset=self.asset)
                # Live safety: any exception after/around order placement must
                # fail closed. Continuing can stack new quotes while stale ones
                # remain live, which is unacceptable with real funds.
                await self.order_mgr.cancel_all()
                self.stop_reason = f"quote_cycle_error: {type(e).__name__}: {e}"
                self._running = False
                return

            # Event-driven wakeup: quote quickly on Binance price ticks, but keep
            # a hard minimum interval to avoid cancel/repost churn and API spam.
            min_interval = max(0.05, float(getattr(self.gc, "min_quote_interval", 0.25)))
            elapsed = _time.time() - cycle_start
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)

            if self._quote_event.is_set():
                self._quote_event.clear()
                continue

            self._quote_event.clear()
            try:
                await asyncio.wait_for(
                    self._quote_event.wait(),
                    timeout=float(self.gc.refresh_interval),
                )
            except asyncio.TimeoutError:
                pass

    async def _handle_standardized_fills(self, market: MarketInfo,
                                         fills: list[dict], fv: float,
                                         pos) -> bool:
        """Record fills and fail closed by removing all resting market quotes.

        A live fill changes the inventory state immediately. If we leave any
        stale quote resting — especially another order on the side that just
        filled — the bot can keep buying the heavy side while the dashboard only
        shows the last synced +5 tail. After any fill, flatten the quote surface;
        the next cycle will rebuild from updated inventory and quote only the
        light side when repair is needed.
        """
        saw_fill = False
        for fill in fills:
            saw_fill = True
            self.inventory.record_fill(
                market.market_id, fill["side"],
                fill["size"], fill["price"], self.asset
            )
            self.pnl.record_fill(
                size=fill["size"],
                price=fill["price"],
                side=fill["side"],
                asset=self.asset,
                market_id=market.market_id,
            )
            self.edge_tracker.record_fill(fill["side"], fill["price"], fv)
            toxicity_monitor = getattr(self, "toxicity_monitor", None)
            if toxicity_monitor:
                toxicity_monitor.record_fill(
                    fill["side"], fill["price"], fill["size"], fv
                )

            # Keep local ActiveQuotes in sync with the fill before canceling the
            # rest. Fully filled orders may already be gone exchange-side, so do
            # not try to cancel an order id that we know was consumed.
            active = self.order_mgr.get_active(market.market_id)
            fill_order_id = str(fill.get("order_id") or "")
            fill_size = float(fill.get("size") or 0)
            if fill["side"] in ("yes", "up") and active.yes_order_id == fill_order_id:
                active.yes_size = max(0, float(active.yes_size or 0) - fill_size)
                if active.yes_size <= 0.0001:
                    active.yes_order_id = None
                    active.yes_price = None
                    active.yes_size = 0
            elif fill["side"] in ("no", "down") and active.no_order_id == fill_order_id:
                active.no_size = max(0, float(active.no_size or 0) - fill_size)
                if active.no_size <= 0.0001:
                    active.no_order_id = None
                    active.no_price = None
                    active.no_size = 0

        if saw_fill:
            # Keep wallet/dashboard capital close to reality after live fills.
            # The normal balance monitor interval can be too slow during a fast
            # pile-up, making the bot size from stale USDC.
            balance_monitor = getattr(self, "balance_monitor", None)
            if balance_monitor and hasattr(balance_monitor, "get_usdc_balance"):
                try:
                    await balance_monitor.get_usdc_balance()
                except Exception as e:
                    log.warning("post_fill_balance_refresh_failed", error=str(e))

            if not await self.order_mgr.cancel_market_quotes(market.market_id):
                self.stop_reason = "fill_reactive_cancel_market_failed"
                log.error("fill_reactive_cancel_market_failed",
                          market=market.market_id[:8],
                          imbalance=round(pos.share_imbalance(), 4))
                self._running = False
                return False
            self._last_close_only_repair_mode = None
            log.warning("fill_reactive_cancelled_market_quotes",
                        market=market.market_id[:8],
                        fills=len(fills),
                        imbalance=round(pos.share_imbalance(), 4),
                        up_shares=round(pos.yes_shares, 4),
                        down_shares=round(pos.no_shares, 4))
        return True

    async def _sync_live_fills_before_quote(self, market: MarketInfo,
                                            fv: float, pos) -> bool:
        """Pull live CLOB fills before computing quotes.

        This is the key live-vs-dry correction. Live fills can occur between
        quote cycles; if we quote before ingesting them, sizing/repair mode is
        computed from stale inventory and the bot can stack the wrong side.
        """
        if not hasattr(self.order_mgr.executor, 'get_fills'):
            return True

        try:
            raw_fills = await self.order_mgr.executor.get_fills(
                market.market_id, force=True
            )
            fills = self.order_mgr.executor.process_fills(
                raw_fills,
                self.inventory,
                market.market_id,
                token_id_to_side={
                    str(market.token_id_up): "yes",
                    str(market.token_id_down): "no",
                },
            )
            if fills:
                log.warning(
                    "pre_quote_live_fills_synced",
                    market=market.market_id[:8],
                    fills=len(fills),
                    imbalance_before=round(pos.share_imbalance(), 4),
                )
                return await self._handle_standardized_fills(market, fills, fv, pos)
            return True
        except TypeError:
            # Compatibility with tests/older wrappers that do not accept force.
            try:
                raw_fills = await self.order_mgr.executor.get_fills(market.market_id)
                fills = self.order_mgr.executor.process_fills(
                    raw_fills,
                    self.inventory,
                    market.market_id,
                    token_id_to_side={
                        str(market.token_id_up): "yes",
                        str(market.token_id_down): "no",
                    },
                )
                return await self._handle_standardized_fills(market, fills, fv, pos)
            except Exception as e:
                log.error("pre_quote_live_fill_sync_error", error=str(e))
                return False
        except Exception as e:
            log.error("pre_quote_live_fill_sync_error", error=str(e))
            return False

    async def _quote_cycle(self, market: MarketInfo):
        """Single quote cycle iteration."""
        now = _time.time()
        remaining = market.time_remaining

        # 1. Get live spot price (shifted to Chainlink estimate)
        raw_spot = self.price_feed.get_price(self.ac.symbol)
        price_age = self.price_feed.get_price_age(self.ac.symbol)

        # Binance websocket stalls are especially toxic for 15m binaries: a
        # frozen spot produces a frozen fair value while the market keeps
        # moving. Try one REST refresh before failing closed/canceling quotes.
        if (not raw_spot) or price_age > MAX_SPOT_PRICE_AGE_SECONDS:
            rest_url = getattr(self.price_feed, "rest_url", "https://api.binance.com/api/v3")
            rest_spot = await self.price_feed.fetch_price_rest(self.ac.symbol, rest_url)
            if rest_spot:
                log.warning(
                    "spot_price_rest_fallback",
                    asset=self.asset,
                    symbol=self.ac.symbol,
                    previous_raw_spot=(round(raw_spot, 4) if raw_spot else None),
                    previous_price_age=(round(price_age, 3) if price_age != float('inf') else "inf"),
                    rest_spot=round(rest_spot, 4),
                )
                raw_spot = rest_spot
                price_age = self.price_feed.get_price_age(self.ac.symbol)

        if not raw_spot:
            log.warning("no_spot_price", symbol=self.ac.symbol)
            await self.order_mgr.cancel_market_quotes(market.market_id)
            return

        if price_age > MAX_SPOT_PRICE_AGE_SECONDS:
            log.warning(
                "spot_price_stale_stop_quoting",
                asset=self.asset,
                symbol=self.ac.symbol,
                raw_binance_spot=round(raw_spot, 4),
                price_age=round(price_age, 3),
                max_age=MAX_SPOT_PRICE_AGE_SECONDS,
            )
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(
                market,
                raw_spot,
                self.last_fair_value or 0,
                0,
                "STALE_SPOT",
                remaining,
            )
            return
            
        spot = raw_spot
        log.info(
            "spot_feed_snapshot",
            asset=self.asset,
            symbol=self.ac.symbol,
            raw_binance_spot=round(raw_spot, 4),
            live_spot=round(spot, 4),
            spread=round(self.chainlink_spread, 4),
            price_age=round(price_age, 3),
            price_source=(self.price_feed.get_price_source(self.ac.symbol)
                          if hasattr(self.price_feed, "get_price_source") else "unknown"),
        )

        # Vatic is authoritative for the dashboard price-to-beat. If startup had
        # to fall back to Binance/spot because Vatic was temporarily unavailable,
        # keep retrying and replace the displayed/model strike as soon as Vatic
        # responds.
        if (self.fair_value_model
                and getattr(self, "start_price_source", "unknown") != "vatic"
                and now - getattr(self, "_last_vatic_retry_ts", 0.0) >= 5.0):
            self._last_vatic_retry_ts = now
            try:
                vatic_price = await self.price_feed.fetch_vatic_strike(
                    self.ac.symbol, market.event_start_ts)
                if vatic_price:
                    old_start = self.fair_value_model.start_price
                    self.fair_value_model.start_price = vatic_price
                    self.start_price_source = "vatic"
                    log.warning(
                        "start_price_corrected_to_vatic",
                        asset=self.asset,
                        old_start=round(float(old_start or 0), 4),
                        vatic=round(float(vatic_price), 4),
                    )
            except Exception as e:
                log.debug("vatic_retry_failed", asset=self.asset, error=str(e))

        # Set start price if not yet captured
        if self.fair_value_model and not self.fair_value_model.start_price:
            self.fair_value_model.set_start_price(spot)

        # 2. Update volatility
        self.vol_estimator.update(spot, now)
        sigma = self.vol_estimator.sigma_for_model()

        t_norm = self.fair_value_model.normalized_time(now)
        total_window = max(1.0, self.fair_value_model.resolve_ts - self.fair_value_model.event_start_ts)
        elapsed_fraction = max(0.0, min(1.0, (now - self.fair_value_model.event_start_ts) / total_window))

        # Fetch Polymarket books early so every dashboard/early-return path uses
        # the same authoritative blended FV. Previously, early returns displayed
        # raw/model FV while the UI/book price was already far away (e.g. UP 15c
        # but dashboard stuck near 54c).
        books = await self.book_reader.get_books([market.token_id_up, market.token_id_down])
        book_up = books.get(market.token_id_up)
        book_down = books.get(market.token_id_down)
        best_ask_yes = book_up.best_ask if book_up else None
        best_bid_yes = book_up.best_bid if book_up else None
        best_ask_no = book_down.best_ask if book_down else None
        best_bid_no = book_down.best_bid if book_down else None
        polymarket_mid_up = polymarket_implied_up_mid(book_up, book_down)

        # Dynamic live oracle/Polymarket spot estimate. Polymarket's displayed
        # spot can run consistently away from Binance by $100+; raw Binance then
        # biases FV. Invert the market-implied UP probability into a spot and use
        # it as the adjusted live spot when the inferred basis is plausible.
        market_implied_spot = spot_from_binary_probability(
            self.fair_value_model.start_price,
            polymarket_mid_up,
            sigma,
            remaining,
        )
        if market_implied_spot and abs(market_implied_spot - raw_spot) <= 300:
            old_spot = spot
            spot = market_implied_spot
            self.chainlink_spread = spot - raw_spot
            log.info(
                "live_spot_adjusted_from_market",
                asset=self.asset,
                raw_binance_spot=round(raw_spot, 4),
                adjusted_spot=round(spot, 4),
                dynamic_spread=round(self.chainlink_spread, 4),
                market_fv=round(polymarket_mid_up, 4),
                old_spot=round(old_spot, 4),
            )
        else:
            self.chainlink_spread = 0

        # 3. Compute raw model fair value: P(Up), using the dynamically adjusted
        # live spot when available. The final trading FV is blended with
        # market-implied probability below.
        model_fv = self.fair_value_model.fair_value(spot, sigma, now, update_state=False)
        fv = model_fv
        try:
            import math
            total_years = total_window / (365.25 * 86400)
            standardized_move = abs(math.log(float(spot) / float(self.fair_value_model.start_price))) / max(1e-9, float(sigma or 0) * math.sqrt(total_years))
        except Exception:
            standardized_move = 0.0

        model_confidence = fv_model_confidence(
            model_fv,
            elapsed_fraction,
            standardized_move,
            polymarket_mid_up,
        )
        fv = blended_fair_value(model_fv, polymarket_mid_up, model_confidence)
        self.last_fair_value = fv
        if hasattr(self.order_mgr.executor, 'update_fair_value'):
            self.order_mgr.executor.update_fair_value(fv, spot)

        basis_delta = abs(model_fv - polymarket_mid_up) if polymarket_mid_up is not None else None
        log.info(
            "fair_value_inputs",
            asset=self.asset,
            start_price=round(float(self.fair_value_model.start_price or 0), 4),
            live_spot=round(float(spot or 0), 4),
            sigma=round(float(sigma or 0), 4),
            elapsed_fraction=round(elapsed_fraction, 4),
            standardized_move=round(float(standardized_move or 0), 4),
            model_fv=round(float(model_fv or 0), 4),
            market_fv=(round(polymarket_mid_up, 4) if polymarket_mid_up is not None else None),
            model_confidence=round(float(model_confidence or 0), 4),
            final_fv=round(float(fv or 0), 4),
            basis_delta=(round(basis_delta, 4) if basis_delta is not None else None),
        )

        # 4. Determine phase
        phase = determine_phase(remaining, self.gc.stop_quoting_seconds,
                                self.gc.reduce_size_seconds)

        # Balance-only mode: last 4 minutes of the window.
        # Goal: stop building the heavy side and quote ONLY to repair inventory.
        # Reduced from 300s to 240s to give more time for normal pair-matching.
        balance_only = remaining <= 240
        min_order_size = max(1, int(getattr(self.ac, "min_order_size", 5)))

        def _repair_size(raw_size: int) -> int:
            """Return a valid close-only repair size or 0 if below live minimum."""
            raw_size = int(raw_size or 0)
            if raw_size < min_order_size:
                return 0
            return raw_size

        def _normalize_quote_sizes(yes_size: int, no_size: int, allow_round_up: bool = True) -> tuple[int, int]:
            """Enforce Polymarket minimum order size on active quote sides."""
            yes_size = int(yes_size or 0)
            no_size = int(no_size or 0)
            if allow_round_up:
                yes_size = min_order_size if 0 < yes_size < min_order_size else yes_size
                no_size = min_order_size if 0 < no_size < min_order_size else no_size
            else:
                yes_size = 0 if 0 < yes_size < min_order_size else yes_size
                no_size = 0 if 0 < no_size < min_order_size else no_size
            return yes_size, no_size

        # Get inventory position early for the DEAD_ZONE check. Attach live CTF
        # identifiers for mid-market merge calls; persisted inventory only stores
        # market_id, but live merge needs condition id and ERC1155 token ids.
        pos = self.inventory.get_or_create(market.market_id, self.asset)
        pos.condition_id = getattr(market, "condition_id", None) or market.market_id
        pos.yes_token_id = str(getattr(market, "token_id_up", "") or "")
        pos.no_token_id = str(getattr(market, "token_id_down", "") or "")

        if not await self._sync_live_fills_before_quote(market, fv, pos):
            return

        if has_negative_matched_pair_edge(pos):
            pairs = int(pos.matched_pairs())
            pair_pnl = round(pos.matched_pair_profit(), 4)
            condition_id = getattr(pos, "condition_id", None) or market.market_id
            log.warning(
                "negative_pair_edge_recovery",
                asset=self.asset,
                market=market.market_id[:8],
                matched_pairs=pairs,
                pair_pnl=pair_pnl,
                msg="Stale negative-edge pairs detected; merging to recover capital",
            )

            # The loss is already locked in from fills. Merge recovers ~$1/pair
            # minus the small loss back to the wallet. Halting would just
            # abandon the capital AND prevent future profitable trading.
            merged = False
            if pairs > 0 and condition_id:
                amount = int(pairs * 1e6)
                tx = None

                # Infer correct collateral for this market
                collateral_token = getattr(self.gasless_merger, "_collateral_token", "") if self.gasless_merger else ""
                if self.balance_monitor and getattr(self.balance_monitor, "_ctf", None):
                    try:
                        from src.execution.ctf_ops import infer_collateral_token_for_market
                        collateral_token = infer_collateral_token_for_market(
                            self.balance_monitor._w3,
                            self.balance_monitor._ctf,
                            condition_id,
                            getattr(pos, "yes_token_id", ""),
                            getattr(pos, "no_token_id", ""),
                            collateral_token,
                        )
                    except Exception:
                        pass

                if self.gasless_merger and self.gasless_merger.is_available:
                    tx = await self.gasless_merger.merge_positions(
                        condition_id, amount, collateral_token=collateral_token)
                if not tx and self.ctf:
                    tx = await self.ctf.merge_positions(
                        condition_id, amount, collateral_token=collateral_token)

                if tx:
                    # Record the (negative) profit, recover capital, clear state
                    profit = pos.matched_pair_profit()
                    self.pnl.record_settlement(profit, market.market_id)
                    self.pnl.record_capital_recovery(pairs * 1.0)
                    pos.acknowledge_settlement()

                    # ERC20 approvals are permanent; only sync CLOB balance.
                    sync_balance = getattr(self.order_mgr.executor, "sync_balance_allowance", None)
                    if callable(sync_balance):
                        await asyncio.sleep(3)
                        for _attempt in range(1, 4):
                            try:
                                if await sync_balance():
                                    break
                            except Exception:
                                pass
                            await asyncio.sleep(2 * _attempt)

                    log.info(
                        "negative_pair_edge_recovered",
                        asset=self.asset,
                        pairs=pairs,
                        profit=f"${profit:.4f}",
                        tx=str(tx)[:16],
                    )
                    merged = True

            # Clear stale inventory regardless of merge success.
            # If merge failed, the on-chain state is unknown (maybe already
            # merged by a previous session). Either way, continuing to trade
            # is better than halting permanently.
            self.inventory.clear_market(market.market_id)
            pos = self.inventory.get_or_create(market.market_id, self.asset)
            pos.condition_id = getattr(market, "condition_id", None) or market.market_id
            pos.yes_token_id = str(getattr(market, "token_id_up", "") or "")
            pos.no_token_id = str(getattr(market, "token_id_down", "") or "")

            if not merged:
                log.warning(
                    "negative_pair_edge_cleared_without_merge",
                    asset=self.asset,
                    market=market.market_id[:8],
                    msg="Cleared stale inventory; on-chain pairs may need manual redemption",
                )

        if phase == "DEAD_ZONE" and pos.share_imbalance() == 0:
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, phase, remaining)
            return

        # 5. Apply phase parameters
        apply_phase_params(phase, self.quote_engine, self.ac)

        # 6. Regime filter
        self.regime_filter.update(fv)
        safe, spread_override = self.regime_filter.is_safe_to_quote()
        regime_halted = False
        if not safe:
            # If inventory is imbalanced, do not fully pause quoting. Continue
            # close-only so the bot can repair exposure instead of freezing while
            # one side is heavy.
            if abs(pos.share_imbalance()) >= min_order_size:
                regime_halted = True
            else:
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(market, spot, fv, sigma, phase, remaining)
                return
        if spread_override:
            self.quote_engine.spread_multiplier = max(
                self.quote_engine.spread_multiplier, spread_override
            )

        # 7. Risk check (only cancel THIS market's quotes, not all assets)
        if self.portfolio_pnl_getter:
            current_pnl = self.portfolio_pnl_getter()
        else:
            current_pnl = pos.mark_to_market(fv) + self.pnl.net_pnl
        is_halted = regime_halted
        halt_reason = "REGIME_HALT" if regime_halted else ""

        if self.risk_engine.halted or not self.risk_engine.check_stops(current_pnl):
            is_halted = True
            halt_reason = self.risk_engine.halt_reason or "HALTED"

        if is_halted:
            if self._repair_mode_started_at is None:
                self._repair_mode_started_at = now
            repair_elapsed = now - self._repair_mode_started_at
            if repair_elapsed > 120:
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(market, spot, fv, sigma, f"{halt_reason}_REPAIR_TIMEOUT", remaining)
                return

            # Repair-only during halts must be less aggressive: wider spread + no
            # oversizing. This reduces getting picked off while still allowing
            # imbalance repair.
            self.quote_engine.spread_multiplier = max(self.quote_engine.spread_multiplier, 2.0)
            self.quote_engine.min_spread = max(self.quote_engine.min_spread, 0.05)
        else:
            self._repair_mode_started_at = None

        # 8. Edge tracker reaction
        # This will auto-adjust the quote_engine.spread_multiplier if toxicity is high.
        # We do NOT return early here, otherwise the bot freezes and stops updating quotes!
        self.edge_tracker.should_react(self.quote_engine)

        # 9. Compute inventory state before toxicity decisions. If a toxic fill
        # leaves us imbalanced, the safest response is not a full quoting freeze;
        # it is close-only repair on the light side with conservative sizing.
        # Uses SHARE COUNT imbalance (Up - Down), not dollar delta.
        imbalance = pos.share_imbalance()
        abs_imbalance = abs(imbalance)
        # Treat any leftover as actionable inventory risk. If one side filled and
        # the other did not, quote ONLY the light side until balanced again.
        inventory_repair = abs_imbalance >= min_order_size
        dust_normalization = 0 < abs_imbalance < min_order_size
        close_only_phase = phase in ["FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE"]

        # 10. Toxicity monitor
        self.toxicity_monitor.update_delayed_mids(fv)
        self.toxicity_monitor.adjust_spread(self.quote_engine)
        if not is_halted and self.toxicity_monitor.check_kill_switch(self.edge_tracker):
            if inventory_repair:
                # Keep repairing the imbalance. A toxicity halt should stop
                # opening/normal quoting, but freezing an unpaired side leaves
                # settlement exposure and made repair take too long in smoke runs.
                is_halted = True
                halt_reason = "TOXICITY_REPAIR_ONLY"
                # Less punitive than a full risk/regime halt: keep pair-edge
                # protection, but do not widen so far that the light-side repair
                # sits behind the market for minutes.
                self.quote_engine.spread_multiplier = max(self.quote_engine.spread_multiplier, 1.25)
                self.quote_engine.min_spread = max(self.quote_engine.min_spread, 0.02)
                if now - self._last_toxicity_repair_override_log >= 5.0:
                    log.warning(
                        "toxicity_repair_override",
                        asset=self.asset,
                        imbalance=round(imbalance, 4),
                        up_shares=round(pos.yes_shares, 4),
                        down_shares=round(pos.no_shares, 4),
                        min_spread=round(self.quote_engine.min_spread, 4),
                        spread_multiplier=round(self.quote_engine.spread_multiplier, 4),
                        msg="toxicity halt converted to close-only repair",
                    )
                    self._last_toxicity_repair_override_log = now
            else:
                is_halted = True
                halt_reason = "TOXICITY_HALT"

        # 11. Compute base quote sizes
        repair_mode = "normal"
        inv_state = self.inventory.get_state(market.market_id, fv, t_norm)
        up_size, down_size = self.inventory.compute_size_adjustment(
            market.market_id, fv, self.quote_engine.max_order_size, t_norm
        )

        # 10.25 Inventory repair / dust-normalization overrides normal quoting.
        # Guardrails:
        # - no unrelated normal two-sided quoting while carrying a tail
        # - dust mode is capped at 2x min size by compute_inventory_repair_sizes()
        # - do not open a two-sided dust plan during halts or close-only phases
        if dust_normalization and not is_halted and not close_only_phase:
            up_size, down_size, repair_mode = compute_fv_aware_dust_repair_sizes(
                imbalance,
                fv,
                min_order_size,
                self.quote_engine.max_order_size,
            )
            log.info(
                "sub_minimum_repair_quote",
                market=market.market_id,
                imbalance=round(imbalance, 4),
                fair_value=round(fv, 4),
                up_size=up_size,
                down_size=down_size,
                mode=repair_mode,
            )
            if up_size == 0 and down_size == 0:
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(market, spot, fv, sigma, phase, remaining)
                return
        elif balance_only or inventory_repair:
            if imbalance != 0:
                up_size, down_size, repair_mode = compute_inventory_repair_sizes(
                    imbalance,
                    min_order_size,
                    self.quote_engine.max_order_size,
                )
                # Larger tails are close-only. If a tiny dust tail reaches a
                # close-only context, cancel instead of making the tail worse.
                if repair_mode.startswith("dust_"):
                    up_size = 0
                    down_size = 0
            else:
                up_size = 0
                down_size = 0

            if up_size == 0 and down_size == 0:
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(market, spot, fv, sigma, phase, remaining)
                return

        # 10.5 Enforce Close-Only quoting during near-expiry phases OR HALTS.
        if is_halted or phase in ["FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE"]:
            if imbalance > 0:
                if abs_imbalance < min_order_size:
                    up_size, down_size, repair_mode = compute_fv_aware_dust_repair_sizes(
                        imbalance, fv, min_order_size, self.quote_engine.max_order_size)
                else:
                    up_size = 0
                    down_size = _repair_size(min(self.quote_engine.max_order_size, int(abs_imbalance)))
                    repair_mode = "repair_down"
            elif imbalance < 0:
                if abs_imbalance < min_order_size:
                    up_size, down_size, repair_mode = compute_fv_aware_dust_repair_sizes(
                        imbalance, fv, min_order_size, self.quote_engine.max_order_size)
                else:
                    down_size = 0
                    up_size = _repair_size(min(self.quote_engine.max_order_size, int(abs_imbalance)))
                    repair_mode = "repair_up"
            else:
                # If we're flat near expiry, we intentionally do not quote.
                up_size = 0
                down_size = 0

                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(
                    market, spot, fv, sigma,
                    halt_reason if is_halted else phase,
                    remaining,
                )
                return

            # If halted/close-only and flat or intentionally holding a favored
            # dust tail, stop quoting entirely.
            if up_size == 0 and down_size == 0:
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(
                    market, spot, fv, sigma,
                    halt_reason if is_halted else phase,
                    remaining,
                )
                return

        # 11. Capital limit check (includes cross-asset arbiter)
        blocks = self.inventory.check_capital_limit(market.market_id, fv, self.asset)
        if blocks.get("block_yes"):
            up_size = 0
        if blocks.get("block_no"):
            down_size = 0

        # Enforce live minimum order size before quote generation. In normal quoting
        # modes, round active-but-small sides up to the minimum. In close-only modes,
        # avoid over-repairing small residual inventory (< min_order_size).
        up_size, down_size = _normalize_quote_sizes(
            up_size,
            down_size,
            allow_round_up=not (inventory_repair or balance_only or phase in ["FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE"] or is_halted),
        )

        # Defensive invariant: dust mode is ONLY for sub-minimum tails. If any
        # earlier sizing path mislabels a real imbalance as dust, convert it
        # back to close-only repair instead of stopping or quoting the wrong side.
        if repair_mode.startswith("dust_") and abs_imbalance >= min_order_size:
            log.warning(
                "dust_mode_invariant_corrected",
                asset=self.asset,
                imbalance=round(imbalance, 4),
                min_order_size=min_order_size,
                previous_mode=repair_mode,
            )
            if imbalance > 0:
                up_size, down_size, repair_mode = 0, min(self.quote_engine.max_order_size, int(abs_imbalance)), "repair_down"
            else:
                up_size, down_size, repair_mode = min(self.quote_engine.max_order_size, int(abs_imbalance)), 0, "repair_up"

        if up_size == 0 and down_size == 0:
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._last_close_only_repair_mode = None
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        # Live safety: when entering close-only repair, cancel every known/open
        # live quote before placing the light-side repair order. This prevents
        # stale heavy-side orders from stacking into 80-vs-5 style inventory when
        # CLOB order listing is unavailable and local ActiveQuotes is incomplete.
        if repair_mode in ("repair_up", "repair_down"):
            if self._last_close_only_repair_mode != repair_mode:
                log.warning(
                    "entering_close_only_repair_cancel_all",
                    asset=self.asset,
                    mode=repair_mode,
                    imbalance=round(imbalance, 4),
                    up_shares=round(pos.yes_shares, 4),
                    down_shares=round(pos.no_shares, 4),
                )
                if not await self.order_mgr.cancel_all():
                    self.stop_reason = "repair_entry_cancel_all_failed"
                    self._running = False
                    return
                self._last_close_only_repair_mode = repair_mode

            # Every repair cycle, explicitly cancel the heavy-side token. This is
            # intentionally harsher than normal repricing: live CLOB reconciliation
            # is incomplete on this SDK, and stale heavy-side orders are worse
            # than losing queue priority.
            if repair_mode == "repair_up":
                ok = await self.order_mgr.cancel_side_quotes(market.market_id, "no", market.token_id_down)
            else:
                ok = await self.order_mgr.cancel_side_quotes(market.market_id, "yes", market.token_id_up)
            if not ok:
                self.stop_reason = f"heavy_side_cancel_failed:{repair_mode}"
                self._running = False
                return
        else:
            self._last_close_only_repair_mode = None

        # Book snapshots/FV blend were already computed before any early-return
        # path so dashboard, risk, sizing, and quotes all use one FV source.
        if (repair_mode == "normal"
                and not balance_only
                and not is_halted
                and basis_guard_triggered(model_fv, polymarket_mid_up)):
            if abs_imbalance >= min_order_size:
                up_size, down_size, repair_mode = compute_inventory_repair_sizes(
                    imbalance,
                    min_order_size,
                    self.quote_engine.max_order_size,
                )
                is_halted = True
                halt_reason = "BASIS_GUARD"
                log.warning(
                    "basis_guard_close_only",
                    asset=self.asset,
                    fair_value=round(fv, 4),
                    model_fv=round(model_fv, 4),
                    polymarket_mid_up=round(polymarket_mid_up, 4),
                    basis_delta=round(basis_delta, 4),
                    imbalance=round(imbalance, 4),
                    repair_mode=repair_mode,
                )
            else:
                log.warning(
                    "basis_guard_stop_quoting",
                    asset=self.asset,
                    fair_value=round(fv, 4),
                    model_fv=round(model_fv, 4),
                    polymarket_mid_up=round(polymarket_mid_up, 4),
                    basis_delta=round(basis_delta, 4),
                    imbalance=round(imbalance, 4),
                    msg="Raw model FV disagrees with Polymarket implied probability; flat/dust inventory held close-only",
                )
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(market, spot, fv, sigma, "BASIS_GUARD", remaining)
                return

        # 12. Generate quotes using share imbalance for price skewing
        #     yes_buy = Up buy price, no_buy = Down buy price
        quotes = self.quote_engine.generate_quotes(
            fair_value=fv,
            t_normalized=t_norm,
            sigma=sigma,
            share_imbalance=imbalance,
            max_imbalance=self.ac.max_dollar_delta,  # reuse config threshold
            yes_size=up_size,
            no_size=down_size,
            best_ask_yes=best_ask_yes,
            best_ask_no=best_ask_no,
            best_bid_yes=best_bid_yes,
            best_bid_no=best_bid_no,
        )
        quotes.phase = phase
        quotes = apply_dust_price_guardrails(
            quotes,
            repair_mode,
            best_ask_yes=best_ask_yes,
            best_ask_no=best_ask_no,
        )

        # Directional market guard: graduated severity instead of binary block.
        # At extreme FVs, the "cheap" side gets filled easily (adverse selection)
        # while the expensive side doesn't fill, creating unmatched inventory.
        #
        # GRADUATED APPROACH (replaces the old binary FV >= 0.65 block):
        #   FV 0.35-0.65: normal quoting (no change)
        #   FV 0.65-0.80 or 0.20-0.35: reduce CHEAP side size by 50%
        #   FV > 0.80 or FV < 0.20: block cheap side entirely (repair-only)
        #
        # This allows pair completion in moderate directional markets while
        # protecting against extreme adverse selection.
        if repair_mode == "normal":
            if fv >= 0.80 or fv <= 0.20:
                # Extreme: block the cheap/adverse side entirely
                log.warning(
                    "normal_quote_reduced_extreme_directional",
                    asset=self.asset,
                    fair_value=round(fv, 4),
                    action="block_cheap_side",
                )
                if fv >= 0.80:
                    # NO is cheap (adverse selection) → block NO
                    quotes.no_buy_size = 0
                else:
                    # YES is cheap (adverse selection) → block YES
                    quotes.yes_buy_size = 0
            elif fv >= 0.65 or fv <= 0.35:
                # Moderate: halve the cheap side to slow adverse fills
                log.info(
                    "normal_quote_reduced_moderate_directional",
                    asset=self.asset,
                    fair_value=round(fv, 4),
                    action="halve_cheap_side",
                )
                if fv >= 0.65:
                    # NO is cheap → halve NO size
                    quotes.no_buy_size = max(0, int(quotes.no_buy_size * 0.5))
                else:
                    # YES is cheap → halve YES size
                    quotes.yes_buy_size = max(0, int(quotes.yes_buy_size * 0.5))

        # 12.25 Pair-cost pre-check: block the adverse side if both-side
        # fill would create negative-edge pairs. This is a HARD guard that
        # catches scenarios like YES@$0.46 + NO@$0.71 = $1.17 before they
        # happen. Unlike the post-generation combined-cost enforcement in
        # quote_engine (which drops the heavy side price), this blocks the
        # side that's LIKELY TO FILL FIRST (the cheap side in a directional
        # market).
        if (repair_mode == "normal"
                and quotes.yes_buy_size > 0
                and quotes.no_buy_size > 0):
            proposed_combined = float(quotes.yes_buy_price or 0) + float(quotes.no_buy_price or 0)
            if proposed_combined > MAX_COMBINED_COST:
                log.warning(
                    "pair_cost_precheck_blocking_adverse_side",
                    asset=self.asset,
                    combined=round(proposed_combined, 4),
                    max_allowed=MAX_COMBINED_COST,
                    yes_price=quotes.yes_buy_price,
                    no_price=quotes.no_buy_price,
                    fair_value=round(fv, 4),
                )
                # Block the CHEAP side (it fills first and creates the problem)
                if fv >= 0.50:
                    # YES is expensive, NO is cheap → block NO
                    quotes.no_buy_size = 0
                else:
                    # NO is expensive, YES is cheap → block YES
                    quotes.yes_buy_size = 0

        # 12.35 FV-favored entry mode: when flat, start by buying only the side
        # the model likes (e.g. FV=0.60 => YES first). Once that side fills,
        # the existing inventory-repair logic quotes only the opposite side to
        # complete profitable pairs under the universal pair-cost guard.
        fv_entry_side = None
        if (repair_mode == "normal" and not balance_only and not is_halted
                and not close_only_phase and remaining >= FV_FAVORED_ENTRY_STOP_SECONDS):
            fv_entry_side = apply_fv_favored_entry_mode(
                quotes,
                fair_value=fv,
                share_imbalance=imbalance,
                min_order_size=min_order_size,
                best_ask_yes=best_ask_yes,
                best_ask_no=best_ask_no,
                best_bid_yes=best_bid_yes,
                best_bid_no=best_bid_no,
            )
            if fv_entry_side:
                log_method = log.warning if fv_entry_side == "blocked" else log.info
                log_method(
                    "fv_favored_entry_mode" if fv_entry_side != "blocked" else "fv_favored_entry_blocked_unrepairable",
                    asset=self.asset,
                    fair_value=round(fv, 4),
                    side=fv_entry_side,
                    yes_size=quotes.yes_buy_size,
                    no_size=quotes.no_buy_size,
                    yes_price=quotes.yes_buy_price,
                    no_price=quotes.no_buy_price,
                    best_ask_yes=best_ask_yes,
                    best_ask_no=best_ask_no,
                    threshold=FV_FAVORED_ENTRY_THRESHOLD,
                )

        # 12.5 Capital guardrail (prevents negative capital in dry-run and
        # keeps live sizing within available funds).
        # Conservative: assume both sides could fill immediately.
        try:
            avail = float(getattr(self.pnl, "current_capital", 0) or 0)
            yes_notional = float(quotes.yes_buy_price or 0) * float(quotes.yes_buy_size or 0)
            no_notional = float(quotes.no_buy_price or 0) * float(quotes.no_buy_size or 0)
            planned = yes_notional + no_notional

            # Live capital can be trapped in matched pairs. If available balance
            # is at/under the merge threshold, or the next repair/quote cannot be
            # funded, force a merge BEFORE trying to place orders. Waiting until
            # after order placement fails leaves the bot unable to repair a
            # 35-vs-15 style imbalance.
            if self.balance_monitor and planned > 0:
                bm_balance = float(getattr(self.balance_monitor, "_last_balance", 0) or 0)
                bm_merge_at = float(getattr(self.balance_monitor, "merge_balance", 0) or 0)
                bm_min_pairs = int(getattr(self.balance_monitor, "min_merge_pairs", 1) or 1)
                matched_pairs = int(pos.matched_pairs())
                if bm_balance > 0:
                    # In live mode the wallet balance is the hard spend limit;
                    # current_capital is local accounting and can be optimistic
                    # after restart or missed reconciliation.
                    avail = min(avail, bm_balance)
                balance_pressure = (bm_balance <= bm_merge_at) or (avail <= 0) or (avail < planned)
                if balance_pressure and matched_pairs >= bm_min_pairs:
                    log.info(
                        "pre_quote_merge_triggered",
                        asset=self.asset,
                        balance=f"${bm_balance:.2f}",
                        current_capital=f"${avail:.2f}",
                        planned=f"${planned:.2f}",
                        matched_pairs=matched_pairs,
                    )
                    merge_result = await self.balance_monitor.check_and_merge(
                        inventory_mgr=self.inventory,
                        gasless_merger=self.gasless_merger,
                        ctf_ops=self.ctf,
                        pnl_tracker=self.pnl,
                        force=True,
                        balance_sync=getattr(self.order_mgr.executor, "sync_balance_allowance", None),
                    )
                    if merge_result.get("merged"):
                        recovered = float(merge_result.get("usdc_recovered", 0) or 0)
                        avail = float(getattr(self.pnl, "current_capital", 0) or 0)
                        if self.inventory.capital_arbiter and recovered > 0:
                            self.inventory.capital_arbiter.record_recovery(self.asset, recovered)
                        log.info(
                            "pre_quote_merge_complete",
                            asset=self.asset,
                            pairs=merge_result.get("pairs_merged", 0),
                            usdc=f"${recovered:.2f}",
                            current_capital=f"${avail:.2f}",
                        )
                    else:
                        self._merge_unavailable_until = _time.time() + 60.0
                        log.warning(
                            "pre_quote_merge_no_recovery",
                            asset=self.asset,
                            matched_pairs=matched_pairs,
                            balance=f"${bm_balance:.2f}",
                            current_capital=f"${avail:.2f}",
                            blocked_until=round(self._merge_unavailable_until, 1),
                        )

            if planned > 0 and avail <= 0:
                log.warning(
                    "quote_blocked_no_available_capital",
                    asset=self.asset,
                    planned=f"${planned:.2f}",
                    matched_pairs=int(pos.matched_pairs()),
                )
                quotes.yes_buy_size = 0
                quotes.no_buy_size = 0
            elif avail > 0 and planned > avail:
                scale = max(0.0, min(1.0, avail / planned))
                quotes.yes_buy_size = int(quotes.yes_buy_size * scale)
                quotes.no_buy_size = int(quotes.no_buy_size * scale)

            # Cross-asset arbiter: ensure we don't exceed dynamic allocation.
            if self.inventory.capital_arbiter:
                planned2 = (float(quotes.yes_buy_price or 0) * float(quotes.yes_buy_size or 0)
                            + float(quotes.no_buy_price or 0) * float(quotes.no_buy_size or 0))
                # If blocked, shrink sizes until allowed (binary-ish backoff).
                # Cap iterations defensively in case can_deploy() misbehaves.
                for i in range(20):
                    if planned2 <= 0:
                        break
                    if self.inventory.capital_arbiter.can_deploy(self.asset, planned2):
                        break
                    quotes.yes_buy_size = int(quotes.yes_buy_size * 0.5)
                    quotes.no_buy_size = int(quotes.no_buy_size * 0.5)
                    planned2 = (float(quotes.yes_buy_price or 0) * float(quotes.yes_buy_size or 0)
                                + float(quotes.no_buy_price or 0) * float(quotes.no_buy_size or 0))
                else:
                    log.debug(
                        "capital_arbiter_backoff_cap_hit",
                        asset=self.asset,
                        planned=planned2,
                        yes_size=quotes.yes_buy_size,
                        no_size=quotes.no_buy_size,
                    )

            # After capital scaling/backoff, drop any active side that fell below
            # Polymarket's minimum order size. Dust-normalization is an atomic
            # paired plan: if either leg is no longer valid, cancel both rather
            # than leaving a one-sided top-up landmine.
            quotes.yes_buy_size, quotes.no_buy_size = _normalize_quote_sizes(
                quotes.yes_buy_size,
                quotes.no_buy_size,
                allow_round_up=False,
            )
            if repair_mode.startswith("dust_") and (
                quotes.yes_buy_size < min_order_size or quotes.no_buy_size < min_order_size
            ):
                quotes.yes_buy_size = 0
                quotes.no_buy_size = 0

            # Final invariant after all capital/backoff transforms: repair mode
            # is close-only. repair_up means Down is heavy, so quote YES only;
            # repair_down means Up is heavy, so quote NO only.
            if repair_mode == "repair_up":
                quotes.no_buy_size = 0
            elif repair_mode == "repair_down":
                quotes.yes_buy_size = 0

            # Normal/balanced quoting is atomic unless we intentionally entered
            # FV-favored one-sided entry mode while flat. Capital scaling/backoff
            # must not accidentally turn a balanced market into a one-sided bet.
            if repair_mode == "normal" and abs_imbalance < min_order_size:
                one_sided_normal = (quotes.yes_buy_size > 0) != (quotes.no_buy_size > 0)
                merge_blocked = self._merge_unavailable_until > _time.time()
                allowed_fv_entry = fv_entry_side in ("yes", "no") and one_sided_normal and not merge_blocked
                if (one_sided_normal and not allowed_fv_entry) or merge_blocked:
                    log.warning(
                        "normal_quote_blocked_not_atomic",
                        asset=self.asset,
                        yes_size=quotes.yes_buy_size,
                        no_size=quotes.no_buy_size,
                        merge_blocked=merge_blocked,
                        imbalance=round(imbalance, 4),
                    )
                    quotes.yes_buy_size = 0
                    quotes.no_buy_size = 0
        except Exception:
            # Never fail a cycle due to sizing guardrails.
            pass

        # Belt-and-suspenders atomicity check outside the guardrail try-block:
        # flat normal mode must be both-side, no-side, or an explicitly allowed
        # FV entry. It must never accidentally leak one naked side.
        if repair_mode == "normal" and abs_imbalance < min_order_size:
            one_sided_normal = (quotes.yes_buy_size > 0) != (quotes.no_buy_size > 0)
            allowed_fv_entry = fv_entry_side in ("yes", "no") and one_sided_normal
            if one_sided_normal and not allowed_fv_entry:
                log.warning(
                    "normal_quote_blocked_not_atomic_final",
                    asset=self.asset,
                    yes_size=quotes.yes_buy_size,
                    no_size=quotes.no_buy_size,
                    imbalance=round(imbalance, 4),
                )
                quotes.yes_buy_size = 0
                quotes.no_buy_size = 0

        if quotes.yes_buy_size == 0 and quotes.no_buy_size == 0:
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        # Absolute post-generation invariant: if inventory is already imbalanced
        # by at least one live-min order, do not quote the heavy side. This is a
        # final backstop against quote-engine/capital transforms reintroducing
        # the side we are trying to stop buying.
        if abs(pos.share_imbalance()) >= min_order_size:
            if pos.share_imbalance() > 0:
                quotes.yes_buy_size = 0
                repair_mode = "repair_down"
            else:
                quotes.no_buy_size = 0
                repair_mode = "repair_up"

        # ──────────────────────────────────────────────────────────
        # Universal pair-cost guard: cap EACH side's bid against
        # unmatched fills on the opposite side, regardless of mode.
        #
        # In normal two-sided quoting, fills happen asynchronously
        # across cycles at different FV levels. A per-cycle combined
        # cost < $1 does NOT guarantee the FIFO pair cost < $1 when
        # fills land at different times. This guard prevents that.
        # ──────────────────────────────────────────────────────────
        for side_label, buy_price_attr, buy_size_attr, best_ask, best_bid in [
            ("yes", "yes_buy_price", "yes_buy_size", best_ask_yes, best_bid_yes),
            ("no",  "no_buy_price",  "no_buy_size",  best_ask_no,  best_bid_no),
        ]:
            size_val = getattr(quotes, buy_size_attr, 0)
            price_val = getattr(quotes, buy_price_attr, None)
            if size_val <= 0 or not price_val:
                continue

            pair_edge = repair_min_edge_for_remaining(remaining, repair_mode)
            cap = float(pos.max_profitable_repair_price(
                side_label, size_val, min_edge=pair_edge))

            # No unmatched fills on opposite → cap is 0.99, no constraint
            if cap >= 0.99:
                continue

            is_repair = (repair_mode == f"repair_{side_label}"
                         or (side_label == "yes" and repair_mode == "repair_up")
                         or (side_label == "no" and repair_mode == "repair_down"))

            if cap < 0.01:
                log.warning("pair_cost_guard_blocked",
                            market=market.market_id[:8], side=side_label,
                            quoted=price_val, cap=round(cap, 4),
                            mode=repair_mode)
                setattr(quotes, buy_size_attr, 0)
            elif is_repair:
                # In repair mode: use aggressive pricing up to cap
                old_price = price_val
                new_price = aggressive_repair_price(
                    price_val, cap, best_ask=best_ask, best_bid=best_bid)
                if new_price is None:
                    setattr(quotes, buy_size_attr, 0)
                else:
                    if old_price and old_price > cap:
                        log.warning("repair_quote_capped_for_pair_edge",
                                    market=market.market_id[:8], side=side_label,
                                    quoted=old_price, cap=round(cap, 4),
                                    min_edge=pair_edge)
                    elif new_price > float(old_price or 0):
                        log.info("repair_quote_aggressed_to_cap",
                                 market=market.market_id[:8], side=side_label,
                                 old=old_price, new=new_price,
                                 cap=round(cap, 4), min_edge=pair_edge,
                                 best_ask=best_ask)
                    setattr(quotes, buy_price_attr, new_price)
            elif float(price_val) > cap:
                # Normal mode: silently clamp to cap
                log.info("pair_cost_guard_clamped",
                         market=market.market_id[:8], side=side_label,
                         quoted=price_val, cap=round(cap, 4),
                         mode=repair_mode)
                setattr(quotes, buy_price_attr, round(cap, 2))

        quotes.combined_cost = round(float(quotes.yes_buy_price or 0) + float(quotes.no_buy_price or 0), 4)
        quotes.edge_per_pair = round(1.0 - quotes.combined_cost, 4)

        if quotes.yes_buy_size == 0 and quotes.no_buy_size == 0:
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        # 13. Pre-trade checks
        fv_fresh = not self.fair_value_model.is_stale
        passed, failed_reasons = pre_trade_checks(fv, quotes, inv_state.value,
                                      fv_fresh, phase)
        if not passed:
            log.warning("pre_trade_failed", market=market.market_id, reasons=failed_reasons)
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        # 14. Update orders
        #     token_id_up = "Up" token, token_id_down = "Down" token
        await self.order_mgr.update_quotes(
            market_id=market.market_id,
            token_id_yes=market.token_id_up,
            token_id_no=market.token_id_down,
            quotes=quotes,
            yes_book_snapshot=book_up,
            no_book_snapshot=book_down,
            repair_mode=repair_mode,
        )
        if getattr(self.order_mgr, "last_order_error", None):
            self.stop_reason = f"order_update_failed:{self.order_mgr.last_order_error}"
            self._running = False
            return

        # 15. Process fills after order updates. Dry-run fills only exist here;
        # live fills were already synced before quote generation, but this cheap
        # post-check can still catch an immediate exchange fill without changing
        # the shared accounting path.
        fills = []
        if hasattr(self.order_mgr.executor, 'check_fills'):
            fills = self.order_mgr.executor.check_fills(
                yes_book_snapshot=book_up,
                no_book_snapshot=book_down,
            )
        elif hasattr(self.order_mgr.executor, 'get_fills'):
            try:
                raw_fills = await self.order_mgr.executor.get_fills(market.market_id)
                fills = self.order_mgr.executor.process_fills(
                    raw_fills,
                    self.inventory,
                    market.market_id,
                    token_id_to_side={
                        str(market.token_id_up): "yes",
                        str(market.token_id_down): "no",
                    },
                )
            except Exception as e:
                log.error("live_fill_check_error", error=str(e))
                self.stop_reason = "live_fill_check_error"
                self._running = False
                return

        if fills and not await self._handle_standardized_fills(market, fills, fv, pos):
            return
        if fills and has_negative_matched_pair_edge(pos):
            pairs = int(pos.matched_pairs())
            log.critical(
                "negative_pair_edge_halt",
                asset=self.asset,
                market=market.market_id[:8],
                matched_pairs=pairs,
                pair_pnl=round(pos.matched_pair_profit(), 4),
                msg="Matched pair cost exceeded 1 after fill; merging and stopping this market",
            )
            # Merge the negative-edge pairs to recover capital before halting.
            # The loss is locked in from fills; leaving pairs unmerged just
            # abandons capital on-chain.
            if pairs > 0:
                condition_id = getattr(pos, "condition_id", None) or market.market_id
                amount = int(pairs * 1e6)
                tx = None
                collateral_token = getattr(self.gasless_merger, "_collateral_token", "") if self.gasless_merger else ""
                if self.gasless_merger and self.gasless_merger.is_available:
                    tx = await self.gasless_merger.merge_positions(
                        condition_id, amount, collateral_token=collateral_token)
                if tx:
                    profit = pos.matched_pair_profit()
                    self.pnl.record_settlement(profit, market.market_id)
                    self.pnl.record_capital_recovery(pairs * 1.0)
                    pos.acknowledge_settlement()
                    log.info("negative_pair_edge_force_merged",
                             pairs=pairs, profit=f"${profit:.4f}", tx=str(tx)[:16])
                    sync_fn = getattr(self.order_mgr.executor, "sync_balance_allowance", None)
                    if callable(sync_fn):
                        await asyncio.sleep(3)
                        try:
                            await sync_fn()
                        except Exception:
                            pass
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self.stop_reason = "negative_pair_edge_halt"
            self._running = False
            return

        # 15.5. Auto-merge check: dollar-based threshold OR low balance OR near expiry
        force_merge = False
        merge_reason = "routine"
        if remaining <= 30 and not getattr(self, '_has_done_30s_merge', False):
            force_merge = True
            merge_reason = "near_expiry"
            self._has_done_30s_merge = True

        # Dollar-based mid-market merge trigger
        if not force_merge and self.inventory.should_merge(market.market_id):
            force_merge = True
            merge_reason = "dollar_threshold"
            log.info("dollar_threshold_merge_triggered",
                     asset=self.asset,
                     locked=f"${pos.locked_capital():.2f}",
                     threshold=f"${self.inventory.auto_merge_dollar_threshold:.2f}")

        if self.balance_monitor:
            merge_result = await self.balance_monitor.check_and_merge(
                inventory_mgr=self.inventory,
                gasless_merger=self.gasless_merger,
                ctf_ops=self.ctf,
                pnl_tracker=self.pnl,
                force=force_merge,
                balance_sync=getattr(self.order_mgr.executor, "sync_balance_allowance", None),
            )
            if merge_result.get("merged"):
                msg = "auto_merge_end_of_market" if merge_reason == "near_expiry" else "auto_merge_during_trading"
                log.info(msg,
                         asset=self.asset,
                         reason=merge_reason,
                         pairs=merge_result["pairs_merged"],
                         usdc=f"${merge_result['usdc_recovered']:.2f}")
                # Update capital arbiter on recovery
                if self.inventory.capital_arbiter:
                    self.inventory.capital_arbiter.record_recovery(
                        self.asset, merge_result['usdc_recovered'])

        # 16. Update dashboard
        self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining,
                                quotes, pos, imbalance, inv_state.value)

    def _update_dashboard_waiting(self):
        if not self._dashboard_cb:
            return
            
        spot = getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0)
        price_age = (self.price_feed.get_price_age(self.ac.symbol)
                     if hasattr(self.price_feed, "get_price_age") else 0)
        price_source = (self.price_feed.get_price_source(self.ac.symbol)
                        if hasattr(self.price_feed, "get_price_source") else "unknown")
        
        state = {
            "asset": self.asset,
            "market_id": "waiting...",
            "slug": "Waiting for next Polymarket window...",
            "question": "",
            "start_price": 0,
            "spot_price": spot,
            "raw_spot": spot,
            "chainlink_spread": 0,
            "price_age": price_age,
            "price_source": price_source,
            "fair_value": 0,
            "sigma": 0,
            "ws_ticks": getattr(self.price_feed, "ticks", 0),
            "phase": "WAITING",
            "time_remaining": 0,
            "regime": "WAITING",
            "up_buy": 0,
            "down_buy": 0,
            "up_size": 0,
            "down_size": 0,
            "combined_cost": 0,
            "edge": 0,
            "up_shares": 0,
            "down_shares": 0,
            "up_avg": 0,
            "down_avg": 0,
            "share_imbalance": 0,
            "dollar_delta": 0,
            "matched_pairs": 0,
            "avg_pair_cost": 0,
            "matched_pair_pnl": 0,
            "negative_pair_edge": False,
            "inv_state": "WAITING",
            "net_trading_pnl": self.pnl.net_trading_pnl,
            "outcome_pnl": self.pnl.outcome_pnl,
            "est_rebates": self.pnl.est_rebates,
            "net_pnl": self.pnl.net_pnl,
            "economic_pnl": self.pnl.economic_pnl,
            "rebates_per_hour": self.pnl.rebates_per_hour(),
            "total_volume": self.pnl.total_volume,
            "total_shares": self.pnl.total_shares,
            "markets_settled": self.pnl.markets_settled,
            "total_fills": self.pnl.total_fills,
            "starting_capital": getattr(self.pnl, "starting_capital", 0),
            "current_capital": getattr(self.pnl, "current_capital", 0),
        }
        
        if self.balance_monitor:
            bm_stats = self.balance_monitor.stats
            state["wallet_balance"] = bm_stats["last_balance"]
            state["auto_merges"] = bm_stats["total_merges"]
            state["auto_merged_usdc"] = bm_stats["total_merged_usdc"]
            state["balance_warn_threshold"] = self.balance_monitor.warn_balance
            state["balance_merge_threshold"] = self.balance_monitor.merge_balance
            state["merge_message"] = bm_stats.get("merge_message", "")
            
        self._dashboard_cb(state)

    def _update_dashboard(self, market, spot, fv, sigma, phase,
                           remaining, quotes=None, pos=None,
                           delta=0, inv_state="NORMAL"):
        """Push state to dashboard callback.
        
        Always fetches the real position from inventory so that
        shares/delta display correctly even when quotes are paused
        (e.g., regime spike, risk halt, dead zone).
        """
        if not self._dashboard_cb:
            return

        start_price = (self.fair_value_model.start_price
                       if self.fair_value_model else 0)

        # Always get the REAL position from inventory
        real_pos = self.inventory.get_or_create(market.market_id, self.asset)
        real_delta = real_pos.dollar_delta(fv) if fv else 0
        real_state = self.inventory.get_state(market.market_id, fv)

        raw_spot = getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, spot)
        price_age = (self.price_feed.get_price_age(self.ac.symbol)
                     if hasattr(self.price_feed, "get_price_age") else 0)
        price_source = (self.price_feed.get_price_source(self.ac.symbol)
                        if hasattr(self.price_feed, "get_price_source") else "unknown")
        
        state = {
            "asset": self.asset,
            "market_id": market.market_id,
            "slug": market.slug,
            "question": market.question,
            "start_price": start_price or 0,
            "spot_price": spot or 0,
            "raw_spot": raw_spot or 0,
            "chainlink_spread": getattr(self, 'chainlink_spread', 0),
            "price_age": price_age,
            "price_source": price_source,
            "fair_value": fv,
            "sigma": sigma,
            "ws_ticks": getattr(self.price_feed, "ticks", 0),
            "phase": phase,
            "time_remaining": remaining,
            "regime": self.regime_filter.regime(),
            "up_buy": quotes.yes_buy_price if quotes else 0,
            "down_buy": quotes.no_buy_price if quotes else 0,
            "up_size": quotes.yes_buy_size if quotes else 0,
            "down_size": quotes.no_buy_size if quotes else 0,
            "combined_cost": quotes.combined_cost if quotes else 0,
            "edge": quotes.edge_per_pair if quotes else 0,
            # Always use real inventory data (share-based)
            "up_shares": real_pos.yes_shares,
            "down_shares": real_pos.no_shares,
            "up_avg": real_pos.yes_avg_entry,
            "down_avg": real_pos.no_avg_entry,
            "share_imbalance": real_pos.share_imbalance(),
            "dollar_delta": real_pos.dollar_delta(fv) if fv else 0,
            "matched_pairs": real_pos.matched_pairs(),
            "avg_pair_cost": real_pos.avg_matched_pair_cost(),
            "matched_pair_pnl": real_pos.matched_pair_profit(),
            "negative_pair_edge": has_negative_matched_pair_edge(real_pos),
            "inv_state": real_state.value,
            # P&L with rebates and outcomes
            "net_trading_pnl": self.pnl.net_trading_pnl,
            "outcome_pnl": self.pnl.outcome_pnl,
            "est_rebates": self.pnl.est_rebates,
            "net_pnl": self.pnl.net_pnl,
            "economic_pnl": self.pnl.economic_pnl,
            "rebates_per_hour": self.pnl.rebates_per_hour(),
            "total_volume": self.pnl.total_volume,
            "total_shares": self.pnl.total_shares,
            "markets_settled": self.pnl.markets_settled,
            "total_fills": self.pnl.total_fills,
            "starting_capital": getattr(self.pnl, "starting_capital", 0),
            "current_capital": getattr(self.pnl, "current_capital", 0),
        }

        # Add balance monitor stats (live mode only)
        if self.balance_monitor:
            bm_stats = self.balance_monitor.stats
            state["wallet_balance"] = bm_stats["last_balance"]
            state["auto_merges"] = bm_stats["total_merges"]
            state["auto_merged_usdc"] = bm_stats["total_merged_usdc"]
            state["balance_warn_threshold"] = self.balance_monitor.warn_balance
            state["balance_merge_threshold"] = self.balance_monitor.merge_balance
            state["merge_message"] = bm_stats.get("merge_message", "")

        self._dashboard_cb(state)

    async def stop(self):
        self._running = False
        if self.current_market:
            await self.order_mgr.cancel_market_quotes(
                self.current_market.market_id
            )
