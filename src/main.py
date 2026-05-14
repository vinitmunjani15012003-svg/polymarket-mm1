"""
Polymarket Market-Maker Bot — Main Entry Point

Usage:
    python -m src.main --mode dry-run          # Paper trading (default)
    python -m src.main --mode live --config config/live.yaml  # Live trading
    python -m src.main --mode dry-run --assets BTC ETH        # Specific assets
"""

import os
import sys
import asyncio
import argparse
import signal
import time
import logging
from dataclasses import dataclass
from typing import Optional

from src.config import load_config, BotConfig
from src.monitoring.logger import setup_logging, get_logger
from src.monitoring.dashboard import Dashboard
from src.monitoring.pnl_tracker import PnLTracker
from src.data.price_feed import PriceFeed
from src.data.market_discovery import MarketDiscovery
from src.data.orderbook import OrderBookReader
from src.strategy.inventory import InventoryManager
from src.strategy.capital_arbiter import CapitalArbiter
from src.execution.order_manager import OrderManager
from src.execution.dry_run import DryRunExecutor
from src.execution.state_manager import StateManager
from src.execution.ctf_ops import (
    CTFOperations, GaslessMerger, BalanceMonitor, SimulatedBalanceMonitor
)
from src.risk.risk_engine import RiskEngine
from src.orchestration.market_cycler import MarketCycler
from src.monitoring.alerter import alerter


log = None  # Initialized after logging setup


@dataclass
class RunResult:
    success: bool
    mode: str
    target_windows: Optional[int]
    markets_settled: int
    total_fills: int
    net_pnl_with_rebates: float
    failure_reason: Optional[str] = None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Market-Maker Bot for 15-min Crypto Binary Markets"
    )
    parser.add_argument(
        "--mode", choices=["dry-run", "live"], default="dry-run",
        help="Trading mode (default: dry-run)"
    )
    parser.add_argument(
        "--config", default="config/default.yaml",
        help="Path to config file (default: config/default.yaml)"
    )
    parser.add_argument(
        "--override", default=None,
        help="Path to override config (e.g., config/live.yaml)"
    )
    parser.add_argument(
        "--assets", nargs="+", default=None,
        help="Assets to trade (default: all enabled in config)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run in headless mode (no dashboard, no confirmation prompts)"
    )
    parser.add_argument(
        "--target-windows", type=int, default=None,
        help="Required settled-window count. Run exits non-zero if stopped before this target."
    )
    parser.add_argument(
        "--progress-heartbeat-minutes", type=float, default=30.0,
        help="Log run progress heartbeat every N minutes (default: 30)."
    )
    parser.add_argument(
        "--alert-webhook", default=None,
        help="Optional Slack/Discord-compatible webhook for immediate failure alerts. Also reads POLYMARKET_ALERT_WEBHOOK_URL, OPENCLAW_ALERT_WEBHOOK_URL, or DISCORD_WEBHOOK_URL."
    )
    return parser.parse_args()


