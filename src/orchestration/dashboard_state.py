"""Dashboard state helpers for MarketCycler.

These functions are intentionally thin and operate on the cycler instance to
preserve the existing dashboard payload shape while keeping MarketCycler focused
on lifecycle orchestration.
"""

import time as _time

from src.services.inventory import has_negative_matched_pair_edge


def set_dashboard_event(cycler, level: str, reason: str, detail: str = "") -> None:
    cycler._dashboard_event = {
        "event_level": level,
        "event_reason": reason,
        "event_detail": detail,
        "event_ts": _time.time(),
    }


def clear_dashboard_event(cycler) -> None:
    cycler._dashboard_event = {}


def update_dashboard_waiting(cycler) -> None:
    if not cycler._dashboard_cb:
        return

    spot = getattr(cycler.price_feed, "prices", {}).get(cycler.ac.symbol, 0)
    price_age = (
        cycler.price_feed.get_price_age(cycler.ac.symbol)
        if hasattr(cycler.price_feed, "get_price_age")
        else 0
    )
    price_source = (
        cycler.price_feed.get_price_source(cycler.ac.symbol)
        if hasattr(cycler.price_feed, "get_price_source")
        else "unknown"
    )
    mt5_bridge_status = (
        cycler.price_feed.get_mt5_bridge_status(cycler.ac.symbol)
        if hasattr(cycler.price_feed, "get_mt5_bridge_status")
        else {}
    )

    state = {
        "asset": cycler.asset,
        "market_id": "waiting...",
        "slug": "Waiting for next Polymarket window...",
        "question": "",
        "start_price": 0,
        "spot_price": spot,
        "raw_spot": spot,
        "chainlink_spread": 0,
        "price_age": price_age,
        "price_source": price_source,
        "mt5_bridge": mt5_bridge_status,
        "fair_value": 0,
        "sigma": 0,
        "ws_ticks": getattr(cycler.price_feed, "ticks", 0),
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
        "net_trading_pnl": cycler.pnl.net_trading_pnl,
        "outcome_pnl": cycler.pnl.outcome_pnl,
        "est_rebates": cycler.pnl.est_rebates,
        "net_pnl": cycler.pnl.net_pnl,
        "economic_pnl": cycler.pnl.economic_pnl,
        "rebates_per_hour": cycler.pnl.rebates_per_hour(),
        "total_volume": cycler.pnl.total_volume,
        "total_shares": cycler.pnl.total_shares,
        "markets_settled": cycler.pnl.markets_settled,
        "total_fills": cycler.pnl.total_fills,
        "starting_capital": getattr(cycler.pnl, "starting_capital", 0),
        "current_capital": getattr(cycler.pnl, "current_capital", 0),
    }

    _add_balance_monitor_stats(cycler, state)
    cycler._dashboard_cb(state)


def dashboard_sigma_for_stale_spot(cycler) -> float:
    """Best dashboard sigma when spot is stale and model updates are paused."""
    if cycler.last_sigma and cycler.last_sigma > 0:
        return cycler.last_sigma
    try:
        sigma = cycler.vol_estimator.sigma_for_model()
        if sigma and sigma > 0:
            return sigma
    except Exception:
        pass
    return float(getattr(cycler.ac, "default_sigma", 0) or 0)


