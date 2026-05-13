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
from src.strategy.quote_engine import QuoteEngine
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

    Normal repair quotes only the light side. But a sub-minimum tail cannot be
    repaired directly on Polymarket because live orders must be at least
    min_order_size shares. For those dust tails, quote a small paired plan:
    top up the heavy/dust side to a 5-share multiple and quote the opposite side
    to the same resulting target.

    Example: Down 3 / Up 0 => imbalance=-3. Quote Down 7 and Up 10, so if both
    fill the book becomes Down 10 / Up 10 instead of carrying unrecoverable dust.
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
    """Return the maximum sane repair bid and why it was chosen.

    The old repair cap only allowed guaranteed-profitable pairs:
    ``new_side_price <= 1 - existing_side_price - edge``. That is correct in a
    calm market, but disastrous during a directional spike. If BTC rips up and
    we are heavy NO, refusing to buy YES above the profitable-pair cap leaves a
    naked wrong-way NO tail that can expire worthless and erase many previous
    good pairs.

    In wrong-way inventory, the rational cap is not the profitable-pair cap; it
    is the expected value of the hedge side. Buying YES below FV when we are
    heavy NO reduces expected loss even if the resulting pair has negative
    realized edge. Same symmetric logic for heavy YES while FV collapses.
    """
    side = (side or "").lower()
    fv = max(0.01, min(0.99, float(fair_value or 0.5)))
    profitable_cap = float(pos.max_profitable_repair_price(side, size, min_edge=min_edge))

    # Buying YES repairs heavy NO. If FV is above 50%, NO is the wrong-way tail;
    # cap repair by YES expected value instead of insisting on guaranteed edge.
    if side in ("yes", "up") and pos.share_imbalance() < 0 and fv > 0.50:
        risk_cap = max(0.0, fv - float(adverse_buffer or 0))
        return max(profitable_cap, risk_cap), "adverse_no_tail"

    # Buying NO repairs heavy YES. If FV is below 50%, YES is the wrong-way tail.
    if side in ("no", "down") and pos.share_imbalance() > 0 and fv < 0.50:
        risk_cap = max(0.0, (1.0 - fv) - float(adverse_buffer or 0))
        return max(profitable_cap, risk_cap), "adverse_yes_tail"

    return profitable_cap, "pair_edge"


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
        self.stop_reason: str | None = None
        self._last_close_only_repair_mode: str | None = None
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
                        if self.gasless_merger and getattr(self.gasless_merger, "_signature_type", 0) == 3:
                            await self.gasless_merger.ensure_deposit_wallet_trading_approvals(
                                collateral_token=collateral_token,
                            )
                        sync_balance = getattr(self.order_mgr.executor, "sync_balance_allowance", None)
                        if callable(sync_balance):
                            sync_ok = await sync_balance()
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
        self._has_done_30s_merge = False
        self._repair_mode_started_at = None
        start_price = None
        binance_start_price = None

        log.info("initializing_new_market", asset=self.asset, slug=market.slug)
        spot = getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0)
        self._update_dashboard(market, spot, 0, 0, "INITIALIZING", market.time_remaining)

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
                self.vol_estimator.update(raw_spot, _time.time())
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
            elapsed = _time.time() - market.event_start_ts
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

    async def _quote_cycle(self, market: MarketInfo):
        """Single quote cycle iteration."""
        now = _time.time()
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

        # Balance-only mode: last 5 minutes of the window.
        # Goal: stop building the heavy side and quote ONLY to repair inventory.
        balance_only = remaining <= 300
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

        # 9. Toxicity monitor
        self.toxicity_monitor.update_delayed_mids(fv)
        self.toxicity_monitor.adjust_spread(self.quote_engine)
        if not is_halted and self.toxicity_monitor.check_kill_switch(self.edge_tracker):
            is_halted = True
            halt_reason = "TOXICITY_HALT"

        # 10. Compute inventory state and sizes
        #     Uses SHARE COUNT imbalance (Up - Down), not dollar delta
        #     Pass t_normalized for time-aware dynamic thresholds
        imbalance = pos.share_imbalance()
        abs_imbalance = abs(imbalance)
        # Treat any leftover as actionable inventory risk. If one side filled and
        # the other did not, quote ONLY the light side until balanced again.
        inventory_repair = abs_imbalance >= min_order_size
        dust_normalization = 0 < abs_imbalance < min_order_size
        close_only_phase = phase in ["FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE"]
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
            up_size, down_size, repair_mode = compute_inventory_repair_sizes(
                imbalance,
                min_order_size,
                self.quote_engine.max_order_size,
            )
            log.info(
                "sub_minimum_repair_quote",
                market=market.market_id,
                imbalance=round(imbalance, 4),
                up_size=up_size,
                down_size=down_size,
                mode=repair_mode,
            )
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
                up_size = 0
                down_size = min_order_size if abs_imbalance < min_order_size else _repair_size(min(self.quote_engine.max_order_size, int(abs_imbalance)))
            elif imbalance < 0:
                down_size = 0
                up_size = min_order_size if abs_imbalance < min_order_size else _repair_size(min(self.quote_engine.max_order_size, int(abs_imbalance)))
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

            # If halted and flat, stop quoting entirely
            if is_halted and up_size == 0 and down_size == 0:
                await self.order_mgr.cancel_market_quotes(market.market_id)
                self._update_dashboard(market, spot, fv, sigma, halt_reason, remaining)
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
                await self.order_mgr.cancel_all()
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

        # 11.5 Fetch live orderbooks in one request to prevent crossing the book.
        books = await self.book_reader.get_books([market.token_id_up, market.token_id_down])
        book_up = books.get(market.token_id_up)
        book_down = books.get(market.token_id_down)
        best_ask_yes = None
        best_ask_no = None
        best_bid_yes = None
        best_bid_no = None
        if book_up:
            best_ask_yes = book_up.best_ask
            best_bid_yes = book_up.best_bid
        if book_down:
            best_ask_no = book_down.best_ask
            best_bid_no = book_down.best_bid

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

        # Directional spike guard: do not run normal two-sided rebate quoting
        # when the binary is already strongly directional. In these states the
        # cheap/out-of-favor side gets filled first and becomes exactly the
        # wrong-way tail Vinit saw during BTC spikes. If we already have a tail,
        # close-only repair can still run; flat/normal quoting must stand down.
        if repair_mode == "normal" and (fv >= 0.65 or fv <= 0.35):
            log.warning(
                "normal_quote_blocked_directional_extreme",
                asset=self.asset,
                fair_value=round(fv, 4),
                yes_size=quotes.yes_buy_size,
                no_size=quotes.no_buy_size,
            )
            quotes.yes_buy_size = 0
            quotes.no_buy_size = 0

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

            # Normal/balanced quoting is atomic: both sides or neither. Capital
            # scaling/backoff must never turn a balanced market into a one-sided
            # bet. This exact failure produced mode=normal yes_size=5/no_size=0.
            if repair_mode == "normal" and abs_imbalance < min_order_size:
                one_sided_normal = (quotes.yes_buy_size > 0) != (quotes.no_buy_size > 0)
                merge_blocked = self._merge_unavailable_until > _time.time()
                if one_sided_normal or merge_blocked:
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

        # Pair-cost guardrail for close-only repair. Existing unmatched fills are
        # sunk. In calm markets, cap repair bids to preserve at least a 1c edge
        # on newly formed pairs. In directional wrong-way inventory, relax that
        # cap up to the hedge side's expected value so the bot can reduce loss
        # instead of holding a naked tail to expiry.
        if repair_mode == "repair_up" and quotes.yes_buy_size > 0:
            cap, cap_reason = repair_price_cap(pos, "yes", quotes.yes_buy_size, fv, min_edge=0.01)
            if cap < 0.01:
                log.warning("repair_quote_blocked_negative_pair_edge",
                            market=market.market_id[:8], side="yes",
                            quoted=quotes.yes_buy_price, cap=round(cap, 4),
                            cap_reason=cap_reason)
                quotes.yes_buy_size = 0
            elif quotes.yes_buy_price and quotes.yes_buy_price > cap:
                log.warning("repair_quote_capped_for_pair_edge",
                            market=market.market_id[:8], side="yes",
                            quoted=quotes.yes_buy_price, cap=round(cap, 4),
                            cap_reason=cap_reason)
                quotes.yes_buy_price = round(cap, 2)
        elif repair_mode == "repair_down" and quotes.no_buy_size > 0:
            cap, cap_reason = repair_price_cap(pos, "no", quotes.no_buy_size, fv, min_edge=0.01)
            if cap < 0.01:
                log.warning("repair_quote_blocked_negative_pair_edge",
                            market=market.market_id[:8], side="no",
                            quoted=quotes.no_buy_price, cap=round(cap, 4),
                            cap_reason=cap_reason)
                quotes.no_buy_size = 0
            elif quotes.no_buy_price and quotes.no_buy_price > cap:
                log.warning("repair_quote_capped_for_pair_edge",
                            market=market.market_id[:8], side="no",
                            quoted=quotes.no_buy_price, cap=round(cap, 4),
                            cap_reason=cap_reason)
                quotes.no_buy_price = round(cap, 2)
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

        # 15. Process fills (handle both dry-run and live CLOB modes)
        fills = []
        if hasattr(self.order_mgr.executor, 'check_fills'):
            # Dry-run (book-based)
            fills = self.order_mgr.executor.check_fills(
                yes_book_snapshot=book_up,
                no_book_snapshot=book_down,
            )
        elif hasattr(self.order_mgr.executor, 'get_fills'):
            # Live CLOB API
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

            # 15.1 FILL-REACTIVE REPRICING
            # After a fill creates imbalance, immediately cancel the OPPOSITE
            # side's stale quote so the next cycle places a fresh, competitive
            # bid. Without this, the stale opposite-side quote sits at an
            # unattractive price and never fills, causing one-sided inventory.
            active = self.order_mgr.get_active(market.market_id)
            # After a fill, immediately cancel any remaining quote on the
            # FILLED/now-heavier side. Keep the opposite light-side quote alive
            # because that is the order needed to repair imbalance. The previous
            # logic cancelled the opposite side and live kept buying the heavy
            # side — exactly the divergence Vinit observed.
            if fill["side"] in ("no", "down") and active.no_order_id:
                cancelled = await self.order_mgr.executor.cancel_order(active.no_order_id)
                if cancelled:
                    active.no_order_id = None
                    active.no_price = None
                    active.no_size = 0
                    log.debug("fill_reactive_reprice", cancelled="no",
                              trigger_side="no", imbalance=pos.share_imbalance())
                else:
                    self.stop_reason = "fill_reactive_cancel_failed:no"
                    log.error("fill_reactive_cancel_failed",
                              side="no", market=market.market_id[:8])
                    self._running = False
                    return
            elif fill["side"] in ("yes", "up") and active.yes_order_id:
                cancelled = await self.order_mgr.executor.cancel_order(active.yes_order_id)
                if cancelled:
                    active.yes_order_id = None
                    active.yes_price = None
                    active.yes_size = 0
                    log.debug("fill_reactive_reprice", cancelled="yes",
                              trigger_side="yes", imbalance=pos.share_imbalance())
                else:
                    self.stop_reason = "fill_reactive_cancel_failed:yes"
                    log.error("fill_reactive_cancel_failed",
                              side="yes", market=market.market_id[:8])
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
        
        state = {
            "asset": self.asset,
            "market_id": "waiting...",
            "slug": "Waiting for next Polymarket window...",
            "question": "",
            "start_price": 0,
            "spot_price": spot,
            "raw_spot": spot,
            "chainlink_spread": 0,
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
            "inv_state": "WAITING",
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