async def run_bot(
    config: BotConfig,
    assets_filter: list[str] = None,
    headless: bool = False,
    target_windows: int | None = None,
    progress_heartbeat_minutes: float = 30.0,
) -> RunResult:
    """Main bot runner."""
    global log

    mode = config.mode
    log.info("bot_starting", mode=mode)

    # Suppress noisy HTTP request logs from httpx/httpcore
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    # Determine which assets to trade
    active_assets = {}
    for name, ac in config.assets.items():
        if not ac.enabled:
            continue
        if assets_filter and name not in assets_filter:
            continue
        active_assets[name] = ac

    if not active_assets:
        log.error("no_active_assets")
        return RunResult(False, mode, target_windows, 0, 0, 0.0, "no active assets")

    log.info("active_assets", assets=list(active_assets.keys()))

    # --- Initialize shared components ---

    # Price feed (REAL for both dry-run and live)
    symbols = [ac.symbol for ac in active_assets.values()]
    price_feed = PriceFeed(
        ws_url=config.credentials.binance_ws_url,
        symbols=symbols,
        vol_lookback=config.global_params.vol_lookback_seconds,
    )

    # Market discovery (REAL for both modes)
    discovery = MarketDiscovery(assets=list(active_assets.keys()))

    # Order book reader (REAL for both modes)
    book_reader = OrderBookReader(host=config.credentials.host)

    # (PnLTracker moved to per-asset loop)

    # Dashboard
    dashboard = Dashboard(mode=mode)
    
    # State Manager (Crash recovery)
    state_manager = StateManager()

    # --- Initialize executor (mode-dependent) ---
    gasless_merger = None
    balance_monitor = None
    ctf_ops = None

    if mode == "dry-run":
        executor = DryRunExecutor(
            min_queue_time=config.dry_run.fill_delay_min,
            max_queue_time=config.dry_run.fill_delay_max,
        )
        log.info("dry_run_executor_initialized", fill_model="price_crossing")
        
        if config.balance_monitor.enabled:
            balance_monitor = SimulatedBalanceMonitor(
                warn_balance=config.balance_monitor.warn_balance,
                merge_balance=config.balance_monitor.merge_balance,
                min_merge_pairs=config.balance_monitor.min_merge_pairs,
                check_interval=config.balance_monitor.check_interval,
            )
            log.info("simulated_balance_monitor_ready")
    else:
        # Live mode — validate required credentials before proceeding
        required_creds = [
            ("private_key", config.credentials.private_key),
            ("api_key", config.credentials.api_key),
            ("api_secret", config.credentials.api_secret),
            ("api_passphrase", config.credentials.api_passphrase),
        ]
        missing = [name for name, val in required_creds if not val]
        if missing:
            log.error("missing_live_credentials", fields=missing)
            print(f"\n[FATAL] Missing required live credentials: {', '.join(missing)}")
            print("  Set them via environment variables or in config/live.yaml")
            print("  Example: export POLYMARKET_PK='0x...'\n")
            return RunResult(False, mode, target_windows, 0, 0, 0.0, f"missing live credentials: {', '.join(missing)}")

        from src.execution.clob_client import ClobClientWrapper
        executor = ClobClientWrapper(
            host=config.credentials.host,
            private_key=config.credentials.private_key,
            chain_id=config.credentials.chain_id,
            api_key=config.credentials.api_key,
            api_secret=config.credentials.api_secret,
            api_passphrase=config.credentials.api_passphrase,
            signature_type=config.credentials.signature_type,
            funder=config.credentials.funder,
        )
        executor.set_state_manager(state_manager)
        await executor.initialize()
        log.info("live_executor_initialized")

        # Deposit wallet flow requires syncing CLOB balance/allowance before
        # trading. Compatibility-guarded for older SDKs.
        if config.credentials.signature_type == 3:
            allowance_ok = await executor.sync_balance_allowance()
            if not allowance_ok:
                log.warning(
                    "balance_allowance_sync_not_confirmed",
                    msg="Deposit wallet balance/allowance sync was unavailable or failed; order placement may be rejected",
                )

        # Reconcile exchange-side state before canceling stale orders. This keeps
        # restarts from blindly discarding order/fill context.
        await executor.reconcile_on_startup()

        # Cancel all orders on startup after reconciliation. This prevents fake
        # state or duplicate exposure.
        await executor.cancel_all()
        log.info("startup_cleanup", msg="Cancelled all stale orders from previous session")

        # --- Initialize gasless merger (Builder Relayer) ---
        gasless_merger = GaslessMerger(
            private_key=config.credentials.private_key,
            builder_api_key=config.credentials.builder_api_key,
            builder_secret=config.credentials.builder_secret,
            builder_passphrase=config.credentials.builder_passphrase,
            relayer_url=config.credentials.builder_relayer_url,
            chain_id=config.credentials.chain_id,
            collateral_token=config.credentials.collateral_token,
            relayer_api_key=config.credentials.relayer_api_key,
            relayer_api_key_address=config.credentials.relayer_api_key_address,
            funder=config.credentials.funder,
            signature_type=config.credentials.signature_type,
        )
        gasless_ok = await gasless_merger.initialize()
        if gasless_ok:
            log.info("gasless_merger_ready",
                     msg="Will use gasless relayer for merge operations")
            if config.credentials.signature_type == 3:
                approval_ok = await gasless_merger.ensure_deposit_wallet_trading_approvals()
                if approval_ok:
                    await executor.sync_balance_allowance()
                else:
                    log.warning("startup_deposit_wallet_activation_not_confirmed")
        else:
            log.warning("gasless_merger_unavailable",
                        msg="Will fall back to on-chain merge (requires POL for gas)")

        # --- Initialize on-chain CTF ops (fallback for merge) ---
        # Proxy/deposit-wallet modes hold tokens in the funder wallet, not the
        # signer EOA. Direct on-chain fallback signs from the EOA, so it cannot
        # safely merge/redeem funder-held positions. Use gasless relayer only in
        # those modes and fail safe if gasless is unavailable.
        if config.credentials.signature_type in (1, 2, 3) and config.credentials.funder:
            ctf_ops = None
            log.warning("onchain_ctf_fallback_disabled",
                        reason="proxy/deposit wallet uses funder address; gasless relayer required")
        else:
            ctf_ops = CTFOperations(
                private_key=config.credentials.private_key,
                rpc_url=config.credentials.polygon_rpc_url,
                collateral_token=config.credentials.collateral_token,
                dry_run=False,
            )
            await ctf_ops.initialize()

        # --- Initialize balance monitor (auto-merge on low USDC) ---
        if config.balance_monitor.enabled:
            balance_monitor = BalanceMonitor(
                private_key=config.credentials.private_key,
                rpc_url=config.credentials.polygon_rpc_url,
                collateral_token=config.credentials.collateral_token,
                warn_balance=config.balance_monitor.warn_balance,
                merge_balance=config.balance_monitor.merge_balance,
                min_merge_pairs=config.balance_monitor.min_merge_pairs,
                check_interval=config.balance_monitor.check_interval,
                balance_address=(
                    config.credentials.funder
                    if config.credentials.signature_type in (1, 2, 3)
                    else ""
                ),
            )
            bal_ok = await balance_monitor.initialize()
            if bal_ok:
                initial_bal = await balance_monitor.get_usdc_balance()
                log.info("balance_monitor_ready",
                         balance=f"${initial_bal:.2f}",
                         merge_at=f"${config.balance_monitor.merge_balance:.2f}")
                
                # Preflight: abort if wallet has zero balance
                if initial_bal <= 0:
                    log.error("zero_balance_abort",
                              msg="Wallet has $0 USDC. Fund your wallet before trading.",
                              address=balance_monitor._address)
                    print("\n[FATAL] Wallet has $0 USDC.e on Polygon!")
                    print(f"  Address: {balance_monitor._address}")
                    print("  Fund with at least $50 USDC.e before running live mode.")
                    print("  Deposit at: https://polymarket.com/deposit\n")
                    return RunResult(False, mode, target_windows, 0, 0, 0.0, "zero live wallet balance")
            else:
                log.warning("balance_monitor_failed",
                            msg="Balance monitoring disabled")
                balance_monitor = None

    # --- Create per-asset market cyclers ---
    cyclers = []
    tasks = []
    shutdown_event = asyncio.Event()
    failure_reason: str | None = None

    def mark_failure(reason: str):
        nonlocal failure_reason
        if failure_reason is None:
            failure_reason = reason
            log.error("run_failure", reason=reason)
            alerter.send_alert("Polymarket run failed", reason, level="ERROR", cooldown=0)
        if not shutdown_event.is_set():
            shutdown_event.set()

    def request_shutdown():
        if not shutdown_event.is_set():
            shutdown_event.set()

    def start_task(coro, name: str):
        task = asyncio.create_task(coro, name=name)

        def _done(t: asyncio.Task):
            if t.cancelled() or shutdown_event.is_set():
                return
            exc = t.exception()
            if exc is not None:
                mark_failure(f"task {name} crashed: {type(exc).__name__}: {exc}")
            else:
                reason = None
                if name.startswith("cycler_"):
                    asset = name.split("_", 1)[1]
                    for cycler in cyclers:
                        if getattr(cycler, "asset", None) == asset:
                            reason = getattr(cycler, "stop_reason", None)
                            break
                mark_failure(f"task {name} exited before shutdown" + (f": {reason}" if reason else ""))

        task.add_done_callback(_done)
        tasks.append(task)
        return task

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_shutdown())

    def dashboard_update(state):
        dashboard.update(state)

    shared_risk = RiskEngine(
        total_capital=config.global_params.total_capital,
        max_daily_loss_pct=config.global_params.max_daily_loss_pct,
        max_drawdown_pct=config.global_params.max_drawdown_pct,
    )

    def portfolio_pnl_getter() -> float:
        total = 0.0
        for c in cyclers:
            total += c.pnl.economic_pnl
            if c.current_market and c.last_fair_value is not None:
                pos = c.inventory.positions.get(c.current_market.market_id)
                if pos:
                    total += pos.mark_to_market(c.last_fair_value)
        return total

    # --- Create shared capital arbiter ---
    capital_arbiter = CapitalArbiter(
        total_capital=config.global_params.total_capital,
        asset_names=list(active_assets.keys()),
        max_per_asset_pct=0.50,
        reserve_pct=0.10,
    )
    for a_name in active_assets:
        capital_arbiter.register_asset(a_name)
    log.info("capital_arbiter_initialized",
             total=config.global_params.total_capital,
             assets=list(active_assets.keys()))

    for asset_name, ac in active_assets.items():
        # Per-asset executor + order manager in dry-run mode
        # Each asset needs its own DryRunExecutor so that fair values
        # and open orders don't collide between assets.
        if mode == "dry-run":
            asset_executor = DryRunExecutor(
                min_queue_time=config.dry_run.fill_delay_min,
                max_queue_time=config.dry_run.fill_delay_max,
            )
        else:
            asset_executor = executor  # Live mode: shared CLOB client

        asset_order_manager = OrderManager(
            executor=asset_executor,
            reprice_threshold=config.global_params.reprice_threshold,
            min_update_interval=config.global_params.min_order_update_interval,
        )

        # Per-asset inventory manager
        inventory = InventoryManager(
            soft_limit=ac.soft_limit,
            hard_limit=ac.hard_limit,
            emergency=ac.emergency,
            max_imbalance=ac.max_dollar_delta,  # Now share-based threshold
            max_capital_per_market=config.global_params.max_capital_per_market,
            auto_merge_dollar_threshold=ac.auto_merge_dollar_threshold,
        )
        inventory.set_state_manager(state_manager)
        inventory.set_capital_arbiter(capital_arbiter)

        # Per-asset P&L tracker — split total capital across assets
        per_asset_capital = config.global_params.starting_capital / max(1, len(active_assets))
        asset_pnl_tracker = PnLTracker()
        asset_pnl_tracker.starting_capital = per_asset_capital
        asset_pnl_tracker.current_capital = per_asset_capital

        cycler = MarketCycler(
            asset=asset_name,
            asset_config=ac,
            global_config=config.global_params,
            price_feed=price_feed,
            order_manager=asset_order_manager,
            market_discovery=discovery,
            book_reader=book_reader,
            inventory_manager=inventory,
            risk_engine=shared_risk,
            pnl_tracker=asset_pnl_tracker,
            regime_config=config.regime,
            toxicity_config=config.toxicity,
            portfolio_pnl_getter=portfolio_pnl_getter,
            dashboard_callback=dashboard_update,
            ctf_ops=ctf_ops,
            gasless_merger=gasless_merger,
            balance_monitor=balance_monitor,
        )
        cyclers.append(cycler)

    # --- Start all tasks ---
    log.info("starting_tasks", count=len(cyclers) + 1)

    # Price feed task
    price_task = start_task(price_feed.start(), "price_feed")

    # Wait for initial prices (up to 10s)
    log.info("waiting_for_prices")
    for _ in range(10):
        await asyncio.sleep(1)
        if all(price_feed.get_price(ac.symbol) for ac in active_assets.values()):
            break

    # Log initial prices
    for name, ac in active_assets.items():
        p = price_feed.get_price(ac.symbol)
        if p:
            log.info("initial_price", asset=name, symbol=ac.symbol,
                     price=round(p, 2))

    # Build symbol -> asset lookup and cycler lookup for live price piping
    symbol_to_asset = {ac.symbol.upper(): name for name, ac in active_assets.items()}
    cycler_by_asset = {}
    last_dashboard_fv_ts: dict[str, float] = {}

    # Market cycler tasks
    for cycler in cyclers:
        cycler_by_asset[cycler.asset] = cycler
        start_task(cycler.run(), f"cycler_{cycler.asset}")

    # --- Real-time price callback: pipe every WS tick directly into dashboard ---
    def on_live_price(symbol: str, price: float, ts: float):
        asset_name = symbol_to_asset.get(symbol.upper())
        if not asset_name:
            return
        cycler = cycler_by_asset.get(asset_name)
        if cycler:
            cycler.notify_price_update()
        spread = getattr(cycler, 'chainlink_spread', 0) if cycler else 0
        adjusted = price + spread
        
        # Compute live FV for the dashboard at a bounded rate. Binance bookTicker
        # can fire many times/sec; recomputing sigma+FV on every tick is avoidable
        # event-loop noise and quote cycles compute the authoritative FV anyway.
        live_fv = None
        last_fv_ts = last_dashboard_fv_ts.get(asset_name, 0.0)
        if (cycler and getattr(cycler, 'fair_value_model', None) is not None
                and ts - last_fv_ts >= 0.25):
            sigma = cycler.vol_estimator.sigma_for_model() if hasattr(cycler, 'vol_estimator') else cycler.ac.default_sigma
            # We use 'adjusted' price to match Chainlink assumption
            live_fv = cycler.fair_value_model.fair_value(adjusted, sigma, ts)
            last_dashboard_fv_ts[asset_name] = ts
            
        # Initialize dashboard state if it doesn't exist yet (e.g., between windows)
        if asset_name not in dashboard._states:
            dashboard._states[asset_name] = {
                'asset': asset_name, 'spot_price': adjusted,
                'phase': 'WAITING', 'time_remaining': 0,
                'start_price': 0, 'fair_value': 0, 'sigma': 0,
            }
            dashboard._global_state.update(dashboard._states[asset_name])
        # Update spot price in-place
        dashboard._states[asset_name]['spot_price'] = adjusted
        dashboard._states[asset_name]['raw_spot'] = price
        dashboard._states[asset_name]['chainlink_spread'] = spread
        if live_fv is not None:
            dashboard._states[asset_name]['fair_value'] = live_fv

        if dashboard._global_state.get('asset') == asset_name:
            dashboard._global_state['spot_price'] = adjusted
            dashboard._global_state['raw_spot'] = price
            dashboard._global_state['chainlink_spread'] = spread
            if live_fv is not None:
                dashboard._global_state['fair_value'] = live_fv

    price_feed.on_price_update(on_live_price)

    async def state_heartbeat_loop():
        while True:
            state_manager.save_state()
            await asyncio.sleep(30)

    async def progress_heartbeat_loop():
        heartbeat_interval = max(60.0, progress_heartbeat_minutes * 60.0)
        last_heartbeat = 0.0
        while not shutdown_event.is_set():
            settled = sum(c.pnl.markets_settled for c in cyclers)
            fills = sum(c.pnl.total_fills for c in cyclers)
            volume = sum(c.pnl.total_volume for c in cyclers)
            net_pnl = sum(c.pnl.snapshot().economic_pnl for c in cyclers)
            now = time.time()

            if target_windows is not None and settled >= target_windows:
                log.info("target_windows_reached", target_windows=target_windows, markets_settled=settled)
                shutdown_event.set()
                return

            if now - last_heartbeat >= heartbeat_interval:
                msg = (
                    f"mode={mode} settled={settled}/{target_windows or 'open'} "
                    f"fills={fills} volume=${volume:.2f} net=${net_pnl:.4f}"
                )
                log.info(
                    "run_heartbeat",
                    mode=mode,
                    target_windows=target_windows,
                    markets_settled=settled,
                    total_fills=fills,
                    total_volume=round(volume, 2),
                    economic_pnl=round(net_pnl, 4),
                )
                alerter.send_alert("Polymarket run heartbeat", msg, level="INFO", cooldown=int(heartbeat_interval * 0.9))
                last_heartbeat = now

            await asyncio.sleep(min(60.0, heartbeat_interval))

    start_task(state_heartbeat_loop(), "state_heartbeat")
    start_task(progress_heartbeat_loop(), "progress_heartbeat")

    log.info("bot_running", assets=list(active_assets.keys()), mode=mode, target_windows=target_windows)

    # Switch to dashboard-only output (suppress console, keep file logs)
    from src.monitoring.logger import suppress_console, restore_console
    if not headless:
        suppress_console()

        # --- Legacy Windows Console Loop ---
        # For older Windows terminals that don't support rich.Live ANSI replacements,
        # we explicitly clear the console every second to prevent infinite scrolling.
        async def dashboard_loop():
            while True:
                try:
                    os.system('cls' if os.name == 'nt' else 'clear')
                    dashboard.console.print(dashboard.render())
                except Exception as e:
                    log.error("dashboard_error", error=str(e))
                await asyncio.sleep(1)

        start_task(dashboard_loop(), "dashboard")

    # --- Wait for shutdown (Windows-compatible) ---
    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # --- Graceful shutdown ---
    # Re-enable console logging for shutdown messages
    if not headless:
        restore_console()
    log.info("shutting_down")

    # Stop all cyclers
    for cycler in cyclers:
        await cycler.stop()

    # Cancel all orders (each cycler has its own order manager)
    for cycler in cyclers:
        await cycler.order_mgr.cancel_all()

    # Stop price feed
    await price_feed.stop()

    # Cancel remaining tasks
    for task in tasks:
        task.cancel()

    # Close clients
    await discovery.close()
    await book_reader.close()

    # Final P&L report
    session_pnl = PnLTracker()
    session_pnl.starting_capital = 0.0
    session_pnl.current_capital = 0.0
    if cyclers:
        session_pnl._session_start = min(cycler.pnl._session_start for cycler in cyclers)
        for cycler in cyclers:
            session_pnl.settlement_pnl += cycler.pnl.settlement_pnl
            session_pnl.outcome_pnl += getattr(cycler.pnl, "outcome_pnl", 0.0)
            session_pnl.spread_income += cycler.pnl.spread_income
            session_pnl.total_fees += cycler.pnl.total_fees
            session_pnl.est_rebates += cycler.pnl.est_rebates
            session_pnl.total_taker_fees += cycler.pnl.total_taker_fees
            session_pnl.total_volume += cycler.pnl.total_volume
            session_pnl.total_shares += cycler.pnl.total_shares
            session_pnl.total_fills += cycler.pnl.total_fills
            session_pnl.markets_settled += cycler.pnl.markets_settled
            session_pnl.markets_traded += cycler.pnl.markets_traded
            session_pnl.starting_capital += cycler.pnl.starting_capital
            session_pnl.current_capital += cycler.pnl.current_capital

    snap = session_pnl.snapshot()

    success = failure_reason is None
    final_failure_reason = failure_reason
    if target_windows is not None and snap.markets_settled < target_windows:
        success = False
        final_failure_reason = (
            final_failure_reason
            or f"incomplete target: settled {snap.markets_settled}/{target_windows} windows"
        )
        log.error(
            "target_windows_incomplete",
            target_windows=target_windows,
            markets_settled=snap.markets_settled,
            reason=final_failure_reason,
        )
        alerter.send_alert("Polymarket run incomplete", final_failure_reason, level="ERROR", cooldown=0)

    print("\n" + "=" * 60)
    print(f"  SESSION {'COMPLETE' if success else 'FAILED'} — {mode.upper()}")
    print(f"  Duration: {session_pnl.session_duration_hours:.2f} hours")
    if target_windows is not None:
        print(f"  Target Windows:  {target_windows}")
    print(f"  Markets Settled: {snap.markets_settled}")
    print(f"  Total Fills: {snap.total_fills}")
    print(f"  Total Volume: ${snap.total_volume:.2f}")
    print(f"  Total Shares: {snap.total_shares:.0f}")
    print("-" * 60)
    print(f"  Merge/Pair P&L:       ${snap.net_trading_pnl:.4f}")
    print(f"  Outcome P&L:          ${snap.outcome_pnl:.4f}")
    print(f"  Est. Rebates:         ${snap.est_rebates:.4f}")
    print(f"  Rebates/Hour:         ${session_pnl.rebates_per_hour():.4f}")
    print(f"  Merge+Rebate P&L:     ${snap.net_pnl_with_rebates:.4f}")
    print(f"  Economic P&L (total): ${snap.economic_pnl:.4f}")
    if final_failure_reason:
        print(f"  Failure Reason:  {final_failure_reason}")
    print("=" * 60)

    return RunResult(
        success=success,
        mode=mode,
        target_windows=target_windows,
        markets_settled=snap.markets_settled,
        total_fills=snap.total_fills,
        net_pnl_with_rebates=snap.economic_pnl,
        failure_reason=final_failure_reason,
    )


