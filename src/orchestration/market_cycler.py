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
from src.execution.ctf_ops import CTFOperations, GaslessMerger, BalanceMonitor
from src.risk.regime_filter import RegimeFilter
from src.risk.toxicity import FillEdgeTracker, ToxicityMonitor
from src.risk.risk_engine import (RiskEngine, determine_phase,
                                   apply_phase_params, pre_trade_checks)
from src.monitoring.pnl_tracker import PnLTracker
from src.monitoring.logger import get_logger
from src.orchestration.quote_cycle import (
    QuoteCycleContext,
    decide_basis_risk,
    decide_inventory_risk,
    decide_negative_pair_edge,
    decide_stale_spot,
    package_book_snapshot,
    package_fair_value_result,
)
from src.orchestration.small_capital import SmallCapitalLifecycle
from src.orchestration.dashboard_state import (
    clear_dashboard_event,
    dashboard_sigma_for_stale_spot,
    set_dashboard_event,
    update_dashboard,
    update_dashboard_waiting,
)
from src.orchestration.settlement import (
    settle_market,
    wait_and_settle_unmatched,
    wait_and_settle_unmatched_by_fields,
)

log = get_logger("market_cycler")

# Roadmap extraction: strategy helpers now live in service packages.  They are
# imported here for backward compatibility with existing tests/callers while
# MarketCycler is gradually reduced to lifecycle orchestration.
PRE_EXPIRY_AUTO_MERGE_SECONDS = 120

