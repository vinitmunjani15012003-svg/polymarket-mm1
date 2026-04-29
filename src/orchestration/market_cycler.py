"""
Market cycler — manages the continuous 15-minute market lifecycle.

For each asset: discover market → quote → wind down → settle → repeat.

NOTE: These are "Up or Down" markets (directional), not strike-based.
Fair value = P(price goes UP from window start to window end).
"""

import asyncio
import time
from typing import Optional

from src.config import AssetConfig, GlobalConfig
from src.data.market_discovery import MarketDiscovery, MarketInfo
from src.data.orderbook import OrderBookReader
from src.strategy.volatility import VolatilityEstimator
from src.strategy.quote_engine import QuoteEngine
from src.strategy.inventory import InventoryManager
from src.execution.order_manager import OrderManager
from src.execution.ctf_ops import (
    CTFOperations, GaslessMerger, BalanceMonitor,
)
from src.risk.regime_filter import RegimeFilter
from src.risk.toxicity import FillEdgeTracker, ToxicityMonitor
from src.risk.risk_engine import (RiskEngine, determine_phase,
                                   apply_phase_params, pre_trade_checks)
from src.monitoring.pnl_tracker import PnLTracker
from src.monitoring.logger import get_logger

log = get_logger("market_cycler")


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
                   now_ts: float = None) -> float:
        """
        Compute P(Up) = P(price_end >= price_start).

        If we know the start price: uses log(current/start) as drift signal.
        If we don't: defaults to 0.50 (no edge from price level).
        """
        from scipy.stats import norm
        import math

        now_ts = now_ts or time.time()
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
        self._last_fair_value = prob
        self._last_update_ts = now_ts
        return prob

    def set_start_price(self, price: float):
        """Set the opening price once known."""
        if self.start_price is None and price > 0:
            self.start_price = price
            log.info("start_price_set", price=price)

    def time_remaining_seconds(self, now_ts: float = None) -> float:
        now_ts = now_ts or time.time()
        return max(0, self.resolve_ts - now_ts)

    def normalized_time(self, now_ts: float = None) -> float:
        now_ts = now_ts or time.time()
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
        return (time.time() - self._last_update_ts) > 5.0


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
        
        # Merge threshold: auto-merge when matched pairs exceed this
        self._merge_threshold = 50  # shares

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
        self.regime_filter = RegimeFilter(
            lookback=regime_lookback,
            trend_threshold=regime_trend,
            spike_threshold=regime_spike,
        )
        self.edge_tracker = FillEdgeTracker(window=tox_edge_window)
        self.toxicity_monitor = ToxicityMonitor(
            window_seconds=tox_window,
            threshold=tox_threshold,
        )
        self.last_fair_value: Optional[float] = None

        self._running = False
        self._last_market_slug = None  # Track to detect new market

    async def run(self):
        """Main loop: cycle through markets continuously."""
        self._running = True
        log.info("cycler_started", asset=self.asset)

        while self._running:
            try:
                # 1. Discover next market
                market = await self._find_next_market()
                if not market:
                    await asyncio.sleep(5)
                    continue

                # Skip if same market as before (already being traded)
                if market.slug == self._last_market_slug:
                    await asyncio.sleep(5)
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

                # 4. Market ended — settle
                await self._settle_market()

                # 5. Wait for current window to expire, then look for next
                wait_time = max(0, market.resolve_ts - time.time()) + 2
                if wait_time > 0:
                    log.info("waiting_for_next_window",
                             asset=self.asset, wait_s=round(wait_time, 1))
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

                    # Prefer gasless merge
                    if self.gasless_merger and self.gasless_merger.is_available:
                        tx = await self.gasless_merger.merge_positions(
                            condition_id, amount
                        )

                    # Fallback to on-chain
                    if not tx and self.ctf:
                        tx = await self.ctf.merge_positions(
                            condition_id, amount
                        )

                    if tx:
                        pair_profit = pos.matched_pair_profit()
                        self.pnl.record_settlement(pair_profit, market.market_id)
                        log.info("pairs_merged",
                                 pairs=pairs,
                                 profit=f"${pair_profit:.4f}",
                                 tx=str(tx)[:16] if tx else "none")

            # Try to redeem any remaining tokens (if market resolved)
            if self.ctf:
                condition_id = getattr(market, 'condition_id', None)
                if condition_id:
                    resolved = await self.ctf.is_market_resolved(condition_id)
                    if resolved:
                        tx = await self.ctf.redeem_positions(condition_id)
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
                unmatched_up = pos.yes_shares - pairs
                unmatched_down = pos.no_shares - pairs
                
                if (unmatched_up > 0 or unmatched_down > 0):
                    # Kick off background task to wait for actual resolution from Polymarket API
                    asyncio.create_task(self._wait_and_settle_unmatched(market, pos, pairs))

            # Clear position from inventory state
            self.inventory.clear_market(market.market_id)
            
            self.current_market = None

        # Reset per-market state for next cycle
        self.quote_engine.reset_params()
        if not self.portfolio_pnl_getter:
            self.risk_engine.reset_for_new_market(self.pnl.net_pnl)

    async def _wait_and_settle_unmatched(self, market: MarketInfo, pos, pairs: int):
        """Background task to poll Gamma API and wait for actual market resolution."""
        unmatched_up = pos.yes_shares - pairs
        unmatched_down = pos.no_shares - pairs
        
        log.info("waiting_for_actual_resolution", slug=market.slug)
        
        while self._running:
            await asyncio.sleep(30)
            try:
                # Re-fetch market metadata from Gamma API
                m = await self.discovery._fetch_market(market.asset, int(market.window_start_ts))
                if not m:
                    continue
                    
                # Market is considered resolved if it's inactive or one token has hit $1.00
                if not m.active or m.up_price == 1.0 or m.down_price == 1.0:
                    won_up = False
                    if m.up_price == 1.0:
                        won_up = True
                    elif m.down_price == 1.0:
                        won_up = False
                    else:
                        if not m.active:
                            won_up = m.up_price > m.down_price
                        else:
                            continue
                            
                    winning_shares = unmatched_up if won_up else unmatched_down
                    losing_shares = unmatched_down if won_up else unmatched_up
                    winner_str = "UP" if won_up else "DOWN"
                    
                    cost_of_winning = winning_shares * (pos.yes_avg_entry if won_up else pos.no_avg_entry)
                    cost_of_losing = losing_shares * (pos.no_avg_entry if won_up else pos.yes_avg_entry)
                    
                    revenue = winning_shares * 1.0
                    net_profit = revenue - cost_of_winning - cost_of_losing
                    
                    self.pnl.record_settlement(net_profit, market.market_id)
                    self.pnl.record_capital_recovery(revenue)
                    
                    log.info("dry_run_actual_resolution",
                             winner=winner_str,
                             winning_shares=winning_shares,
                             losing_shares=losing_shares,
                             pnl=f"${net_profit:.4f}")
                    break
            except Exception as e:
                log.error("wait_and_settle_error", error=str(e))

    async def _find_next_market(self) -> Optional[MarketInfo]:
        """Find the next eligible market for this asset."""
        market = await self.discovery.discover_single(
            self.asset,
            min_remaining=self.gc.stop_quoting_seconds + 30
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
        # Fetch the "price to beat" — the exact strike price at window open.
        #
        # Polymarket 15-min windows use the Vatic Trading API to set the strike.
        #
        # Priority:
        #   1. Vatic Trading API (EXACT price to beat)
        #   2. Binance 15m candle open (fast, reliable fallback, ~$10-20 off)
        #   3. Current spot (only if window just opened < 30s ago)
        
        self._has_done_30s_merge = False
        start_price = None
        binance_start_price = None

        # 1. PRIMARY: Exact price-to-beat from Vatic API (Chainlink)
        start_price = await self.price_feed.fetch_vatic_strike(
            self.ac.symbol, market.event_start_ts
        )
        if start_price:
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
                self.vol_estimator.update(raw_spot, time.time())
                sigma = self.vol_estimator.sigma_for_model()
                calibrated = self._calibrate_strike_from_market(market, raw_spot, sigma)
                if calibrated:
                    start_price = calibrated
                    log.info("start_price_from_calibration",
                             asset=self.asset, price=start_price)

        # 4. Fallback: just use Binance if calibration failed
        if binance_start_price and not start_price:
            start_price = binance_start_price
            log.info("start_price_from_binance",
                     asset=self.asset, price=start_price)
            
        # Calculate the Chainlink vs Binance spread
        self.chainlink_spread = 0
        if start_price and binance_start_price and start_price != binance_start_price:
            self.chainlink_spread = start_price - binance_start_price
            log.info("chainlink_spread_calculated", 
                     asset=self.asset, spread=round(self.chainlink_spread, 2))

        # Adjust the current Binance spot by the spread to simulate Chainlink live spot
        raw_binance_spot = self.price_feed.get_price(self.ac.symbol)
        current_spot = raw_binance_spot + self.chainlink_spread if raw_binance_spot else None

        # 3. Fallback: Chainlink on-chain aggregator
        if not start_price:
            start_price = await self.price_feed.fetch_chainlink_price(
                self.ac.symbol, market.event_start_ts
            )
            if start_price:
                log.info("start_price_from_chainlink",
                         asset=self.asset, price=start_price)

        # 4. Last resort: Current spot
        if not start_price:
            elapsed = time.time() - market.event_start_ts
            start_price = current_spot
            if elapsed < 30:
                log.info("start_price_from_spot",
                         asset=self.asset, reason="window_just_opened")
            else:
                log.warning("start_price_from_spot",
                            asset=self.asset,
                            reason="all_sources_failed",
                            elapsed_s=round(elapsed))

        log.info("market_start_price",
                 asset=self.asset,
                 start_price=start_price,
                 current_spot=current_spot,
                 window_start_ts=market.event_start_ts)

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
        self.regime_filter = RegimeFilter(
            lookback=regime_lookback,
            trend_threshold=regime_trend,
            spike_threshold=regime_spike,
        )
        self.edge_tracker = FillEdgeTracker(window=tox_edge_window)
        self.toxicity_monitor = ToxicityMonitor(
            window_seconds=tox_window,
            threshold=tox_threshold,
        )
        self.quote_engine.reset_params()

        while self._running:
            remaining = market.time_remaining
            if remaining <= 0:
                log.info("market_expired", asset=self.asset, slug=market.slug)
                break  # Market resolved

            # Skip quoting if risk-halted (but keep looping to check expiry)
            if self.risk_engine.halted:
                # Check if cooldown expired
                self.risk_engine.check_stops(self.pnl.net_pnl)
                if self.risk_engine.halted:
                    spot = self.price_feed.get_price(self.ac.symbol) or 0
                    spot += self.chainlink_spread  # Apply spread to display
                    self._update_dashboard(market, spot, 0.5, 0, "HALTED", remaining)
                    await asyncio.sleep(self.gc.refresh_interval)
                    continue

            try:
                await self._quote_cycle(market)
            except Exception as e:
                log.error("quote_cycle_error", error=str(e),
                          asset=self.asset)
                await self.order_mgr.cancel_market_quotes(market.market_id)

            await asyncio.sleep(self.gc.refresh_interval)

    async def _quote_cycle(self, market: MarketInfo):
        """Single quote cycle iteration."""
        now = time.time()
        remaining = market.time_remaining

        # 1. Get live spot price (shifted to Chainlink estimate)
        raw_spot = self.price_feed.get_price(self.ac.symbol)
        if not raw_spot:
            log.warning("no_spot_price", symbol=self.ac.symbol)
            return
            
        spot = raw_spot + self.chainlink_spread

        # Set start price if not yet captured
        if self.fair_value_model and not self.fair_value_model.start_price:
            self.fair_value_model.set_start_price(spot)

        # 2. Update volatility
        self.vol_estimator.update(spot, now)
        sigma = self.vol_estimator.sigma_for_model()

        # 3. Compute fair value: P(Up)
        fv = self.fair_value_model.fair_value(spot, sigma, now)
        self.last_fair_value = fv
        t_norm = self.fair_value_model.normalized_time(now)

        # Feed FV to dry-run executor for price-crossing fill simulation
        if hasattr(self.order_mgr.executor, 'update_fair_value'):
            self.order_mgr.executor.update_fair_value(fv, spot)

        # 4. Determine phase
        phase = determine_phase(remaining, self.gc.stop_quoting_seconds,
                                self.gc.reduce_size_seconds)

        # Get inventory position early for the DEAD_ZONE check
        pos = self.inventory.get_or_create(market.market_id, self.asset)

        if phase == "DEAD_ZONE" and pos.share_imbalance() == 0:
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, phase, remaining)
            return

        # 5. Apply phase parameters
        apply_phase_params(phase, self.quote_engine, self.ac)

        # 6. Regime filter
        self.regime_filter.update(fv)
        safe, spread_override = self.regime_filter.is_safe_to_quote()
        if not safe:
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
        if not self.risk_engine.check_stops(current_pnl):
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, "HALTED", remaining)
            return

        # 8. Edge tracker reaction
        # This will auto-adjust the quote_engine.spread_multiplier if toxicity is high.
        # We do NOT return early here, otherwise the bot freezes and stops updating quotes!
        self.edge_tracker.should_react(self.quote_engine)

        # 9. Toxicity monitor
        self.toxicity_monitor.update_delayed_mids(fv)
        self.toxicity_monitor.adjust_spread(self.quote_engine)
        if self.toxicity_monitor.check_kill_switch(self.edge_tracker):
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, "TOXICITY_HALT", remaining)
            return

        # 10. Compute inventory state and sizes
        #     Uses SHARE COUNT imbalance (Up - Down), not dollar delta
        imbalance = pos.share_imbalance()
        inv_state = self.inventory.get_state(market.market_id, fv)
        up_size, down_size = self.inventory.compute_size_adjustment(
            market.market_id, fv, self.quote_engine.max_order_size
        )

        # 10.5 Enforce Close-Only quoting during near-expiry phases
        if phase in ["FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE"]:
            # Only allow quoting the side that reduces the imbalance
            if imbalance > 0:
                up_size = 0
                down_size = min(self.quote_engine.max_order_size, int(imbalance))
            elif imbalance < 0:
                down_size = 0
                up_size = min(self.quote_engine.max_order_size, int(abs(imbalance)))
            else:
                up_size = 0
                down_size = 0

        # 11. Capital limit check
        blocks = self.inventory.check_capital_limit(market.market_id, fv)
        if blocks.get("block_yes"):
            up_size = 0
        if blocks.get("block_no"):
            down_size = 0

        # 11.5 Fetch live orderbook to prevent crossing the book
        book_up = await self.book_reader.get_book(market.token_id_up)
        book_down = await self.book_reader.get_book(market.token_id_down)
        best_ask_yes = None
        best_ask_no = None
        if book_up:
            best_ask_yes = book_up.best_ask
        if book_down:
            best_ask_no = book_down.best_ask

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
        )
        quotes.phase = phase

        # 13. Pre-trade checks
        fv_fresh = not self.fair_value_model.is_stale
        passed, failed_reasons = pre_trade_checks(fv, quotes, inv_state.value,
                                      fv_fresh, phase)
        if not passed:
            log.warning("pre_trade_failed", market=market.market_id, reasons=failed_reasons)
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, phase, remaining)
            return

        # 14. Update orders
        #     token_id_up = "Up" token, token_id_down = "Down" token
        await self.order_mgr.update_quotes(
            market_id=market.market_id,
            token_id_yes=market.token_id_up,
            token_id_no=market.token_id_down,
            quotes=quotes,
        )

        # 15. Process fills (handle both dry-run and live CLOB modes)
        fills = []
        if hasattr(self.order_mgr.executor, 'check_fills'):
            # Dry-run
            fills = self.order_mgr.executor.check_fills()
        elif hasattr(self.order_mgr.executor, 'get_fills'):
            # Live CLOB API
            try:
                raw_fills = await self.order_mgr.executor.get_fills(market.market_id)
                fills = self.order_mgr.executor.process_fills(raw_fills, self.inventory, market.market_id)
            except Exception as e:
                log.error("live_fill_check_error", error=str(e))

        for fill in fills:
            # Record in inventory
            self.inventory.record_fill(
                market.market_id, fill["side"],
                fill["size"], fill["price"], self.asset
            )
            # Record in P&L tracker (with rebate calculation)
            self.pnl.record_fill(
                size=fill["size"],
                price=fill["price"],
                side=fill["side"],
                asset=self.asset,
                market_id=market.market_id,
            )
            self.edge_tracker.record_fill(
                fill["side"], fill["price"], fv
            )

        # 15.5. Auto-merge check (live mode: reclaim USDC when balance is low)
        force_merge = False
        if remaining <= 30 and not getattr(self, '_has_done_30s_merge', False):
            force_merge = True
            self._has_done_30s_merge = True

        if self.balance_monitor:
            merge_result = await self.balance_monitor.check_and_merge(
                inventory_mgr=self.inventory,
                gasless_merger=self.gasless_merger,
                ctf_ops=self.ctf,
                pnl_tracker=self.pnl,
                force=force_merge
            )
            if merge_result.get("merged"):
                msg = "auto_merge_end_of_market" if force_merge else "auto_merge_during_trading"
                log.info(msg,
                         asset=self.asset,
                         pairs=merge_result["pairs_merged"],
                         usdc=f"${merge_result['usdc_recovered']:.2f}")

        # 16. Update dashboard
        self._update_dashboard(market, spot, fv, sigma, phase, remaining,
                                quotes, pos, imbalance, inv_state.value)

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
        
        state = {
            "asset": self.asset,
            "market_id": market.market_id,
            "slug": market.slug,
            "question": market.question,
            "start_price": start_price or 0,
            "spot_price": spot or 0,
            "raw_spot": raw_spot or 0,
            "chainlink_spread": getattr(self, 'chainlink_spread', 0),
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
            "inv_state": real_state.value,
            # P&L with rebates
            "net_trading_pnl": self.pnl.net_trading_pnl,
            "est_rebates": self.pnl.est_rebates,
            "net_pnl": self.pnl.net_pnl,
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