def main():
    global log

    args = parse_args()

    # Auto-detect live.yaml when running in live mode
    override = args.override
    if args.mode == "live" and not override:
        if os.path.exists("config/live.yaml"):
            override = "config/live.yaml"
            print("[INFO] Auto-loading config/live.yaml for live mode")

    # Load config
    config = load_config(args.config, override)
    config.mode = args.mode

    # Setup logging
    setup_logging(level=args.log_level)
    log = get_logger("main")
    alert_webhook = (
        args.alert_webhook
        or os.environ.get("POLYMARKET_ALERT_WEBHOOK_URL")
        or os.environ.get("OPENCLAW_ALERT_WEBHOOK_URL")
        or os.environ.get("DISCORD_WEBHOOK_URL")
    )
    alerter.configure(alert_webhook)

    # Banner
    print("\n" + "=" * 60)
    print("  POLYMARKET MARKET-MAKER BOT")
    print(f"  Mode: {args.mode.upper()}")
    print(f"  Assets: {args.assets or 'ALL ENABLED'}")
    print("  Strategy: BUY-ONLY | post_only=True")
    print("  Markets: Up/Down 15-minute crypto binaries")
    print("=" * 60 + "\n")

    if args.mode == "live" and not args.headless:
        # Safety confirmation for live mode
        print("[WARNING] LIVE MODE — Real money will be used!")
        confirm = input("Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            return

    # Run
    try:
        result = asyncio.run(
            run_bot(
                config,
                args.assets,
                args.headless,
                target_windows=args.target_windows,
                progress_heartbeat_minutes=args.progress_heartbeat_minutes,
            )
        )
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
        alerter.send_alert("Polymarket run interrupted", "KeyboardInterrupt", level="ERROR", cooldown=0)
        sys.exit(130)
    except Exception as e:
        message = f"unhandled exception: {type(e).__name__}: {e}"
        log.exception("run_unhandled_exception")
        alerter.send_alert("Polymarket run crashed", message, level="ERROR", cooldown=0)
        print(f"\n[FATAL] {message}")
        sys.exit(1)

    if not result.success:
        sys.exit(1)


if __name__ == "__main__":
    main()
