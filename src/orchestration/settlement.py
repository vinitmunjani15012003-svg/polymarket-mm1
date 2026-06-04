"""Settlement orchestration helpers for MarketCycler.

Functions in this module intentionally accept the cycler instance to preserve
current behavior while moving non-quoting lifecycle work out of market_cycler.
"""

import asyncio
import time as _time

from src.execution.ctf_ops import infer_collateral_token_for_market
from src.monitoring.logger import get_logger

log = get_logger("market_cycler")


async def settle_market(cycler) -> None:
    """Clean up after a market expires, merge pairs and redeem winnings."""
    if cycler.current_market:
        market = cycler.current_market
        await cycler.order_mgr.cancel_market_quotes(market.market_id)

        # Get final position
        pos = cycler.inventory.get_or_create(market.market_id, cycler.asset)
        pairs = pos.matched_pairs()

        log.info(
            "market_settling",
            asset=cycler.asset,
            slug=market.slug,
            up_shares=pos.yes_shares,
            down_shares=pos.no_shares,
            matched_pairs=pairs,
        )

        # --- CTF Operations ---
        if pairs > 0:
            # Use gasless merger if available, else on-chain
            condition_id = getattr(market, "condition_id", None)
            if condition_id:
                amount = int(pairs * 1e6)  # Convert to USDC base units
                tx = None
                collateral_token = getattr(cycler.gasless_merger, "_collateral_token", "")
                if cycler.balance_monitor and getattr(cycler.balance_monitor, "_ctf", None):
                    collateral_token = infer_collateral_token_for_market(
                        cycler.balance_monitor._w3,
                        cycler.balance_monitor._ctf,
                        condition_id,
                        getattr(market, "token_id_up", ""),
                        getattr(market, "token_id_down", ""),
                        collateral_token,
                    )

                # Prefer gasless merge
                if cycler.gasless_merger and cycler.gasless_merger.is_available:
                    tx = await cycler.gasless_merger.merge_positions(
                        condition_id, amount, collateral_token=collateral_token
                    )

                # Fallback to on-chain
                if not tx and cycler.ctf:
                    tx = await cycler.ctf.merge_positions(
                        condition_id, amount, collateral_token=collateral_token
                    )

                if tx:
                    # ERC20 approvals are permanent (MAX_UINT256) and set at
                    # startup. Post-merge we only need to sync the CLOB's
                    # indexed view so the returned USDC.e is credited as cash.
                    sync_balance = getattr(cycler.order_mgr.executor, "sync_balance_allowance", None)
                    if callable(sync_balance):
                        # Wait for CLOB indexer to catch up with on-chain merge
                        # state before syncing balance/allowance.
                        await asyncio.sleep(3)
                        sync_ok = False
                        for attempt in range(1, 6):
                            try:
                                sync_ok = bool(await sync_balance())
                            except Exception as e:
                                log.warning(
                                    "post_settle_merge_balance_sync_error",
                                    asset=cycler.asset,
                                    attempt=attempt,
                                    error=str(e),
                                )
                                sync_ok = False
                            if sync_ok:
                                break
                            await asyncio.sleep(min(2 * attempt, 8))
                        if sync_ok:
                            log.info("post_settle_merge_balance_allowance_synced", asset=cycler.asset)
                        else:
                            log.warning("post_settle_merge_balance_allowance_sync_failed", asset=cycler.asset)
                    pair_profit = pos.matched_pair_profit()
                    cycler.pnl.record_settlement(pair_profit, market.market_id)
                    cycler.pnl.record_capital_recovery(pairs * 1.0)
                    pos.acknowledge_settlement()
                    log.info(
                        "pairs_merged",
                        pairs=pairs,
                        profit=f"${pair_profit:.4f}",
                        tx=str(tx)[:16] if tx else "none",
                    )

        # Try to redeem any remaining tokens (if market resolved)
        if cycler.ctf or cycler.gasless_merger:
            condition_id = getattr(market, "condition_id", None)
            if condition_id:
                resolved = await cycler.ctf.is_market_resolved(condition_id) if cycler.ctf else False
                if resolved:
                    tx = None
                    if cycler.gasless_merger and cycler.gasless_merger.is_available:
                        tx = await cycler.gasless_merger.redeem_positions(condition_id)
                    elif cycler.ctf:
                        log.error(
                            "gasless_redeem_unavailable",
                            msg="Gasless redeem unavailable; on-chain fallback disabled by policy",
                        )
                    if tx:
                        # Calculate redemption value for unmatched tokens
                        unmatched_up = pos.yes_shares - pairs
                        unmatched_down = pos.no_shares - pairs
                        log.info(
                            "tokens_redeemed",
                            unmatched_up=unmatched_up,
                            unmatched_down=unmatched_down,
                            tx=tx[:16] if tx else "none",
                        )

        # Simulate redemption of unmatched tokens in Dry-Run
        elif not cycler.ctf and not cycler.gasless_merger:
            if pairs > 0:
                pair_profit = pos.matched_pair_profit()
                cycler.pnl.record_settlement(pair_profit, market.market_id)
                cycler.pnl.record_capital_recovery(pairs * 1.0)
                pos.acknowledge_settlement()
                log.info("dry_run_pairs_merged", pairs=pairs, profit=f"${pair_profit:.4f}")

            unmatched_up = pos.yes_shares - pairs
            unmatched_down = pos.no_shares - pairs

            # Always track/record the real outcome (even if flat). This is useful
            # for analyzing market behavior and verifying the model.
            pos_snapshot = {
                "yes_avg_entry": pos.yes_avg_entry,
                "no_avg_entry": pos.no_avg_entry,
                "unmatched_up": unmatched_up,
                "unmatched_down": unmatched_down,
            }

            # Persist a pending resolution record so the next run can finish it even
            # if this process exits (timeout/restart).
            sm = getattr(cycler.inventory, "state_manager", None)
            if sm:
                try:
                    sm.add_pending_resolution(
                        {
                            "slug": market.slug,
                            "asset": market.asset,
                            "window_start_ts": int(market.window_start_ts),
                            "market_id": market.market_id,
                            "yes_avg_entry": pos_snapshot["yes_avg_entry"],
                            "no_avg_entry": pos_snapshot["no_avg_entry"],
                            "unmatched_up": pos_snapshot["unmatched_up"],
                            "unmatched_down": pos_snapshot["unmatched_down"],
                            "created_ts": _time.time(),
                        }
                    )
                except Exception as ex:
                    log.debug("pending_resolution_persist_failed", slug=market.slug, error=str(ex))

            # Kick off background task to wait for actual resolution from Gamma API
            asyncio.create_task(cycler._wait_and_settle_unmatched(market, pos_snapshot))

        # Clear position from inventory state
        cycler.inventory.clear_market(market.market_id)

        cycler.current_market = None

    # Reset per-market state for next cycle
    cycler.quote_engine.reset_params()
    if not cycler.portfolio_pnl_getter:
        cycler.risk_engine.reset_for_new_market(cycler.pnl.net_pnl)


