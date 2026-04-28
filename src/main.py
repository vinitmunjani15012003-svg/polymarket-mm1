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

from src.config import load_config, BotConfig
from src.monitoring.logger import setup_logging, get_logger
from src.monitoring.dashboard import Dashboard
from src.monitoring.pnl_tracker import PnLTracker
from src.data.price_feed import PriceFeed
from src.data.market_discovery import MarketDiscovery
from src.data.orderbook import OrderBookReader
from src.strategy.inventory import InventoryManager
from src.execution.order_manager import OrderManager
from src.execution.dry_run import DryRunExecutor
from src.execution.ctf_ops import (
    CTFOperations, GaslessMerger, BalanceMonitor, SimulatedBalanceMonitor
)
from src.risk.risk_engine import RiskEngine
from src.orchestration.market_cycler import MarketCycler


log = None  # Initialized after logging setup


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
    return parser.parse_args()


async def run_bot(config: BotConfig, assets_filter: list[str] = None):
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
        return

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

    # P&L tracker
    pnl_tracker = PnLTracker()
    pnl_tracker.starting_capital = config.global_params.starting_capital
    pnl_tracker.current_capital = config.global_params.starting_capital

    # Dashboard
    dashboard = Dashboard(mode=mode)

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
        # Live mode
        from src.execution.clob_client import ClobClientWrapper
        executor = ClobClientWrapper(
            host=config.credentials.host,
            private_key=config.credentials.private_key,
            chain_id=config.credentials.chain_id,
            api_key=config.credentials.api_key,
            api_secret=config.credentials.api_secret,
            api_passphrase=config.credentials.api_passphrase,
        )
        await executor.initialize()
        log.info("live_executor_initialized")

        # --- Initialize gasless merger (Builder Relayer) ---
        gasless_merger = GaslessMerger(
            private_key=config.credentials.private_key,
            builder_api_key=config.credentials.builder_api_key,
            builder_secret=config.credentials.builder_secret,
            builder_passphrase=config.credentials.builder_passphrase,
            relayer_url=config.credentials.builder_relayer_url,
            chain_id=config.credentials.chain_id,
        )
        gasless_ok = await gasless_merger.initialize()
        if gasless_ok:
            log.info("gasless_merger_ready",
                     msg="Will use gasless relayer for merge operations")
        else:
            log.warning("gasless_merger_unavailable",
                        msg="Will fall back to on-chain merge (requires POL for gas)")

        # --- Initialize on-chain CTF ops (fallback for merge) ---
        ctf_ops = CTFOperations(
            private_key=config.credentials.private_key,
            rpc_url=config.credentials.polygon_rpc_url,
            dry_run=False,
        )
        await ctf_ops.initialize()

        # --- Initialize balance monitor (auto-merge on low USDC) ---
        if config.balance_monitor.enabled:
            balance_monitor = BalanceMonitor(
                private_key=config.credentials.private_key,
                rpc_url=config.credentials.polygon_rpc_url,
                warn_balance=config.balance_monitor.warn_balance,
                merge_balance=config.balance_monitor.merge_balance,
                min_merge_pairs=config.balance_monitor.min_merge_pairs,
                check_interval=config.balance_monitor.check_interval,
            )
            bal_ok = await balance_monitor.initialize()
            if bal_ok:
                initial_bal = await balance_monitor.get_usdc_balance()
                log.info("balance_monitor_ready",
                         balance=f"${initial_bal:.2f}",
                         merge_at=f"${config.balance_monitor.merge_balance:.2f}")
            else:
                log.warning("balance_monitor_failed",
                            msg="Balance monitoring disabled")
                balance_monitor = None

    # Order manager
    order_manager = OrderManager(
        executor=executor,
        reprice_threshold=config.global_params.reprice_threshold,
    )

    # --- Create per-asset market cyclers ---
    cyclers = []
    tasks = []

    def dashboard_update(state):
        dashboard.update(state)

    for asset_name, ac in active_assets.items():
        # Per-asset inventory manager
        inventory = InventoryManager(
            soft_limit=ac.soft_limit,
            hard_limit=ac.hard_limit,
            emergency=ac.emergency,
            max_imbalance=ac.max_dollar_delta,  # Now share-based threshold
            max_capital_per_market=config.global_params.max_capital_per_market,
        )

        # Per-asset risk engine
        risk = RiskEngine(
            total_capital=config.global_params.total_capital,
            max_daily_loss_pct=config.global_params.max_daily_loss_pct,
            max_drawdown_pct=config.global_params.max_drawdown_pct,
        )

        cycler = MarketCycler(
            asset=asset_name,
            asset_config=ac,
            global_config=config.global_params,
            price_feed=price_feed,
            order_manager=order_manager,
            market_discovery=discovery,
            book_reader=book_reader,
            inventory_manager=inventory,
            risk_engine=risk,
            pnl_tracker=pnl_tracker,
            dashboard_callback=dashboard_update,
            ctf_ops=ctf_ops,
            gasless_merger=gasless_merger,
            balance_monitor=balance_monitor,
        )
        cyclers.append(cycler)

    # --- Start all tasks ---
    log.info("starting_tasks", count=len(cyclers) + 1)

    # Price feed task
    price_task = asyncio.create_task(price_feed.start())
    tasks.append(price_task)

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

    # Market cycler tasks
    for cycler in cyclers:
        cycler_by_asset[cycler.asset] = cycler
        task = asyncio.create_task(cycler.run())
        tasks.append(task)

    # --- Real-time price callback: pipe every WS tick directly into dashboard ---
    def on_live_price(symbol: str, price: float, ts: float):
        asset_name = symbol_to_asset.get(symbol.upper())
        if not asset_name:
            return
        cycler = cycler_by_asset.get(asset_name)
        spread = getattr(cycler, 'chainlink_spread', 0) if cycler else 0
        adjusted = price + spread
        
        # Compute real-time live FV for the dashboard
        live_fv = None
        if cycler and getattr(cycler, 'fair_value_model', None) is not None:
            sigma = cycler.vol_estimator.sigma_for_model() if hasattr(cycler, 'vol_estimator') else cycler.ac.default_sigma
            # We use 'adjusted' price to match Chainlink assumption
            live_fv = cycler.fair_value_model.fair_value(adjusted, sigma, ts)
            
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

    log.info("bot_running", assets=list(active_assets.keys()), mode=mode)

    # Switch to dashboard-only output (suppress console, keep file logs)
    from src.monitoring.logger import suppress_console, restore_console
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

    dash_task = asyncio.create_task(dashboard_loop())
    tasks.append(dash_task)

    # --- Wait for shutdown (Windows-compatible) ---
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # --- Graceful shutdown ---
    # Re-enable console logging for shutdown messages
    restore_console()
    log.info("shutting_down")

    # Stop all cyclers
    for cycler in cyclers:
        await cycler.stop()

    # Cancel all orders
    await order_manager.cancel_all()

    # Stop price feed
    await price_feed.stop()

    # Cancel remaining tasks
    for task in tasks:
        task.cancel()

    # Close clients
    await discovery.close()
    await book_reader.close()

    # Final P&L report
    snap = pnl_tracker.snapshot()

    print("\n" + "=" * 60)
    print(f"  SESSION COMPLETE — {mode.upper()}")
    print(f"  Duration: {pnl_tracker.session_duration_hours:.2f} hours")
    print(f"  Markets Settled: {snap.markets_settled}")
    print(f"  Total Fills: {snap.total_fills}")
    print(f"  Total Volume: ${snap.total_volume:.2f}")
    print(f"  Total Shares: {snap.total_shares:.0f}")
    print("-" * 60)
    print(f"  Trading P&L:     ${snap.net_trading_pnl:.4f}")
    print(f"  Est. Rebates:    ${snap.est_rebates:.4f}   ")
    print(f"  Rebates/Hour:    ${pnl_tracker.rebates_per_hour():.4f}")
    print(f"  Net P&L (total): ${snap.net_pnl_with_rebates:.4f}")
    print("=" * 60)


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

    # Banner
    print("\n" + "=" * 60)
    print("  POLYMARKET MARKET-MAKER BOT")
    print(f"  Mode: {args.mode.upper()}")
    print(f"  Assets: {args.assets or 'ALL ENABLED'}")
    print("  Strategy: BUY-ONLY | post_only=True")
    print("  Markets: Up/Down 15-minute crypto binaries")
    print("=" * 60 + "\n")

    if args.mode == "live":
        # Safety confirmation for live mode
        print("[WARNING] LIVE MODE — Real money will be used!")
        confirm = input("Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            return

    # Run
    try:
        asyncio.run(run_bot(config, args.assets))
    except KeyboardInterrupt:
        print("\nBot stopped by user.")


if __name__ == "__main__":
    main()