def update_dashboard(cycler, market, spot, fv, sigma, phase,
                     remaining, quotes=None, pos=None,
                     delta=0, inv_state="NORMAL") -> None:
    """Push state to dashboard callback."""
    if not cycler._dashboard_cb:
        return

    start_price = cycler.fair_value_model.start_price if cycler.fair_value_model else 0

    # Local inventory keeps cost basis/P&L. In live mode, wallet/ERC1155
    # balances are the display/position truth because CLOB fills can lag.
    real_pos = cycler.inventory.get_or_create(market.market_id, cycler.asset)
    real_delta = real_pos.dollar_delta(fv) if fv else 0
    real_state = cycler.inventory.get_state(market.market_id, fv)
    wallet_snapshot = getattr(cycler, "_wallet_truth_by_market", {}).get(market.market_id)
    display_up_shares = real_pos.yes_shares
    display_down_shares = real_pos.no_shares
    display_imbalance = real_pos.share_imbalance()
    display_matched_pairs = real_pos.matched_pairs()
    display_delta = real_delta
    inventory_source = "local"
    if wallet_snapshot:
        display_up_shares = float(wallet_snapshot.get("yes_shares", 0) or 0)
        display_down_shares = float(wallet_snapshot.get("no_shares", 0) or 0)
        display_imbalance = float(wallet_snapshot.get("share_imbalance", display_up_shares - display_down_shares) or 0)
        display_matched_pairs = float(wallet_snapshot.get("matched_pairs", min(display_up_shares, display_down_shares)) or 0)
        display_delta = (display_up_shares * fv - display_down_shares * (1 - fv)) if fv else 0
        inventory_source = "wallet"

    raw_spot = getattr(cycler.price_feed, "prices", {}).get(cycler.ac.symbol, spot)
    price_age = (
        cycler.price_feed.get_price_age(cycler.ac.symbol)
        if hasattr(cycler.price_feed, "get_price_age")
        else 0
    )
    price_source = (
        cycler.price_feed.get_price_source(cycler.ac.symbol)
        if hasattr(cycler.price_feed, "get_price_source")
        else "unknown"
    )
    mt5_bridge_status = (
        cycler.price_feed.get_mt5_bridge_status(cycler.ac.symbol)
        if hasattr(cycler.price_feed, "get_mt5_bridge_status")
        else {}
    )

    state = {
        "asset": cycler.asset,
        "market_id": market.market_id,
        "slug": market.slug,
        "question": market.question,
        "start_price": start_price or 0,
        "spot_price": spot or 0,
        "raw_spot": raw_spot or 0,
        "chainlink_spread": getattr(cycler, "chainlink_spread", 0),
        "price_age": price_age,
        "price_source": price_source,
        "mt5_bridge": mt5_bridge_status,
        "fair_value": fv,
        "sigma": sigma,
        "ws_ticks": getattr(cycler.price_feed, "ticks", 0),
        "phase": phase,
        "time_remaining": remaining,
        "regime": cycler.regime_filter.regime(),
        "up_buy": quotes.yes_buy_price if quotes else 0,
        "down_buy": quotes.no_buy_price if quotes else 0,
        "up_size": quotes.yes_buy_size if quotes else 0,
        "down_size": quotes.no_buy_size if quotes else 0,
        "combined_cost": quotes.combined_cost if quotes else 0,
        "edge": quotes.edge_per_pair if quotes else 0,
        "up_shares": display_up_shares,
        "down_shares": display_down_shares,
        "up_avg": real_pos.yes_avg_entry,
        "down_avg": real_pos.no_avg_entry,
        "share_imbalance": display_imbalance,
        "dollar_delta": display_delta,
        "matched_pairs": display_matched_pairs,
        "avg_pair_cost": real_pos.avg_matched_pair_cost(),
        "matched_pair_pnl": real_pos.matched_pair_profit(),
        "negative_pair_edge": (
            cycler._decide_negative_pair_edge(real_pos).triggered
            if hasattr(cycler, "_decide_negative_pair_edge")
            else has_negative_matched_pair_edge(real_pos)
        ),
        "inventory_source": inventory_source,
        "inv_state": real_state.value,
        "net_trading_pnl": cycler.pnl.net_trading_pnl,
        "outcome_pnl": cycler.pnl.outcome_pnl,
        "est_rebates": cycler.pnl.est_rebates,
        "net_pnl": cycler.pnl.net_pnl,
        "economic_pnl": cycler.pnl.economic_pnl,
        "rebates_per_hour": cycler.pnl.rebates_per_hour(),
        "total_volume": cycler.pnl.total_volume,
        "total_shares": cycler.pnl.total_shares,
        "markets_settled": cycler.pnl.markets_settled,
        "total_fills": cycler.pnl.total_fills,
        "starting_capital": getattr(cycler.pnl, "starting_capital", 0),
        "current_capital": getattr(cycler.pnl, "current_capital", 0),
    }
    if getattr(cycler, "_dashboard_event", None):
        state.update(cycler._dashboard_event)

    _add_balance_monitor_stats(cycler, state)
    cycler._dashboard_cb(state)


def _add_balance_monitor_stats(cycler, state: dict) -> None:
    if not cycler.balance_monitor:
        return
    bm_stats = cycler.balance_monitor.stats
    state["wallet_balance"] = bm_stats["last_balance"]
    state["auto_merges"] = bm_stats["total_merges"]
    state["auto_merged_usdc"] = bm_stats["total_merged_usdc"]
    state["balance_warn_threshold"] = cycler.balance_monitor.warn_balance
    state["balance_merge_threshold"] = cycler.balance_monitor.merge_balance
    state["merge_message"] = bm_stats.get("merge_message", "")