async def wait_and_settle_unmatched(cycler, market, pos_snapshot: dict) -> None:
    """Background task to poll Gamma API and wait for actual market resolution."""
    await cycler._wait_and_settle_unmatched_by_fields(
        asset=market.asset,
        slug=market.slug,
        window_start_ts=int(market.window_start_ts),
        market_id=market.market_id,
        pos_snapshot=pos_snapshot,
    )


async def wait_and_settle_unmatched_by_fields(
    cycler,
    asset: str,
    slug: str,
    window_start_ts: int,
    market_id: str,
    pos_snapshot: dict,
) -> None:
    """Poll Gamma until the market is inactive, then record outcome.

    NOTE: We require m.active == False to avoid false positives.
    """
    unmatched_up = pos_snapshot["unmatched_up"]
    unmatched_down = pos_snapshot["unmatched_down"]
    yes_avg = pos_snapshot["yes_avg_entry"]
    no_avg = pos_snapshot["no_avg_entry"]

    log.info("waiting_for_actual_resolution", slug=slug)

    while cycler._running:
        await asyncio.sleep(30)
        try:
            m = await cycler.discovery._fetch_market(asset, int(window_start_ts))
            if not m:
                continue

            # Require actual Gamma closed/inactive/archived status to prevent
            # volatility false positives while still supporting markets that remain
            # active=True after close.
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

            cycler.pnl.record_outcome_resolution(net_profit, market_id)
            cycler.pnl.record_capital_recovery(revenue)

            log.info(
                "dry_run_actual_resolution",
                slug=slug,
                winner=winner_str,
                winning_shares=winning_shares,
                losing_shares=losing_shares,
                outcome_pnl=round(net_profit, 4),
                pnl=f"${net_profit:.4f}",
            )

            sm = getattr(cycler.inventory, "state_manager", None)
            if sm:
                try:
                    sm.remove_pending_resolution(slug)
                except Exception as ex:
                    log.debug("pending_resolution_remove_failed", slug=slug, error=str(ex))
            break
        except Exception as e:
            log.error("wait_and_settle_error", slug=slug, error=str(e))