from src.services.fair_value import (
    BASIS_GUARD_MAX_FV_DEVIATION,
    FAST_ADVERSE_CANCEL_MIN_EDGE,
    MAX_EXNESS_PRICE_AGE_SECONDS,
    MAX_SPOT_PRICE_AGE_SECONDS,
    MAX_TRADING_FV_MARKET_DEVIATION,
    FairValueEngine,
    FairValueInputs,
    UpDownFairValue,
    apply_fast_feed_confidence_floor,
    basis_guard_triggered,
    blended_fair_value,
    cap_fair_value_to_market,
    clamp_probability,
    fv_model_confidence,
    polymarket_implied_up_mid,
    spot_from_binary_probability,
    start_price_disagrees_with_market,
)
from src.services.inventory import (
    aggressive_repair_price,
    apply_dust_price_guardrails,
    balanced_repair_debt_eligible,
    compute_fv_aware_dust_repair_sizes,
    compute_inventory_repair_sizes,
    has_negative_matched_pair_edge,  # compatibility re-export; live cycler uses quote_cycle decisions
    plan_balanced_negative_edge_repair,
    plan_repair_price_cap,
    repair_min_edge_for_remaining,
    repair_price_cap,  # compatibility re-export; live repair planning owns behavior
)
from src.services.quoting import (
    FV_FAVORED_ENTRY_MAX_SIZE,
    FV_FAVORED_ENTRY_MIN_EDGE,
    FV_FAVORED_ENTRY_STOP_SECONDS,
    FV_FAVORED_ENTRY_THRESHOLD,
    MIN_LIVE_PAIR_EDGE,
    QuotePolicy,
    apply_directional_market_guard,
    apply_fv_favored_entry_mode,
    apply_pair_cost_precheck,
    normalize_quote_sizes,
    repair_size_or_zero,
)


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
                 balance_monitor: Optional[BalanceMonitor] = None,
                 small_capital_config=None,
                 balanced_repair_config=None):

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
        self.small_capital_config = small_capital_config
        self.balanced_repair_config = balanced_repair_config
        self.small_capital = SmallCapitalLifecycle(self)
        log.info(
            "market_cycler_small_capital_mode",
            asset=self.asset,
            enabled=self._small_capital_enabled(),
            configured=bool(small_capital_config),
            one_cycle_per_window=bool(getattr(small_capital_config, "one_cycle_per_window", False)),
            max_shares_per_order=int(getattr(small_capital_config, "max_shares_per_order", 0) or 0),
        )
        
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
        self.last_sigma: Optional[float] = None
        self.start_price_source: str = "unknown"
        self._last_vatic_retry_ts: float = 0.0
        self.stop_reason: str | None = None
        self._last_close_only_repair_mode: str | None = None
        self._last_toxicity_repair_override_log: float = 0.0
        self._merge_unavailable_until: float = 0.0
        self._dashboard_event: dict = {}
        self._wallet_truth_by_market: dict[str, dict] = {}

        self._running = False
        self._last_market_slug = None  # Track to detect new market
        self._quote_event = asyncio.Event()

    def _set_dashboard_event(self, level: str, reason: str, detail: str = "") -> None:
        set_dashboard_event(self, level, reason, detail)

    def _clear_dashboard_event(self) -> None:
        clear_dashboard_event(self)

    def notify_price_update(self):
        """Wake the quote loop on a fresh price tick, with rate limit in loop."""
        if self._running and self.current_market:
            self._quote_event.set()

    def _small_capital_lifecycle(self) -> SmallCapitalLifecycle:
        lifecycle = getattr(self, "small_capital", None)
        if lifecycle is None:
            lifecycle = SmallCapitalLifecycle(self)
            self.small_capital = lifecycle
        return lifecycle

    def _small_capital_enabled(self) -> bool:
        return self._small_capital_lifecycle()._small_capital_enabled()

    def _should_pre_expiry_auto_merge(self, remaining: float, matched_pairs: int) -> bool:
        return (
            remaining <= PRE_EXPIRY_AUTO_MERGE_SECONDS
            and not getattr(self, "_has_done_pre_expiry_merge", False)
        )

    def _wallet_truth_snapshot(self, wallet_truth) -> dict | None:
        return self._small_capital_lifecycle()._wallet_truth_snapshot(wallet_truth)

    def _apply_wallet_truth_to_small_capital_state(self, market_id: str, wallet_snapshot: dict | None) -> None:
        return self._small_capital_lifecycle()._apply_wallet_truth_to_small_capital_state(market_id, wallet_snapshot)

    async def _maybe_pre_expiry_auto_merge(self, market: MarketInfo, pos, remaining: float, wallet_truth=None) -> dict:
        wallet_pairs = 0
        if wallet_truth is not None:
            wallet_pairs = int(min(float(wallet_truth[0] or 0), float(wallet_truth[1] or 0)))
        matched_pairs = max(int(pos.matched_pairs() or 0), wallet_pairs)
        if not self._should_pre_expiry_auto_merge(remaining, matched_pairs):
            return {"checked": False, "merged": False, "pairs_merged": 0, "reason": "not_due"}
        last_attempt = float(getattr(self, "_last_pre_expiry_merge_attempt_ts", 0.0) or 0.0)
        now = _time.time()
        if now - last_attempt < 10.0:
            return {"checked": False, "merged": False, "pairs_merged": 0, "reason": "retry_throttled"}
        self._last_pre_expiry_merge_attempt_ts = now

        log.info(
            "pre_expiry_auto_merge_triggered",
            asset=self.asset,
            remaining=round(remaining, 1),
            local_pairs=int(pos.matched_pairs() or 0),
            wallet_pairs=wallet_pairs,
            matched_pairs=matched_pairs,
        )
        if not self.balance_monitor:
            log.warning("pre_expiry_auto_merge_unavailable", asset=self.asset, reason="no_balance_monitor")
            return {"checked": False, "merged": False, "pairs_merged": 0, "reason": "no_balance_monitor"}

        # Always use the BalanceMonitor path. It performs the authoritative
        # on-chain YES/NO balance preflight, expands stale local inventory to
        # chain-confirmed pairs, handles deposit-wallet relayer approvals, then
        # retries CLOB balance/allowance sync after merge. Direct merge calls
        # bypassed too much of that machinery and could leave recovered pUSD
        # invisible to trading.
        merge_result = await self.balance_monitor.check_and_merge(
            inventory_mgr=self.inventory,
            gasless_merger=self.gasless_merger,
            ctf_ops=self.ctf,
            pnl_tracker=self.pnl,
            force=True,
            balance_sync=getattr(self.order_mgr.executor, "sync_balance_allowance", None),
        )
        if merge_result.get("merged"):
            self._has_done_pre_expiry_merge = True
            log.info(
                "auto_merge_pre_expiry",
                asset=self.asset,
                reason="pre_expiry_2m",
                pairs=merge_result.get("pairs_merged", 0),
                usdc=f"${float(merge_result.get('usdc_recovered', 0) or 0):.2f}",
            )
            self._set_dashboard_event(
                "info",
                "PRE_EXPIRY_AUTO_MERGE",
                f"merged {merge_result.get('pairs_merged', 0)} pairs; synced tradeable balance",
            )
            if self.inventory.capital_arbiter:
                self.inventory.capital_arbiter.record_recovery(
                    self.asset, float(merge_result.get("usdc_recovered", 0) or 0)
                )
        else:
            log.warning(
                "pre_expiry_auto_merge_noop",
                asset=self.asset,
                local_pairs=int(pos.matched_pairs() or 0),
                wallet_pairs=wallet_pairs,
                result=merge_result,
            )
        return merge_result

    def _small_capital_balancing_side(self, market_id: str, pos, wallet_imbalance: float | None = None) -> str:
        return self._small_capital_lifecycle()._small_capital_balancing_side(market_id, pos, wallet_imbalance)

    def _apply_small_capital_balancing_override(self, market_id: str, pos, quotes, repair_mode: str, min_order_size: int, wallet_imbalance: float | None = None) -> str:
        return self._small_capital_lifecycle()._apply_small_capital_balancing_override(market_id, pos, quotes, repair_mode, min_order_size, wallet_imbalance)

    def _apply_small_capital_opening_one_side(self, market_id: str, quotes, repair_mode: str, fair_value: float) -> str | None:
        return self._small_capital_lifecycle()._apply_small_capital_opening_one_side(market_id, quotes, repair_mode, fair_value)

    def _small_capital_saved_repair_cap(self, state: dict, repair_side: str, min_edge: float) -> float | None:
        return self._small_capital_lifecycle()._small_capital_saved_repair_cap(state, repair_side, min_edge)

    def _small_capital_emergency_hedge_cap(self, state: dict, repair_side: str) -> tuple[float | None, bool, float]:
        return self._small_capital_lifecycle()._small_capital_emergency_hedge_cap(state, repair_side)

    async def _wallet_position_truth(self, market: MarketInfo) -> tuple[float, float] | None:
        return await self._small_capital_lifecycle()._wallet_position_truth(market)

    async def _refresh_wallet_truth_for_market(self, market: MarketInfo) -> tuple[tuple[float, float] | None, dict | None]:
        return await self._small_capital_lifecycle()._refresh_wallet_truth_for_market(market)

    def _small_capital_state(self, market_id: str) -> dict:
        return self._small_capital_lifecycle()._small_capital_state(market_id)

    def _save_small_capital_state(self, market_id: str, state: dict):
        return self._small_capital_lifecycle()._save_small_capital_state(market_id, state)

    def _repair_small_capital_unfilled_opening_state(self, market_id: str, state: dict, has_resting_opening_quote: bool, matched_pairs: int) -> bool:
        return self._small_capital_lifecycle()._repair_small_capital_unfilled_opening_state(market_id, state, has_resting_opening_quote, matched_pairs)

    def _small_capital_should_hold_opening_quote(self, state: dict, has_resting_opening_quote: bool, matched_pairs: int) -> bool:
        return self._small_capital_lifecycle()._small_capital_should_hold_opening_quote(state, has_resting_opening_quote, matched_pairs)

    def _apply_small_capital_opening_reprice_guard(self, market_id: str, quotes, state: dict, min_order_size: int) -> str:
        return self._small_capital_lifecycle()._apply_small_capital_opening_reprice_guard(market_id, quotes, state, min_order_size)

    def _small_capital_opening_spent(self, state: dict) -> bool:
        return self._small_capital_lifecycle()._small_capital_opening_spent(state)

    async def _cancel_fast_adverse_active_quotes(self, market: MarketInfo, fast_fv: float,
                                                min_edge: float = FAST_ADVERSE_CANCEL_MIN_EDGE) -> bool:
        """Cancel active bids immediately when fast-feed FV removes their edge.

        This is intentionally earlier than the normal cancel/reprice path. The
        normal path waits for book fetch + quote generation and can defer
        touched/crossed bids; adverse-selection protection should not wait.
        """
        if not self.current_market or market is None:
            return False
        active = self.order_mgr.get_active(market.market_id)
        fv = max(0.0, min(1.0, float(fast_fv or 0.5)))
        min_edge = max(0.0, float(min_edge or 0.0))
        cancelled = False

        yes_price = float(active.yes_price or 0.0)
        if active.yes_order_id and yes_price > 0 and (fv - yes_price) < min_edge:
            log.warning(
                "fast_adverse_yes_cancel",
                asset=self.asset,
                market=market.market_id[:8],
                fast_fv=round(fv, 4),
                active_price=round(yes_price, 4),
                edge=round(fv - yes_price, 4),
                min_edge=round(min_edge, 4),
            )
            ok = bool(await self.order_mgr.cancel_side_quotes(
                market.market_id, "yes", market.token_id_up
            ))
            if not ok:
                self.stop_reason = "fast_adverse_yes_cancel_failed"
                self._running = False
                return True
            cancelled = True

        no_price = float(active.no_price or 0.0)
        no_fair = 1.0 - fv
        if active.no_order_id and no_price > 0 and (no_fair - no_price) < min_edge:
            log.warning(
                "fast_adverse_no_cancel",
                asset=self.asset,
                market=market.market_id[:8],
                fast_fv=round(fv, 4),
                active_price=round(no_price, 4),
                edge=round(no_fair - no_price, 4),
                min_edge=round(min_edge, 4),
            )
            ok = bool(await self.order_mgr.cancel_side_quotes(
                market.market_id, "no", market.token_id_down
            ))
            if not ok:
                self.stop_reason = "fast_adverse_no_cancel_failed"
                self._running = False
                return True
            cancelled = True

        return cancelled

    async def _small_capital_fail_closed_before_quotes(self, market: MarketInfo, pos, wallet_snapshot: dict | None, fv: float, sigma: float, remaining: float) -> bool:
        return await self._small_capital_lifecycle()._small_capital_fail_closed_before_quotes(market, pos, wallet_snapshot, fv, sigma, remaining)

    def _mark_small_capital_quote_started(self, market: MarketInfo, quotes, repair_mode: str):
        return self._small_capital_lifecycle()._mark_small_capital_quote_started(market, quotes, repair_mode)

    def _small_capital_record_fills(self, market: MarketInfo, fills: list[dict]):
        return self._small_capital_lifecycle()._small_capital_record_fills(market, fills)

    async def _small_capital_maybe_stop_completed(self, market: MarketInfo, pos, reason: str = "", wallet_snapshot: dict | None = None) -> bool:
        return await self._small_capital_lifecycle()._small_capital_maybe_stop_completed(market, pos, reason, wallet_snapshot)

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
        await settle_market(self)

    async def _wait_and_settle_unmatched(self, market: MarketInfo, pos_snapshot: dict):
        await wait_and_settle_unmatched(self, market, pos_snapshot)

    async def _wait_and_settle_unmatched_by_fields(self, asset: str, slug: str,
                                                   window_start_ts: int,
                                                   market_id: str,
                                                   pos_snapshot: dict):
        await wait_and_settle_unmatched_by_fields(
            self, asset, slug, window_start_ts, market_id, pos_snapshot
        )

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
        self._has_done_pre_expiry_merge = False
        self._last_pre_expiry_merge_attempt_ts = 0.0
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
        
        # 3. Fallback: Chainlink on-chain aggregator before any synthetic value.
        if not start_price:
            start_price = await self.price_feed.fetch_chainlink_price(
                self.ac.symbol, market.event_start_ts
            )
            if start_price:
                start_price_source = "chainlink"
                log.info("start_price_from_chainlink",
                         asset=self.asset, price=start_price)

        # Do not use Binance/adjusted/current spot as price-to-beat. If Vatic
        # and Chainlink are unavailable, fail closed and retry the market loop;
        # showing spot as the strike is worse than not quoting.
        if not start_price:
            log.error(
                "price_to_beat_unavailable",
                asset=self.asset,
                market=market.slug,
                msg="Vatic/Chainlink strike unavailable; refusing Binance/spot fallback",
            )
            self.stop_reason = "price_to_beat_unavailable"
            self._update_dashboard(market, spot, 0, 0, "NO_STRIKE", market.time_remaining)
            return
            
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

        # Vatic/Chainlink are the price-to-beat source of truth; do not replace
        # them with market-calibrated or spot-derived values.
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
            self._small_capital_record_fills(market, fills)
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

    def _quote_cycle_context(self, market: MarketInfo, now: float | None = None) -> QuoteCycleContext:
        return QuoteCycleContext.from_market(market, now=_time.time() if now is None else now)

    def _package_book_snapshot(self, books, market: MarketInfo):
        return package_book_snapshot(books, market)

    def _package_fair_value_result(self, fv_result, polymarket_mid_up):
        return package_fair_value_result(fv_result, polymarket_mid_up)

    async def _quote_cycle(self, market: MarketInfo):
        """Single quote cycle iteration."""
        ctx = self._quote_cycle_context(market)
        now = ctx.now
        remaining = ctx.remaining

        # 1. Get live spot price. Prefer Exness/MT5 bridge when configured;
        # it has tracked the Polymarket oracle faster than Binance in live tests.
        if hasattr(self.price_feed, "fetch_mt5_bridge_price"):
            await self.price_feed.fetch_mt5_bridge_price(self.ac.symbol)
        raw_spot = self.price_feed.get_price(self.ac.symbol)
        price_age = self.price_feed.get_price_age(self.ac.symbol)
        price_source = (self.price_feed.get_price_source(self.ac.symbol)
                        if hasattr(self.price_feed, "get_price_source") else "unknown")

        # Binance websocket stalls are especially toxic for 15m binaries: a
        # frozen spot produces a frozen fair value while the market keeps
        # moving. Try one REST refresh before failing closed/canceling quotes.
        mt5_configured = bool(getattr(self.price_feed, "mt5_bridge_url", ""))
        max_spot_age = self._max_spot_price_age_seconds()
        if ((not raw_spot) or price_age > max_spot_age) and not mt5_configured:
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
                price_source = (self.price_feed.get_price_source(self.ac.symbol)
                                if hasattr(self.price_feed, "get_price_source") else "unknown")

        stale_spot_decision = decide_stale_spot(raw_spot, price_age, max_spot_age)
        if stale_spot_decision.should_stop and stale_spot_decision.dashboard_reason == "NO_SPOT":
            mt5_status = (
                self.price_feed.get_mt5_bridge_status(self.ac.symbol)
                if hasattr(self.price_feed, "get_mt5_bridge_status")
                else {}
            )
            mt5_detail = ""
            if mt5_configured:
                host = mt5_status.get("host") or "unknown_host"
                last_error = mt5_status.get("last_error") or "no_response"
                attempts = mt5_status.get("attempts", 0)
                failures = mt5_status.get("failures", 0)
                timeout = mt5_status.get("timeout_seconds")
                mt5_detail = f"; mt5 host={host} error={last_error} attempts={attempts} failures={failures} timeout={timeout}s"
            log.warning("no_spot_price", symbol=self.ac.symbol, mt5_bridge=mt5_status)
            self._set_dashboard_event("skip", "NO_SPOT_PRICE", f"{self.ac.symbol} unavailable{mt5_detail}")
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, 0, self.last_fair_value or 0, self._dashboard_sigma_for_stale_spot(), "NO_SPOT", remaining)
            return

        if stale_spot_decision.should_stop and stale_spot_decision.dashboard_reason == "STALE_SPOT":
            log.warning(
                "spot_price_stale_stop_quoting",
                asset=self.asset,
                symbol=self.ac.symbol,
                raw_binance_spot=round(raw_spot, 4),
                price_age=round(price_age, 3),
                max_age=max_spot_age,
            )
            self._set_dashboard_event(
                "skip",
                "STALE_SPOT",
                stale_spot_decision.event_message or f"age {price_age:.2f}s > max {max_spot_age:.2f}s",
            )
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(
                market,
                raw_spot,
                self.last_fair_value or 0,
                self._dashboard_sigma_for_stale_spot(),
                "STALE_SPOT",
                remaining,
            )
            return

        if (getattr(self, "_dashboard_event", {}) or {}).get("event_reason") == "STALE_SPOT":
            self._clear_dashboard_event()
            
        spot = raw_spot
        log.info(
            "spot_feed_snapshot",
            asset=self.asset,
            symbol=self.ac.symbol,
            raw_binance_spot=round(raw_spot, 4),
            live_spot=round(spot, 4),
            spread=round(self.chainlink_spread, 4),
            price_age=round(price_age, 3),
            price_source=price_source,
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

        # Never set price-to-beat from live/adjusted spot in quote cycles. The
        # strike must come from Vatic/Chainlink initialization or retry above.
        if self.fair_value_model and not self.fair_value_model.start_price:
            log.warning("missing_authoritative_start_price", asset=self.asset)
            self._set_dashboard_event("skip", "NO_STRIKE", "missing authoritative Vatic/Chainlink start price")
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, 0, 0, "NO_STRIKE", remaining)
            return

        # 2. Update volatility
        self.vol_estimator.update(spot, now)
        sigma = self.vol_estimator.sigma_for_model()
        self.last_sigma = sigma

        t_norm = self.fair_value_model.normalized_time(now)
        total_window = max(1.0, self.fair_value_model.resolve_ts - self.fair_value_model.event_start_ts)
        elapsed_fraction = max(0.0, min(1.0, (now - self.fair_value_model.event_start_ts) / total_window))

        if price_source == "exness_mt5":
            fast_model_fv = self.fair_value_model.fair_value(spot, sigma, now, update_state=False)
            if await self._cancel_fast_adverse_active_quotes(market, fast_model_fv):
                self._set_dashboard_event("skip", "FAST_ADVERSE_CANCEL", "fast Exness FV removed active quote edge")
                self._update_dashboard(market, spot, fast_model_fv, sigma, "FAST_ADVERSE_CANCEL", remaining)
                return

        # Fetch Polymarket books early so every dashboard/early-return path uses
        # the same authoritative blended FV. Previously, early returns displayed
        # raw/model FV while the UI/book price was already far away (e.g. UP 15c
        # but dashboard stuck near 54c).
        books = await self.book_reader.get_books([market.token_id_up, market.token_id_down])
        book_snapshot = self._package_book_snapshot(books, market)
        book_up = book_snapshot.book_up
        book_down = book_snapshot.book_down
        best_ask_yes = book_snapshot.best_ask_yes
        best_bid_yes = book_snapshot.best_bid_yes
        best_ask_no = book_snapshot.best_ask_no
        best_bid_no = book_snapshot.best_bid_no
        polymarket_mid_up = book_snapshot.polymarket_mid_up

        # Dynamic live oracle/Polymarket spot estimate. Only use this to adjust
        # Binance/fallback feeds. If Exness/MT5 is active, it is the primary live
        # spot and must not be overwritten by book-implied spot.
        market_implied_spot = spot_from_binary_probability(
            self.fair_value_model.start_price,
            polymarket_mid_up,
            sigma,
            remaining,
        )
        if (price_source != "exness_mt5"
                and market_implied_spot
                and abs(market_implied_spot - raw_spot) <= 300):
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
            if price_source == "exness_mt5" and market_implied_spot:
                log.info(
                    "live_spot_uses_exness_primary",
                    asset=self.asset,
                    exness_spot=round(raw_spot, 4),
                    market_implied_spot=round(market_implied_spot, 4),
                    market_fv=(round(polymarket_mid_up, 4) if polymarket_mid_up is not None else None),
                )

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

        fv_result = FairValueEngine(self.fair_value_model).compute(
            FairValueInputs(
                spot=spot,
                sigma=sigma,
                now_ts=now,
                elapsed_fraction=elapsed_fraction,
                standardized_move=standardized_move,
                market_fv=polymarket_mid_up,
                price_source=price_source,
            ),
            update_state=False,
        )
        fv_package = self._package_fair_value_result(fv_result, polymarket_mid_up)
        model_fv = fv_package.model_fv
        model_confidence = fv_package.model_confidence
        uncapped_fv = fv_package.uncapped_fv
        fv = fv_package.tradable_fv
        if polymarket_mid_up is not None and abs(float(fv or 0) - float(uncapped_fv or 0)) >= 0.0001:
            log.warning(
                "fair_value_market_deviation_capped",
                asset=self.asset,
                uncapped_fv=round(float(uncapped_fv or 0), 4),
                capped_fv=round(float(fv or 0), 4),
                market_fv=round(float(polymarket_mid_up or 0), 4),
                max_deviation=MAX_TRADING_FV_MARKET_DEVIATION,
                msg="model FV too far from current Polymarket UP price; capping tradable FV",
            )
        self.last_fair_value = fv
        # The final blended FV is the authoritative trading FV. Refresh the
        # model freshness timestamp here; otherwise pre_trade_checks sees the
        # model as stale because raw model_fv is intentionally computed with
        # update_state=False for dashboard/market blending.
        self.fair_value_model._last_fair_value = fv
        self.fair_value_model._last_update_ts = now
        if hasattr(self.order_mgr.executor, 'update_fair_value'):
            self.order_mgr.executor.update_fair_value(fv, spot)

        basis_delta = fv_package.basis_delta
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
            uncapped_fv=round(float(uncapped_fv or 0), 4),
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

        # Get inventory position early for the DEAD_ZONE check. Attach live CTF
        # identifiers for mid-market merge calls; persisted inventory only stores
        # market_id, but live merge needs condition id and ERC1155 token ids.
        pos = self.inventory.get_or_create(market.market_id, self.asset)
        pos.condition_id = getattr(market, "condition_id", None) or market.market_id
        pos.yes_token_id = str(getattr(market, "token_id_up", "") or "")
        pos.no_token_id = str(getattr(market, "token_id_down", "") or "")

        if not await self._sync_live_fills_before_quote(market, fv, pos):
            return

        wallet_truth, wallet_snapshot = await self._refresh_wallet_truth_for_market(market)
        wallet_imbalance: float | None = None
        if await self._small_capital_maybe_stop_completed(
            market, pos, "pre_quote_balanced", wallet_snapshot=wallet_snapshot
        ):
            return
        if await self._small_capital_fail_closed_before_quotes(
            market, pos, wallet_snapshot, fv, sigma, remaining
        ):
            return

        if wallet_truth is not None:
            wallet_yes, wallet_no = wallet_truth
            wallet_imbalance = wallet_yes - wallet_no
            local_imbalance = float(pos.share_imbalance() or 0)
            if abs(wallet_imbalance - local_imbalance) >= 0.5:
                log.warning(
                    "wallet_inventory_truth_diverged",
                    asset=self.asset,
                    market=market.market_id[:8],
                    local_imbalance=round(local_imbalance, 4),
                    wallet_imbalance=round(wallet_imbalance, 4),
                    wallet_yes=round(wallet_yes, 4),
                    wallet_no=round(wallet_no, 4),
                    msg="using wallet truth for quote side selection",
                )

        await self._maybe_pre_expiry_auto_merge(market, pos, remaining, wallet_truth=wallet_truth)

        negative_pair_edge = decide_negative_pair_edge(pos)
        if negative_pair_edge.triggered:
            pairs = negative_pair_edge.matched_pairs
            pair_pnl = round(negative_pair_edge.pair_pnl, 4)
            debt_eligible, debt_reason, debt_meta = balanced_repair_debt_eligible(
                pos, self.balanced_repair_config
            )
            if debt_eligible and not self._small_capital_enabled():
                log.warning(
                    "negative_pair_edge_balanced_repair_armed",
                    asset=self.asset,
                    market=market.market_id[:8],
                    matched_pairs=pairs,
                    pair_pnl=pair_pnl,
                    debt=debt_meta.get("debt"),
                    msg="Keeping negative-edge pairs open for balanced repair instead of immediate merge",
                )
                self._set_dashboard_event(
                    "warn",
                    "BALANCED_REPAIR_ARMED",
                    f"debt ${float(debt_meta.get('debt', 0) or 0):.2f}; waiting for profitable pairs",
                )
            else:
                condition_id = getattr(pos, "condition_id", None) or market.market_id
                log.warning(
                    "negative_pair_edge_recovery",
                    asset=self.asset,
                    market=market.market_id[:8],
                    matched_pairs=pairs,
                    pair_pnl=pair_pnl,
                    balanced_repair_reason=debt_reason,
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

        if phase == "DEAD_ZONE" and float(wallet_imbalance if wallet_imbalance is not None else pos.share_imbalance()) == 0:
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
        inventory_plan = decide_inventory_risk(
            float(wallet_imbalance if wallet_imbalance is not None else pos.share_imbalance()),
            min_order_size,
        )
        imbalance = inventory_plan.imbalance
        abs_imbalance = inventory_plan.abs_imbalance
        # Treat any leftover as actionable inventory risk. If one side filled and
        # the other did not, quote ONLY the light side until balanced again.
        inventory_repair = inventory_plan.inventory_repair
        dust_normalization = inventory_plan.dust_normalization
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
        sct_opening_spent = False
        if self._small_capital_enabled():
            sct_opening_spent = self._small_capital_opening_spent(
                self._small_capital_state(market.market_id)
            )

        # 10.25 Inventory repair / dust-normalization overrides normal quoting.
        # Guardrails:
        # - no unrelated normal two-sided quoting while carrying a tail
        # - dust mode is capped at 2x min size by compute_inventory_repair_sizes()
        # - do not open a two-sided dust plan during halts or close-only phases
        if sct_opening_spent and abs_imbalance > 0:
            # Small-capital mode is fail-closed after the opening attempt. Any
            # nonzero wallet/local inventory must be handled as strict
            # opposite-side repair. Do not use FV-aware dust/top-up logic during
            # sudden moves; it can resume normal-style quoting and deepen the
            # wrong-side inventory.
            up_size, down_size, repair_mode = compute_inventory_repair_sizes(
                imbalance,
                min_order_size,
                self.quote_engine.max_order_size,
            )
            log.warning(
                "small_capital_strict_repair_after_opening_spent",
                asset=self.asset,
                market=market.market_id[:8],
                imbalance=round(imbalance, 4),
                up_size=up_size,
                down_size=down_size,
                mode=repair_mode,
            )
        elif dust_normalization and not is_halted and not close_only_phase:
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
                    down_size = repair_size_or_zero(min(self.quote_engine.max_order_size, int(abs_imbalance)), min_order_size)
                    repair_mode = "repair_down"
            elif imbalance < 0:
                if abs_imbalance < min_order_size:
                    up_size, down_size, repair_mode = compute_fv_aware_dust_repair_sizes(
                        imbalance, fv, min_order_size, self.quote_engine.max_order_size)
                else:
                    down_size = 0
                    up_size = repair_size_or_zero(min(self.quote_engine.max_order_size, int(abs_imbalance)), min_order_size)
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
        up_size, down_size = normalize_quote_sizes(
            up_size,
            down_size,
            min_order_size,
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
        basis_risk_decision = decide_basis_risk(
            repair_mode=repair_mode,
            balance_only=balance_only,
            is_halted=is_halted,
            model_fv=model_fv,
            polymarket_mid_up=polymarket_mid_up,
            abs_imbalance=abs_imbalance,
            min_order_size=min_order_size,
        )
        if basis_risk_decision.triggered:
            if basis_risk_decision.action == "close_only":
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
        quote_policy = QuotePolicy()
        quotes.phase = phase
        quotes = apply_dust_price_guardrails(
            quotes,
            repair_mode,
            best_ask_yes=best_ask_yes,
            best_ask_no=best_ask_no,
        )

        # Balanced repair: if previous matched pairs locked a loss but current
        # market prices offer a clearly profitable YES+NO pair, add equal size
        # on both sides to offset the repair debt. This intentionally runs
        # before directional guards so extreme-price repair pairs like
        # YES 0.15 + NO 0.84 can remain two-sided when config permits it.
        balanced_repair_plan = plan_balanced_negative_edge_repair(
            pos,
            yes_price=quotes.yes_buy_price,
            no_price=quotes.no_buy_price,
            min_order_size=min_order_size,
            max_order_size=self.quote_engine.max_order_size,
            config=self.balanced_repair_config,
            remaining_seconds=remaining,
            abs_imbalance=abs_imbalance,
            is_halted=is_halted,
            close_only_phase=close_only_phase,
            small_capital_enabled=self._small_capital_enabled(),
        )
        if balanced_repair_plan.mode == "balanced_repair":
            quotes.yes_buy_size = balanced_repair_plan.yes_size
            quotes.no_buy_size = balanced_repair_plan.no_size
            repair_mode = "balanced_repair"
            quotes.combined_cost = round(float(quotes.yes_buy_price or 0) + float(quotes.no_buy_price or 0), 4)
            quotes.edge_per_pair = round(1.0 - quotes.combined_cost, 4)
            log.warning(
                "balanced_repair_quote_planned",
                asset=self.asset,
                market=market.market_id[:8],
                yes_price=quotes.yes_buy_price,
                no_price=quotes.no_buy_price,
                size=balanced_repair_plan.yes_size,
                pair_cost=balanced_repair_plan.metadata.get("pair_cost"),
                pair_edge=balanced_repair_plan.metadata.get("pair_edge"),
                debt=balanced_repair_plan.metadata.get("debt"),
                needed_pairs=balanced_repair_plan.metadata.get("needed_pairs"),
            )

        directional_action = apply_directional_market_guard(quotes, fv, repair_mode)
        if directional_action == "block_cheap_side":
            log.warning(
                "normal_quote_reduced_extreme_directional",
                asset=self.asset,
                fair_value=round(fv, 4),
                action=directional_action,
            )
        elif directional_action == "halve_cheap_side":
            log.info(
                "normal_quote_reduced_moderate_directional",
                asset=self.asset,
                fair_value=round(fv, 4),
                action=directional_action,
            )

        proposed_combined = float(quotes.yes_buy_price or 0) + float(quotes.no_buy_price or 0)
        if apply_pair_cost_precheck(quotes, fv, repair_mode, MAX_COMBINED_COST):
            log.warning(
                "pair_cost_precheck_blocking_adverse_side",
                asset=self.asset,
                combined=round(proposed_combined, 4),
                max_allowed=MAX_COMBINED_COST,
                yes_price=quotes.yes_buy_price,
                no_price=quotes.no_buy_price,
                fair_value=round(fv, 4),
            )

        # 12.35 FV-favored entry mode: when flat, start by buying only the side
        # the model likes (e.g. FV=0.60 => YES first). Once that side fills,
        # the existing inventory-repair logic quotes only the opposite side to
        # complete profitable pairs under the universal pair-cost guard.
        fv_entry_side = None
        if (repair_mode == "normal" and not balance_only and not is_halted
                and not close_only_phase):
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

        # Small-capital one-cycle mode: the first/opening attempt is one order
        # only, then after that side fills we force the opposite side until the
        # pair is balanced/mergeable.
        sct_entry_side = self._apply_small_capital_opening_one_side(
            market.market_id,
            quotes,
            repair_mode,
            fv,
        )
        repair_mode = self._apply_small_capital_balancing_override(
            market.market_id,
            pos,
            quotes,
            repair_mode,
            min_order_size,
            wallet_imbalance=wallet_imbalance,
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

            # QuotePolicy owns the post-capital quote invariants: minimum live
            # sizes, close-only repair side enforcement, and normal-mode
            # atomicity. MarketCycler only emits side effects for the decision.
            post_capital_decision = quote_policy.apply_post_capital_safety(
                quotes,
                min_order_size=min_order_size,
                allow_round_up=False,
                repair_mode=repair_mode,
                abs_imbalance=abs_imbalance,
                fv_entry_side=fv_entry_side,
                sct_entry_side=sct_entry_side,
                merge_blocked=self._merge_unavailable_until > _time.time(),
                atomic_reason="NORMAL_QUOTE_NOT_ATOMIC",
            )
            if not post_capital_decision.allowed and post_capital_decision.reason == "NORMAL_QUOTE_NOT_ATOMIC":
                log.warning(
                    "normal_quote_blocked_not_atomic",
                    asset=self.asset,
                    yes_size=post_capital_decision.metadata.get("before", {}).get("yes_size", quotes.yes_buy_size),
                    no_size=post_capital_decision.metadata.get("before", {}).get("no_size", quotes.no_buy_size),
                    merge_blocked=post_capital_decision.metadata.get("merge_blocked", False),
                    imbalance=round(imbalance, 4),
                )
        except Exception:
            # Never fail a cycle due to sizing guardrails.
            pass

        # Belt-and-suspenders safety check outside the guardrail try-block:
        # flat normal mode must be both-side, no-side, or an explicitly allowed
        # entry mode, and repair modes must remain close-only.
        final_post_capital_decision = quote_policy.apply_post_capital_safety(
            quotes,
            min_order_size=min_order_size,
            allow_round_up=False,
            repair_mode=repair_mode,
            abs_imbalance=abs_imbalance,
            fv_entry_side=fv_entry_side,
            sct_entry_side=sct_entry_side,
            atomic_reason="NORMAL_QUOTE_NOT_ATOMIC_FINAL",
        )
        if not final_post_capital_decision.allowed and final_post_capital_decision.reason == "NORMAL_QUOTE_NOT_ATOMIC_FINAL":
            log.warning(
                "normal_quote_blocked_not_atomic_final",
                asset=self.asset,
                yes_size=final_post_capital_decision.metadata.get("before", {}).get("yes_size", quotes.yes_buy_size),
                no_size=final_post_capital_decision.metadata.get("before", {}).get("no_size", quotes.no_buy_size),
                imbalance=round(imbalance, 4),
            )

        if quotes.yes_buy_size == 0 and quotes.no_buy_size == 0:
            self._set_dashboard_event("skip", "NO_QUOTES", halt_reason if is_halted else phase)
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        if self._small_capital_enabled():
            max_sct_size = int(getattr(getattr(self, "small_capital_config", None), "max_shares_per_order", 0) or 0)
            if max_sct_size > 0:
                quotes.yes_buy_size = min(int(quotes.yes_buy_size or 0), max_sct_size)
                quotes.no_buy_size = min(int(quotes.no_buy_size or 0), max_sct_size)

            sct_state = self._small_capital_state(market.market_id)
            if self._small_capital_opening_spent(sct_state) and repair_mode == "normal" and abs_imbalance < min_order_size:
                active = self.order_mgr.get_active(market.market_id)
                has_resting_opening_quote = bool(active.yes_order_id or active.no_order_id)
                if self._repair_small_capital_unfilled_opening_state(
                    market.market_id,
                    sct_state,
                    has_resting_opening_quote,
                    int(pos.matched_pairs() or 0),
                ):
                    sct_state = self._small_capital_state(market.market_id)
                    if not self._small_capital_opening_spent(sct_state):
                        log.info(
                            "small_capital_unfilled_opening_retry_enabled",
                            asset=self.asset,
                            market=market.market_id[:8],
                            msg="opening order was canceled before fill; allowing retry",
                        )
                        self._set_dashboard_event(
                            "info",
                            "SMALL_CAP_RETRY_UNFILLED_OPENING",
                            "previous opening canceled before fill; retrying",
                        )
                        # Continue to update_quotes below with the freshly
                        # computed normal quote. No inventory was ever opened.
                    else:
                        log.warning(
                            "small_capital_stale_quote_cycle_repaired",
                            asset=self.asset,
                            market=market.market_id[:8],
                            msg="opening order was canceled before fill; preserving one-cycle spent state",
                        )
                        self._set_dashboard_event(
                            "skip",
                            "SMALL_CAP_OPENING_SPENT",
                            "opening quote was already attempted this window",
                        )
                        await self.order_mgr.cancel_market_quotes(market.market_id)
                        self._update_dashboard(market, spot, fv, sigma, "SMALL_CAP_WAIT_NEXT", remaining)
                        return
                elif int(pos.matched_pairs() or 0) > 0:
                    await self._small_capital_maybe_stop_completed(market, pos, "normal_quote_balanced")
                    return
                elif self._small_capital_should_hold_opening_quote(
                    sct_state,
                    has_resting_opening_quote,
                    int(pos.matched_pairs() or 0),
                ):
                    reprice_side = self._apply_small_capital_opening_reprice_guard(
                        market.market_id,
                        quotes,
                        sct_state,
                        min_order_size,
                    )
                    if not reprice_side:
                        log.warning(
                            "small_capital_opening_reprice_side_unknown",
                            asset=self.asset,
                            market=market.market_id[:8],
                            msg="opening quote is resting but side could not be inferred; holding without reprice",
                        )
                        self._set_dashboard_event(
                            "info",
                            "SMALL_CAP_HOLD_OPENING",
                            "opening quote already placed; waiting for fill/cancel",
                        )
                        self._update_dashboard(market, spot, fv, sigma, "SMALL_CAP_HOLD_OPENING", remaining)
                        return
                    self._set_dashboard_event(
                        "info",
                        "SMALL_CAP_REPRICE_OPENING",
                        f"repricing existing {reprice_side} opening quote",
                    )
                    # Continue to universal guards and update_quotes.
                    # OrderManager will cancel/replace only if the target price
                    # materially changed; otherwise existing queue priority stays.
                elif not has_resting_opening_quote:
                    log.warning(
                        "small_capital_no_second_opening_quote",
                        asset=self.asset,
                        market=market.market_id[:8],
                        msg="one opening quote cycle already used; not opening again this window",
                    )
                    self._set_dashboard_event(
                        "skip",
                        "SMALL_CAP_WAIT_FILL",
                        "opening quote cycle already used for this window",
                    )
                    await self.order_mgr.cancel_market_quotes(market.market_id)
                    self._update_dashboard(market, spot, fv, sigma, "SMALL_CAP_WAIT_FILL", remaining)
                    return

        # Absolute post-generation invariant: if inventory is already imbalanced
        # by at least one live-min order, QuotePolicy blocks the heavy side
        # before pair-cost caps are computed for the remaining repair quote.
        heavy_side_decision = quote_policy.enforce_inventory_heavy_side(
            quotes,
            pos.share_imbalance(),
            min_order_size,
            repair_mode,
        )
        repair_mode = heavy_side_decision.metadata.get("repair_mode", repair_mode)

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
            cap_decision = plan_repair_price_cap(
                pos,
                side_label,
                size_val,
                fv,
                min_edge=pair_edge,
                repair_mode=repair_mode,
                small_capital_opening_spent=sct_opening_spent,
                small_capital_state=self._small_capital_state(market.market_id) if sct_opening_spent else None,
                small_capital_config=getattr(self, "small_capital_config", None),
                abs_imbalance=abs_imbalance,
            )
            cap = float(cap_decision.cap)
            sct_guard_source = cap_decision.source


            if cap_decision.blocked:
                if cap_decision.reason == "SMALL_CAPITAL_EMERGENCY_HEDGE_MISSING_ENTRY_PRICE":
                    log.warning(
                        "small_capital_emergency_hedge_blocked_missing_entry_price",
                        market=market.market_id[:8],
                        side=side_label,
                        quoted=price_val,
                        mode=repair_mode,
                        elapsed=round(float(cap_decision.metadata.get("emergency_elapsed", 0.0) or 0.0), 2),

                    )
                else:
                    log.warning(
                        "small_capital_repair_blocked_missing_entry_price",
                        market=market.market_id[:8],
                        side=side_label,
                        quoted=price_val,
                        mode=repair_mode,
                        imbalance=round(imbalance, 4),
                    )
                setattr(quotes, buy_size_attr, 0)
                continue

            if sct_guard_source == "small_capital_emergency_hedge":
                emergency_elapsed = float(cap_decision.metadata.get("emergency_elapsed", 0.0) or 0.0)
                self._set_dashboard_event(
                    "warn",
                    "SMALL_CAP_EMERGENCY_HEDGE",
                    f"{side_label} cap {cap:.2f} after {emergency_elapsed:.0f}s",
                )
                log.warning(
                    "small_capital_emergency_hedge_active",
                    market=market.market_id[:8],
                    side=side_label,
                    cap=round(cap, 4),
                    elapsed=round(emergency_elapsed, 2),
                    quoted=price_val,
                    mode=repair_mode,
                )

            # No unmatched fills on opposite → cap is 0.99, no constraint
            if cap >= 0.99:
                continue

            pair_cost_decision = quote_policy.apply_pair_cost_side_guard(
                quotes,
                side_label=side_label,
                repair_mode=repair_mode,
                cap=cap,
                pair_edge=pair_edge,
                best_ask=best_ask,
                best_bid=best_bid,
                aggressive_price_fn=aggressive_repair_price,
                guard_source=sct_guard_source,
            )
            if pair_cost_decision.reason == "PAIR_COST_BLOCKED":
                log.warning("pair_cost_guard_blocked",
                            market=market.market_id[:8], side=side_label,
                            quoted=price_val, cap=round(cap, 4),
                            mode=repair_mode)
            elif pair_cost_decision.reason == "REPAIR_QUOTE_CAPPED_FOR_PAIR_EDGE":
                log.warning("repair_quote_capped_for_pair_edge",
                            market=market.market_id[:8], side=side_label,
                            quoted=pair_cost_decision.metadata.get("old_price"), cap=round(cap, 4),
                            min_edge=pair_edge,
                            source=sct_guard_source)
            elif pair_cost_decision.reason == "REPAIR_QUOTE_AGGRESSED_TO_CAP":
                log.info("repair_quote_aggressed_to_cap",
                         market=market.market_id[:8], side=side_label,
                         old=pair_cost_decision.metadata.get("old_price"),
                         new=pair_cost_decision.metadata.get("new_price"),
                         cap=round(cap, 4), min_edge=pair_edge,
                         best_ask=best_ask)
            elif pair_cost_decision.reason == "PAIR_COST_CLAMPED":
                # Normal mode: silently clamp to cap
                log.info("pair_cost_guard_clamped",
                         market=market.market_id[:8], side=side_label,
                         quoted=price_val, cap=round(cap, 4),
                         mode=repair_mode)

        quotes.combined_cost = round(float(quotes.yes_buy_price or 0) + float(quotes.no_buy_price or 0), 4)
        quotes.edge_per_pair = round(1.0 - quotes.combined_cost, 4)

        if quotes.yes_buy_size == 0 and quotes.no_buy_size == 0:
            self._set_dashboard_event("skip", "NO_QUOTES", halt_reason if is_halted else phase)
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        final_quote_decision = quote_policy.apply_final_inventory_safety(
            quotes,
            imbalance=pos.share_imbalance(),
            min_order_size=min_order_size,
            repair_mode=repair_mode,
            max_combined_cost=MAX_COMBINED_COST,
        )
        repair_mode = final_quote_decision.metadata.get("repair_mode", repair_mode)
        if not final_quote_decision.allowed:
            log.warning(
                "final_quote_validation_failed",
                market=market.market_id,
                reason=final_quote_decision.reason,
                **final_quote_decision.metadata,
            )
            self._set_dashboard_event("skip", final_quote_decision.reason, str(final_quote_decision.metadata)[:160])
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return
        quotes.combined_cost = round(
            (float(quotes.yes_buy_price or 0) if quotes.yes_buy_size else 0.0)
            + (float(quotes.no_buy_price or 0) if quotes.no_buy_size else 0.0),
            4,
        )
        quotes.edge_per_pair = round(1.0 - quotes.combined_cost, 4)
        if quotes.yes_buy_size == 0 and quotes.no_buy_size == 0:
            self._set_dashboard_event("skip", "NO_QUOTES", halt_reason if is_halted else phase)
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining)
            return

        # 13. Pre-trade checks
        fv_fresh = not self.fair_value_model.is_stale
        passed, failed_reasons = pre_trade_checks(fv, quotes, inv_state.value,
                                      fv_fresh, phase)
        if not passed:
            log.warning("pre_trade_failed", market=market.market_id, reasons=failed_reasons)
            self._set_dashboard_event("skip", "PRE_TRADE_FAILED", ", ".join(map(str, failed_reasons))[:160])
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
            self._set_dashboard_event("error", "ORDER_UPDATE_FAILED", str(self.order_mgr.last_order_error))
            self._update_dashboard(market, spot, fv, sigma, "ORDER_ERROR", remaining, quotes=quotes, pos=pos)
            self._running = False
            return

        self._mark_small_capital_quote_started(market, quotes, repair_mode)

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
        if fills:
            wallet_truth, wallet_snapshot = await self._refresh_wallet_truth_for_market(market)
        if fills and await self._small_capital_maybe_stop_completed(market, pos, "post_fill_balanced", wallet_snapshot=wallet_snapshot):
            return
        post_fill_negative_pair_edge = decide_negative_pair_edge(pos) if fills else None
        if post_fill_negative_pair_edge and post_fill_negative_pair_edge.triggered:
            pairs = post_fill_negative_pair_edge.matched_pairs
            debt_eligible, debt_reason, debt_meta = balanced_repair_debt_eligible(
                pos, self.balanced_repair_config
            )
            if debt_eligible and not self._small_capital_enabled():
                log.warning(
                    "negative_pair_edge_balanced_repair_after_fill",
                    asset=self.asset,
                    market=market.market_id[:8],
                    matched_pairs=pairs,
                    pair_pnl=round(post_fill_negative_pair_edge.pair_pnl, 4),
                    debt=debt_meta.get("debt"),
                    msg="Negative pair edge accepted as repair debt; future balanced pairs must offset it",
                )
                self._set_dashboard_event(
                    "warn",
                    "BALANCED_REPAIR_DEBT",
                    f"debt ${float(debt_meta.get('debt', 0) or 0):.2f}",
                )
            else:
                log.critical(
                    "negative_pair_edge_halt",
                    asset=self.asset,
                    market=market.market_id[:8],
                    matched_pairs=pairs,
                    pair_pnl=round(post_fill_negative_pair_edge.pair_pnl, 4),
                    balanced_repair_reason=debt_reason,
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
                self._set_dashboard_event("error", "NEGATIVE_PAIR_EDGE", "matched pair cost exceeded 1")
                self._running = False
                return

        # 15.5. Auto-merge check: dollar-based threshold OR low balance. The
        # 2-minute pre-expiry force merge runs earlier so it is not skipped by
        # quote/order early returns.
        force_merge = False
        merge_reason = "routine"
        debt_eligible_for_repair, _, debt_repair_meta = balanced_repair_debt_eligible(
            pos, self.balanced_repair_config
        )
        suppress_routine_merge_for_repair = debt_eligible_for_repair and not self._small_capital_enabled()
        # Dollar-based mid-market merge trigger. If balanced repair is armed,
        # keep the negative debt visible so profitable new pairs can offset it;
        # low-balance/pre-expiry merge paths may still recover capital.
        if not force_merge and self.inventory.should_merge(market.market_id) and not suppress_routine_merge_for_repair:
            force_merge = True
            merge_reason = "dollar_threshold"
            log.info("dollar_threshold_merge_triggered",
                     asset=self.asset,
                     locked=f"${pos.locked_capital():.2f}",
                     threshold=f"${self.inventory.auto_merge_dollar_threshold:.2f}")
        elif suppress_routine_merge_for_repair:
            log.info(
                "dollar_threshold_merge_suppressed_for_balanced_repair",
                asset=self.asset,
                debt=debt_repair_meta.get("debt"),
                locked=f"${pos.locked_capital():.2f}",
            )

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
                log.info("auto_merge_during_trading",
                         asset=self.asset,
                         reason=merge_reason,
                         pairs=merge_result["pairs_merged"],
                         usdc=f"${merge_result['usdc_recovered']:.2f}")
                # Update capital arbiter on recovery
                if self.inventory.capital_arbiter:
                    self.inventory.capital_arbiter.record_recovery(
                        self.asset, merge_result['usdc_recovered'])

        # 16. Update dashboard
        if not (getattr(self, "_dashboard_event", {}) or {}).get("event_reason") == "PRE_EXPIRY_AUTO_MERGE":
            self._clear_dashboard_event()
        self._update_dashboard(market, spot, fv, sigma, halt_reason if is_halted else phase, remaining,
                                quotes, pos, imbalance, inv_state.value)

    def _update_dashboard_waiting(self):
        update_dashboard_waiting(self)

    def _max_spot_price_age_seconds(self) -> float:
        """Allowed active spot age for live quoting.

        Exness/MT5 bridge freshness is governed by its configured stale window.
        The active price timestamp is the bridge receive time, not the raw MT5
        tick timestamp, because MT5 ticks may repeat while the bridge is still
        healthy and returning current bid/ask/mid data.
        """
        if bool(getattr(self.price_feed, "mt5_bridge_url", "")):
            configured = float(getattr(self.price_feed, "mt5_bridge_stale_seconds", MAX_EXNESS_PRICE_AGE_SECONDS)
                               or MAX_EXNESS_PRICE_AGE_SECONDS)
            return max(0.5, configured)
        return MAX_SPOT_PRICE_AGE_SECONDS

    def _dashboard_sigma_for_stale_spot(self) -> float:
        return dashboard_sigma_for_stale_spot(self)

    def _update_dashboard(self, market, spot, fv, sigma, phase,
                           remaining, quotes=None, pos=None,
                           delta=0, inv_state="NORMAL"):

        update_dashboard(self, market, spot, fv, sigma, phase, remaining, quotes, pos, delta, inv_state)


    async def stop(self):
        self._running = False
        if self.current_market:
            await self.order_mgr.cancel_market_quotes(
                self.current_market.market_id
            )
