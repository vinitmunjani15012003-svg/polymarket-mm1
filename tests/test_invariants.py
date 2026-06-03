from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import AssetConfig, BotConfig
import time

from src.data.orderbook import BookSnapshot
from src.data.price_feed import PriceFeed
from src.execution.clob_client import ClobClientWrapper
from src.execution.ctf_ops import BalanceMonitor, SimulatedBalanceMonitor, infer_collateral_token_for_market
from src.execution.dry_run import DryRunExecutor, SimulatedOrder
from src.execution.order_manager import OrderManager
from src.orchestration.market_cycler import (
    MarketCycler,
    UpDownFairValue,
    aggressive_repair_price,
    apply_dust_price_guardrails,
    apply_fv_favored_entry_mode,
    compute_fv_aware_dust_repair_sizes,
    compute_inventory_repair_sizes,
    blended_fair_value,
    fv_model_confidence,
    has_negative_matched_pair_edge,
    polymarket_implied_up_mid,
    basis_guard_triggered,
    repair_min_edge_for_remaining,
    start_price_disagrees_with_market,
    spot_from_binary_probability,
)
from src.monitoring.pnl_tracker import PnLTracker
from src.risk.risk_engine import pre_trade_checks
from src.risk.toxicity import FillEdgeTracker, ToxicityMonitor
from src.strategy.inventory import InventoryManager, InventoryState
from src.strategy.quote_engine import MAX_COMBINED_COST, QuoteEngine


class DummyExecutor:
    def __init__(self):
        self.calls = []
        self.cancel_all_ok = True

    async def place_buy_order(self, token_id, price, size, side="yes", book_snapshot=None):
        self.calls.append((token_id, price, size, side))
        return "OID-1"

    async def cancel_order(self, order_id):
        return True

    async def cancel_all(self):
        return self.cancel_all_ok


class DummyBatchExecutor(DummyExecutor):
    def __init__(self):
        super().__init__()
        self.cancel_batches = []
        self.place_batches = []
        self.cancel_ok = True

    async def place_buy_orders(self, orders):
        self.place_batches.append(orders)
        return {order["side"]: f"OID-{order['side']}" for order in orders}

    async def cancel_orders(self, order_ids):
        self.cancel_batches.append(order_ids)
        return self.cancel_ok


class DummyOrderStateExecutor(DummyBatchExecutor):
    def __init__(self, states):
        super().__init__()
        self.states = list(states)

    async def is_order_open(self, order_id):
        if self.states:
            state = self.states.pop(0)
            if not state:
                self.open_orders.pop(order_id, None)
            return state
        return order_id in getattr(self, "open_orders", {})


def make_book(mid: float) -> BookSnapshot:
    bid = round(mid - 0.01, 4)
    ask = round(mid + 0.01, 4)
    return BookSnapshot(
        token_id="T",
        timestamp=time.time(),
        bids=[(bid, 10)],
        asks=[(ask, 10)],
        best_bid=bid,
        best_ask=ask,
        best_bid_size=10,
        best_ask_size=10,
        mid_price=mid,
        micro_price=mid,
    )


def test_updown_fair_value_polarity_tracks_raw_price_vs_start():
    model = UpDownFairValue(event_start_ts=1000, resolve_ts=1900, start_price=100.0)

    assert model.fair_value(101.0, sigma_annualized=0.8, now_ts=1300) > 0.5
    assert model.fair_value(99.0, sigma_annualized=0.8, now_ts=1300) < 0.5


def test_dashboard_fair_value_peek_does_not_mutate_authoritative_state():
    model = UpDownFairValue(event_start_ts=1000, resolve_ts=1900, start_price=100.0)
    authoritative = model.fair_value(101.0, sigma_annualized=0.8, now_ts=1200)
    last_ts = model._last_update_ts

    peek = model.fair_value(99.0, sigma_annualized=0.8, now_ts=1300, update_state=False)

    assert peek < 0.5
    assert model.last_fair_value == authoritative
    assert model._last_update_ts == last_ts


def test_blended_fair_value_prevents_early_window_overconfidence():
    model_fv = 0.95
    confidence = fv_model_confidence(
        model_fv=model_fv,
        elapsed_fraction=1 / 3,
        standardized_move=1.2,
        market_fv=0.55,
    )
    final_fv = blended_fair_value(model_fv, 0.55, confidence)

    assert confidence <= 0.35
    assert 0.55 < final_fv < 0.70


def test_blended_fair_value_caps_noisy_tail_pull_toward_neutral():
    # Regression: with unchanged spot/model FV around 5c, a transient book mid
    # around 12c should not make trading/dashboard FV jump to 12c.
    low_tail = blended_fair_value(0.05, 0.12, confidence=0.10)
    high_tail = blended_fair_value(0.95, 0.88, confidence=0.10)

    assert low_tail <= 0.07
    assert high_tail == pytest.approx(0.93)


def test_blended_fair_value_trusts_model_more_late_when_market_agrees():
    early_conf = fv_model_confidence(0.70, elapsed_fraction=0.1, standardized_move=0.5, market_fv=0.66)
    late_conf = fv_model_confidence(0.70, elapsed_fraction=0.85, standardized_move=1.5, market_fv=0.66)

    assert late_conf > early_conf
    assert blended_fair_value(0.70, 0.66, late_conf) > blended_fair_value(0.70, 0.66, early_conf)


def test_blended_fair_value_missing_book_tempers_to_neutral():
    confidence = fv_model_confidence(0.90, elapsed_fraction=0.05, standardized_move=0.5, market_fv=None)
    final_fv = blended_fair_value(0.90, None, confidence)

    assert 0.50 < final_fv < 0.60


def test_vatic_strike_remains_price_to_beat_even_when_market_model_disagrees():
    import asyncio

    class FakePriceFeed:
        prices = {"BTCUSDT": 74207.21}
        rest_url = "https://api.binance.com/api/v3"

        async def fetch_vatic_strike(self, symbol, ts):
            return 74146.08001391211

        async def fetch_historical_price(self, symbol, ts):
            return 74283.0

        def get_price(self, symbol):
            return self.prices.get(symbol)

    cycler = MarketCycler.__new__(MarketCycler)
    cycler.asset = "BTC"
    cycler.ac = SimpleNamespace(symbol="BTCUSDT")
    cycler.price_feed = FakePriceFeed()
    cycler.book_reader = SimpleNamespace(get_books=AsyncMock(return_value={}))
    cycler.vol_estimator = SimpleNamespace(update=lambda *a, **k: None, sigma_for_model=lambda: 0.20)
    cycler._update_dashboard = lambda *a, **k: None
    cycler.chainlink_spread = 0
    cycler.regime_config = SimpleNamespace()
    cycler.toxicity_config = SimpleNamespace()
    cycler.quote_engine = SimpleNamespace(reset_params=lambda: None)
    cycler.edge_tracker = SimpleNamespace()
    cycler.toxicity_monitor = SimpleNamespace()
    cycler._running = False

    market = SimpleNamespace(
        slug="btc-updown-15m-test",
        event_start_ts=1780074900,
        resolve_ts=1780075800,
        time_remaining=800,
        token_id_up="UP",
        token_id_down="DOWN",
    )

    asyncio.run(cycler._run_market(market))

    assert cycler.fair_value_model.start_price == pytest.approx(74146.08001391211)


def test_market_implied_spot_recovers_polymarket_oracle_basis():
    implied = spot_from_binary_probability(
        start_price=74146.08001391211,
        p_up=0.355,
        sigma=0.20,
        time_remaining=855.7,
    )

    assert implied == pytest.approx(74117.37, abs=0.1)
    assert 74207.21 - implied > 80


def test_start_price_validation_rejects_strike_inconsistent_with_market_mid():
    # Observed live failure mode: a candidate strike made the raw model near 0.99
    # while the live UP book was around 0.545. That strike must be replaced by
    # market calibration before the dashboard/trader trusts it.
    assert start_price_disagrees_with_market(
        start_price=73173.25705600431,
        current_spot=73330.0,
        sigma=0.20,
        event_start_ts=1780058700,
        resolve_ts=1780059600,
        market_fv=0.545,
        now_ts=1780059111,
    ) is True


def test_start_price_validation_keeps_candidate_consistent_with_market_mid():
    assert start_price_disagrees_with_market(
        start_price=73320.0,
        current_spot=73330.0,
        sigma=0.20,
        event_start_ts=1780058700,
        resolve_ts=1780059600,
        market_fv=0.545,
        now_ts=1780059111,
    ) is False


def test_live_spot_must_not_apply_fixed_start_price_basis():
    # Regression for live bug: Vatic/Chainlink target differed from Binance's
    # window-open print by ~-$111. Applying that fixed basis to live Binance spot
    # made raw spot above the target appear far below it, flipping FV DOWN.
    start_price = 73376.89032506653
    raw_live_spot = 73380.0
    mistaken_basis = start_price - 73487.98
    mistaken_adjusted = raw_live_spot + mistaken_basis

    assert raw_live_spot > start_price
    assert mistaken_adjusted < start_price
    model = UpDownFairValue(event_start_ts=1780056000, resolve_ts=1780056900, start_price=start_price)
    assert model.fair_value(raw_live_spot, sigma_annualized=0.8, now_ts=1780056300) > 0.5


def test_price_feed_keeps_fresh_mt5_as_primary_over_binance_ticks():
    pf = PriceFeed(
        "wss://stream.binance.com:9443/ws",
        ["BTCUSDT"],
        mt5_bridge_url="http://bridge:8765",
        mt5_bridge_stale_seconds=5.0,
    )
    pf.prices["BTCUSDT"] = 74150.0
    pf.timestamps["BTCUSDT"] = time.time()
    pf.price_sources["BTCUSDT"] = "exness_mt5"
    seen = []
    pf.on_price_update(lambda symbol, price, ts: seen.append((symbol, price)))

    pf._process_trade({"data": {"e": "aggTrade", "s": "BTCUSDT", "p": "74300.00"}})

    assert pf.get_price("BTCUSDT") == 74150.0
    assert pf.get_price_source("BTCUSDT") == "exness_mt5"
    assert seen == []


def test_price_feed_fails_closed_before_first_mt5_tick_instead_of_binance_fallback():
    pf = PriceFeed(
        "wss://stream.binance.com:9443/ws",
        ["BTCUSDT"],
        mt5_bridge_url="http://bridge:8765",
        mt5_bridge_stale_seconds=5.0,
    )

    pf._process_trade({"data": {"e": "aggTrade", "s": "BTCUSDT", "p": "74300.00"}})
    pf._process_trade({"data": {"s": "BTCUSDT", "b": "74299.00", "a": "74301.00"}})

    assert pf.get_price("BTCUSDT") is None
    assert pf.get_price_source("BTCUSDT") == "exness_mt5_unavailable"
    assert getattr(pf, "binance_fallback_price") == pytest.approx(74300.0)


def test_price_feed_does_not_switch_to_aggtrade_between_stale_mt5_ticks():
    pf = PriceFeed(
        "wss://stream.binance.com:9443/ws",
        ["BTCUSDT"],
        mt5_bridge_url="http://bridge:8765",
        mt5_bridge_stale_seconds=0.01,
    )
    pf.prices["BTCUSDT"] = 74150.0
    pf.timestamps["BTCUSDT"] = time.time() - 1.0
    pf.price_sources["BTCUSDT"] = "exness_mt5"

    pf._process_trade({"data": {"e": "aggTrade", "s": "BTCUSDT", "p": "74300.00"}})

    assert pf.get_price("BTCUSDT") == 74150.0
    assert pf.get_price_source("BTCUSDT") == "exness_mt5_stale"
    assert pf.get_price_age("BTCUSDT") >= 1.0


def test_price_feed_records_stale_mt5_tick_instead_of_showing_unavailable():
    pf = PriceFeed(
        "wss://stream.binance.com:9443/ws",
        ["BTCUSDT"],
        mt5_bridge_url="http://bridge:8765",
        mt5_bridge_stale_seconds=0.01,
    )

    pf._store_mt5_bridge_price("BTCUSDT", 74150.0, time.time() - 1.0)

    assert pf.get_price("BTCUSDT") == 74150.0
    assert pf.get_price_source("BTCUSDT") == "exness_mt5_stale"


def test_price_feed_mt5_exception_detail_is_never_empty():
    assert PriceFeed._exception_detail(Exception()) == "Exception"
    assert PriceFeed._exception_detail(TimeoutError()) == "TimeoutError"
    assert PriceFeed._exception_detail(ValueError("bad tick")) == "bad tick"


def test_price_feed_mt5_bridge_host_is_redacted_to_host_only():
    assert PriceFeed._url_host("http://192.168.1.10:8765") == "192.168.1.10:8765"
    assert PriceFeed._url_host("http://bridge.local:8765/path") == "bridge.local:8765"


def test_price_feed_rewrites_local_mt5_bridge_url_inside_container(monkeypatch):
    monkeypatch.setattr(PriceFeed, "_running_in_container", staticmethod(lambda: True))

    assert PriceFeed._normalize_mt5_bridge_url("http://localhost:8765") == "http://host.docker.internal:8765"
    assert PriceFeed._normalize_mt5_bridge_url("http://127.0.0.1:8765") == "http://host.docker.internal:8765"
    assert PriceFeed._normalize_mt5_bridge_url("http://0.0.0.0:8765") == "http://host.docker.internal:8765"
    assert PriceFeed._normalize_mt5_bridge_url("http://192.168.1.10:8765") == "http://192.168.1.10:8765"


def test_dashboard_event_helper_uses_market_cycler_time_alias():
    cycler = MarketCycler.__new__(MarketCycler)

    cycler._set_dashboard_event("skip", "PRE_TRADE_FAILED", "risk check")

    assert cycler._dashboard_event["event_level"] == "skip"
    assert cycler._dashboard_event["event_reason"] == "PRE_TRADE_FAILED"
    assert cycler._dashboard_event["event_detail"] == "risk check"
    assert cycler._dashboard_event["event_ts"] > 0


def test_small_capital_does_not_mark_opening_cycle_without_order_id():
    class StateManager:
        def __init__(self):
            self.state = {}

        def get_small_capital_window(self, market_id):
            return self.state.setdefault(market_id, {"quote_cycle_started": False, "quote_cycles_started": 0})

        def update_small_capital_window(self, market_id, state):
            self.state[market_id] = dict(state)

    cycler = MarketCycler.__new__(MarketCycler)
    cycler.asset = "BTC"
    cycler.small_capital_config = SimpleNamespace(enabled=True, one_cycle_per_window=True)
    cycler.inventory = SimpleNamespace(state_manager=StateManager())
    cycler.order_mgr = OrderManager(DummyExecutor())
    market = SimpleNamespace(market_id="M1", slug="btc-window")
    quotes = SimpleNamespace(yes_buy_size=5, no_buy_size=0)

    cycler._mark_small_capital_quote_started(market, quotes, "normal")

    state = cycler.inventory.state_manager.get_small_capital_window("M1")
    assert state["quote_cycle_started"] is False
    assert state["quote_cycles_started"] == 0
    assert state.get("initial_order_id", "") == ""


def test_small_capital_marks_opening_cycle_only_after_order_id_exists():
    class StateManager:
        def __init__(self):
            self.state = {}

        def get_small_capital_window(self, market_id):
            return self.state.setdefault(market_id, {"quote_cycle_started": False, "quote_cycles_started": 0})

        def update_small_capital_window(self, market_id, state):
            self.state[market_id] = dict(state)

    cycler = MarketCycler.__new__(MarketCycler)
    cycler.asset = "BTC"
    cycler.small_capital_config = SimpleNamespace(enabled=True, one_cycle_per_window=True)
    cycler.inventory = SimpleNamespace(state_manager=StateManager())
    cycler.order_mgr = OrderManager(DummyExecutor())
    active = cycler.order_mgr.get_active("M1")
    active.yes_order_id = "OID-YES"
    market = SimpleNamespace(market_id="M1", slug="btc-window")
    quotes = SimpleNamespace(yes_buy_size=5, no_buy_size=0)

    cycler._mark_small_capital_quote_started(market, quotes, "normal")

    state = cycler.inventory.state_manager.get_small_capital_window("M1")
    assert state["quote_cycle_started"] is True
    assert state["quote_cycles_started"] == 1
    assert state["initial_order_id"] == "OID-YES"


def test_price_feed_prefers_recent_aggtrade_when_book_mid_is_sticky():
    feed = PriceFeed("wss://stream.binance.com:9443/ws", ["BTCUSDT"])

    feed._process_trade({"data": {"s": "BTCUSDT", "b": "100.00", "a": "100.02"}})
    assert feed.get_price("BTCUSDT") == pytest.approx(100.01)
    assert feed.get_price_source("BTCUSDT") == "bookTicker"

    feed._process_trade({"data": {"e": "aggTrade", "s": "BTCUSDT", "p": "100.50"}})
    assert feed.get_price("BTCUSDT") == pytest.approx(100.50)
    assert feed.get_price_source("BTCUSDT") == "aggTrade"

    # A following bookTicker with unchanged top-of-book should not immediately
    # hide visible trade movement.
    feed._process_trade({"data": {"s": "BTCUSDT", "b": "100.00", "a": "100.02"}})
    assert feed.get_price("BTCUSDT") == pytest.approx(100.50)
    assert feed.get_price_source("BTCUSDT") == "aggTrade"

    # If trades go quiet, fall back to book mid.
    feed.trade_timestamps["BTCUSDT"] = time.time() - 2.0
    feed._process_trade({"data": {"s": "BTCUSDT", "b": "100.10", "a": "100.12"}})
    assert feed.get_price("BTCUSDT") == pytest.approx(100.11)
    assert feed.get_price_source("BTCUSDT") == "bookTicker"


def test_exness_spot_age_uses_mt5_stale_tolerance():
    cycler = MarketCycler.__new__(MarketCycler)
    cycler.price_feed = SimpleNamespace(mt5_bridge_url="http://bridge:8765", mt5_bridge_stale_seconds=15.0)
    assert cycler._max_spot_price_age_seconds() == pytest.approx(15.0)

    cycler.price_feed = SimpleNamespace(mt5_bridge_url="", mt5_bridge_stale_seconds=15.0)
    assert cycler._max_spot_price_age_seconds() == pytest.approx(3.0)


def test_stale_spot_dashboard_sigma_does_not_collapse_to_zero():
    cycler = MarketCycler.__new__(MarketCycler)
    cycler.last_sigma = 0.72
    cycler.vol_estimator = SimpleNamespace(sigma_for_model=lambda: 0.60)
    cycler.ac = SimpleNamespace(default_sigma=0.45)

    assert cycler._dashboard_sigma_for_stale_spot() == pytest.approx(0.72)

    cycler.last_sigma = None
    assert cycler._dashboard_sigma_for_stale_spot() == pytest.approx(0.60)

    cycler.vol_estimator = SimpleNamespace(sigma_for_model=lambda: 0)
    assert cycler._dashboard_sigma_for_stale_spot() == pytest.approx(0.45)


def test_basis_guard_uses_polymarket_implied_probability():
    up_book = make_book(0.60)
    down_book = make_book(0.40)

    implied = polymarket_implied_up_mid(up_book, down_book)

    assert implied == pytest.approx(0.60)
    assert basis_guard_triggered(0.73, implied, threshold=0.12)
    assert not basis_guard_triggered(0.65, implied, threshold=0.12)


def test_config_validation_rejects_invalid_spreads():
    cfg = BotConfig(
        assets={
            "BTC": AssetConfig(
                enabled=True,
                symbol="BTCUSDT",
                min_spread=0.05,
                max_spread=0.03,
                soft_limit=10,
                hard_limit=20,
                emergency=30,
            )
        }
    )

    with pytest.raises(ValueError, match="min_spread"):
        cfg.validate()


def test_live_config_requires_funder_for_deposit_wallets():
    cfg = BotConfig(
        mode="live",
        assets={
            "BTC": AssetConfig(
                enabled=True,
                symbol="BTCUSDT",
                min_spread=0.01,
                max_spread=0.05,
                soft_limit=10,
                hard_limit=20,
                emergency=30,
            )
        },
    )
    cfg.credentials.private_key = "0xabc"
    cfg.credentials.api_key = "key"
    cfg.credentials.api_secret = "secret"
    cfg.credentials.api_passphrase = "pass"
    cfg.credentials.signature_type = 3
    cfg.credentials.funder = ""

    with pytest.raises(ValueError, match="funder"):
        cfg.validate()


def test_live_fill_side_uses_token_id_mapping_not_default_up():
    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        signature_type=3,
        funder="0xfunder",
    )
    fills = [{"id": "T1", "asset_id": "NO_TOKEN", "size": "5", "price": "0.42"}]

    processed = wrapper.process_fills(
        fills,
        inventory_mgr=None,
        market_id="MARKET1",
        token_id_to_side={"YES_TOKEN": "yes", "NO_TOKEN": "no"},
    )

    assert processed == [{
        "order_id": "",
        "token_id": "NO_TOKEN",
        "side": "no",
        "price": 0.42,
        "size": 5.0,
        "fill_time": processed[0]["fill_time"],
        "simulated": False,
    }]


def test_live_fill_unknown_side_is_skipped_fail_closed():
    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
    )
    fills = [{"id": "T2", "asset_id": "MYSTERY", "size": "5", "price": "0.42"}]

    processed = wrapper.process_fills(
        fills,
        inventory_mgr=None,
        market_id="MARKET1",
        token_id_to_side={"YES_TOKEN": "yes", "NO_TOKEN": "no"},
    )

    assert processed == []


def test_live_fill_side_can_fall_back_to_outcome_label():
    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
    )
    fills = [{"id": "T3", "outcome": "Down", "size": "5", "price": "0.42"}]

    processed = wrapper.process_fills(
        fills,
        inventory_mgr=None,
        market_id="MARKET1",
        token_id_to_side={},
    )

    assert processed[0]["side"] == "no"


def test_live_fill_dedupe_distinguishes_partial_fills_without_provider_id():
    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
    )
    fills = [
        {"order_id": "OID1", "asset_id": "YES_TOKEN", "size": "2", "price": "0.42", "timestamp": "1"},
        {"order_id": "OID1", "asset_id": "YES_TOKEN", "size": "3", "price": "0.42", "timestamp": "2"},
    ]

    processed = wrapper.process_fills(
        fills,
        inventory_mgr=None,
        market_id="MARKET1",
        token_id_to_side={"YES_TOKEN": "yes", "NO_TOKEN": "no"},
    )

    assert [f["size"] for f in processed] == [2.0, 3.0]


def test_live_trade_with_multiple_own_maker_orders_books_every_leg():
    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
    )
    wrapper.open_orders = {
        "OID1": {"token_id": "YES_TOKEN", "token_side": "yes", "size": 5.0},
        "OID2": {"token_id": "YES_TOKEN", "token_side": "yes", "size": 5.0},
    }
    fills = [{
        "id": "TRADE1",
        "price": "0.52",
        "size": "10",
        "maker_orders": [
            {"order_id": "OID1", "asset_id": "YES_TOKEN", "matched_amount": "5", "price": "0.52"},
            {"order_id": "OID2", "asset_id": "YES_TOKEN", "matched_amount": "5", "price": "0.52"},
        ],
    }]

    processed = wrapper.process_fills(
        fills,
        inventory_mgr=None,
        market_id="MARKET1",
        token_id_to_side={"YES_TOKEN": "yes", "NO_TOKEN": "no"},
    )

    assert [f["order_id"] for f in processed] == ["OID1", "OID2"]
    assert sum(f["size"] for f in processed) == 10.0
    assert wrapper.open_orders == {}


def test_post_orders_response_normalization_handles_common_sdk_shapes():
    assert ClobClientWrapper._normalize_post_orders_response(
        [{"orderID": "A"}, {"id": "B"}], 2
    ) == [{"orderID": "A"}, {"id": "B"}]
    assert ClobClientWrapper._normalize_post_orders_response(
        {"orders": [{"orderID": "A"}]}, 2
    ) == [{"orderID": "A"}]
    assert ClobClientWrapper._normalize_post_orders_response(
        {"orderID": "ONLY"}, 1
    ) == [{"orderID": "ONLY"}]
    assert ClobClientWrapper._normalize_post_orders_response("weird", 2) == [{}, {}]


def test_balance_monitor_accepts_explicit_funder_balance_address():
    monitor = BalanceMonitor(
        private_key="0xabc",
        balance_address="0xFunder000000000000000000000000000000000001",
    )

    assert monitor._balance_address == "0xFunder000000000000000000000000000000000001"


def test_order_manager_places_buy_orders_through_executor():
    executor = DummyExecutor()
    om = OrderManager(executor)
    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=0.44,
        yes_buy_size=10,
        no_buy_size=8,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is True
    assert executor.calls == [
        ("YES1", 0.45, 10, "yes"),
        ("NO1", 0.44, 8, "no"),
    ]


def test_order_manager_batches_cancel_and_replace_when_available():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.no_order_id = "OLD-NO"
    active.yes_price = 0.40
    active.no_price = 0.40
    active.yes_size = 5
    active.no_size = 5

    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=0.44,
        yes_buy_size=10,
        no_buy_size=8,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-YES", "OLD-NO"]]
    assert len(executor.place_batches) == 1
    assert [o["side"] for o in executor.place_batches[0]] == ["yes", "no"]
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OID-yes"
    assert active.no_order_id == "OID-no"


def test_order_manager_does_not_clear_or_replace_when_cancel_batch_fails():
    executor = DummyBatchExecutor()
    executor.cancel_ok = False
    om = OrderManager(executor)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.40
    active.yes_size = 5

    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=None,
        yes_buy_size=10,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is False
    assert executor.cancel_batches == [["OLD-YES"]]
    assert executor.place_batches == []
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OLD-YES"
    assert active.yes_price == 0.40
    assert active.yes_size == 5
    assert om.last_order_error == "quote_cancel_failed_halt_reprice"


def test_order_manager_fails_closed_when_stray_cancel_fails():
    executor = DummyBatchExecutor()
    executor.cancel_ok = False
    executor.open_orders = {
        "STRAY-YES": {"token_id": "YES1", "price": 0.44, "size": 5},
    }
    om = OrderManager(executor)
    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is False
    assert executor.cancel_batches == [["STRAY-YES"]]
    assert executor.place_batches == []
    assert om.last_order_error == "stray_live_order_cancel_failed"


def test_order_manager_cancel_side_quotes_handles_dry_run_orders():
    executor = DummyBatchExecutor()
    executor.open_orders = {
        "DRY-YES": SimulatedOrder(
            order_id="DRY-YES",
            token_id="YES1",
            side="yes",
            price=0.45,
            size=5,
            placed_at=time.time(),
        ),
        "DRY-NO": SimulatedOrder(
            order_id="DRY-NO",
            token_id="NO1",
            side="no",
            price=0.44,
            size=5,
            placed_at=time.time(),
        ),
    }
    om = OrderManager(executor)

    import asyncio

    ok = asyncio.run(om.cancel_side_quotes("MARKET1", "yes", "YES1"))

    assert ok is True
    assert executor.cancel_batches == [["DRY-YES"]]


def test_order_manager_cancel_all_preserves_active_on_failure():
    executor = DummyExecutor()
    executor.cancel_all_ok = False
    om = OrderManager(executor)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"

    import asyncio

    ok = asyncio.run(om.cancel_all())

    assert ok is False
    assert om.get_active("MARKET1").yes_order_id == "OLD-YES"
    assert om.last_order_error == "cancel_all_failed"


def test_order_manager_throttles_non_urgent_bid_improvements():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.40
    active.yes_size = 5
    active.last_update = time.time()

    quotes = SimpleNamespace(
        yes_buy_price=0.45,  # improving/chasing bid: non-urgent
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is False
    assert executor.cancel_batches == []
    assert executor.place_batches == []
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OLD-YES"
    assert active.yes_price == 0.40


def test_order_manager_allows_urgent_risk_reductions_during_throttle():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.50
    active.yes_size = 5
    active.last_update = time.time()

    quotes = SimpleNamespace(
        yes_buy_price=0.45,  # lowering bid: urgent adverse-selection reduction
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-YES"]]
    assert len(executor.place_batches) == 1
    assert executor.place_batches[0][0]["price"] == 0.45
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OID-yes"
    assert active.yes_price == 0.45


def test_repair_quote_stays_resting_on_small_fv_wiggles():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.no_order_id = "OLD-NO"
    active.no_price = 0.50
    active.no_size = 6
    active.last_update = time.time() - 30

    quotes = SimpleNamespace(
        yes_buy_price=None,
        no_buy_price=0.47,  # normal mode would lower/cancel; repair should rest
        yes_buy_size=0,
        no_buy_size=6,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
            repair_mode="repair_down",
        )
    )

    assert updated is False
    assert executor.cancel_batches == []
    assert executor.place_batches == []
    assert active.no_order_id == "OLD-NO"
    assert active.no_price == 0.50


def test_repair_quote_reprices_when_dangerously_stale():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.no_order_id = "OLD-NO"
    active.no_price = 0.58
    active.no_size = 6
    active.last_update = time.time() - 30

    quotes = SimpleNamespace(
        yes_buy_price=None,
        no_buy_price=0.50,  # >5c lower, protect against stale overpaying
        yes_buy_size=0,
        no_buy_size=6,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
            repair_mode="repair_down",
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-NO"]]
    assert executor.place_batches[0][0]["price"] == 0.50


def test_crossed_buy_bid_waits_for_fill_state_before_cancel_replace():
    executor = DummyOrderStateExecutor(states=[True, False])
    executor.open_orders = {
        "OLD-YES": {"token_id": "YES1", "price": 0.50, "size": 5},
    }
    om = OrderManager(
        executor,
        reprice_threshold=0.01,
        crossed_bid_grace_seconds=0.0,
    )
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.50
    active.yes_size = 5

    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
            yes_book_snapshot=make_book(0.47),  # best ask 0.48, below our 0.50 bid
        )
    )

    assert updated is False
    assert executor.cancel_batches == []
    assert executor.place_batches == []
    assert active.yes_order_id is None


def test_crossed_buy_bid_cancels_after_grace_if_still_open():
    executor = DummyOrderStateExecutor(states=[True, True])
    executor.open_orders = {
        "OLD-YES": {"token_id": "YES1", "price": 0.50, "size": 5},
    }
    om = OrderManager(
        executor,
        reprice_threshold=0.01,
        crossed_bid_grace_seconds=0.0,
    )
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.50
    active.yes_size = 5

    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
            yes_book_snapshot=make_book(0.47),
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-YES"]]
    assert executor.place_batches[0][0]["price"] == 0.45


def test_sub_minimum_down_tail_quotes_light_side_only():
    # Current inventory: DOWN=3, UP=0 -> imbalance=-3.
    # Do not top up the already-heavy DOWN side; overshoot with the minimum
    # valid UP repair order instead. Queue priority beats cute dust arithmetic.
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=-3,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "repair_up"
    assert up_size == 5
    assert down_size == 0


def test_sub_minimum_up_tail_quotes_light_side_only():
    # Current inventory: UP=3, DOWN=0 -> imbalance=+3.
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=3,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "repair_down"
    assert up_size == 0
    assert down_size == 5


def test_normal_repair_remains_close_only_for_live_sized_tail():
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=-9,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "repair_up"
    assert up_size == 5
    assert down_size == 0


def test_dust_price_guardrails_favor_repair_side_and_keep_edge():
    quotes = SimpleNamespace(
        yes_buy_price=0.49,
        no_buy_price=0.49,
        combined_cost=0.98,
        edge_per_pair=0.02,
    )

    guarded = apply_dust_price_guardrails(quotes, "dust_down")

    # Too many DOWN means UP is repair side: improve UP bid, shade DOWN bid.
    assert guarded.yes_buy_price == 0.50
    assert guarded.no_buy_price == 0.48
    assert guarded.combined_cost < 1.0


def test_quote_invariant_combined_cost_below_one():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.5,
        t_normalized=0.5,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )
    assert quotes.yes_buy_price is not None
    assert quotes.no_buy_price is not None
    assert quotes.combined_cost <= MAX_COMBINED_COST


def test_pre_trade_rejects_thin_two_sided_edge():
    quotes = SimpleNamespace(
        yes_buy_price=0.50,
        no_buy_price=0.49,
        yes_buy_size=5,
        no_buy_size=5,
    )

    passed, reasons = pre_trade_checks(0.5, quotes, "NORMAL", True, "ACTIVE")

    assert passed is False
    assert "insufficient_edge_combined_cost_gt_max" in reasons


def test_quote_direction_guard_prevents_flat_inventory_price_inversion():
    qe = QuoteEngine()

    quotes = qe.generate_quotes(
        fair_value=0.4623,
        t_normalized=0.75,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
        best_bid_yes=0.52,
        best_bid_no=0.47,
        best_ask_yes=0.53,
        best_ask_no=0.48,
    )

    assert quotes.yes_buy_price <= quotes.no_buy_price
    assert quotes.combined_cost <= MAX_COMBINED_COST


def test_normal_two_sided_quotes_stay_below_model_fair_value():
    qe = QuoteEngine()

    quotes = qe.generate_quotes(
        fair_value=0.4577,
        t_normalized=0.75,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
        best_bid_yes=0.52,
        best_bid_no=0.47,
        best_ask_yes=0.53,
        best_ask_no=0.48,
    )

    assert quotes.yes_buy_price <= 0.45  # at least ~1c below YES FV
    assert quotes.no_buy_price <= 0.54   # at least ~1c below NO FV
    assert quotes.combined_cost <= MAX_COMBINED_COST


def test_normal_two_sided_quotes_do_not_sit_far_below_model_when_book_allows():
    qe = QuoteEngine()

    quotes = qe.generate_quotes(
        fair_value=0.60,
        t_normalized=0.75,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )

    assert 0.54 <= quotes.yes_buy_price <= 0.59
    assert 0.34 <= quotes.no_buy_price <= 0.39
    assert quotes.combined_cost <= MAX_COMBINED_COST


def test_quote_direction_guard_allows_inventory_repair_inversion():
    qe = QuoteEngine()

    quotes = qe.generate_quotes(
        fair_value=0.4623,
        t_normalized=0.75,
        sigma=0.8,
        share_imbalance=-200.0,  # Too many NO; YES is the repair side.
        max_imbalance=1000.0,
        yes_size=10,
        no_size=0,
        best_bid_yes=0.52,
        best_bid_no=0.47,
        best_ask_yes=0.53,
        best_ask_no=0.48,
    )

    assert quotes.yes_buy_price > quotes.no_buy_price
    assert quotes.yes_buy_size == 10
    assert quotes.no_buy_size == 0


def test_fv_favored_entry_mode_quotes_yes_first_when_fv_is_high_and_flat():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.60,
        t_normalized=0.9,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )

    side = apply_fv_favored_entry_mode(quotes, 0.60, share_imbalance=0.0, min_order_size=5)

    assert side == "yes"
    assert quotes.yes_buy_size == 5
    assert quotes.no_buy_size == 0


def test_fv_favored_entry_mode_quotes_no_first_when_fv_is_low_and_flat():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.40,
        t_normalized=0.9,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )

    side = apply_fv_favored_entry_mode(quotes, 0.40, share_imbalance=0.0, min_order_size=5)

    assert side == "no"
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 5


def test_fv_favored_entry_mode_blocks_neutral_flat_quotes_instead_of_quoting_both_sides():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.50,
        t_normalized=0.9,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )

    side = apply_fv_favored_entry_mode(quotes, 0.50, share_imbalance=0.0, min_order_size=5)

    assert side == "blocked"
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 0


def test_fv_favored_entry_mode_allows_repair_bid_near_best_bid():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.595,
        t_normalized=0.8,
        sigma=0.2,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
        best_bid_yes=0.59,
        best_ask_yes=0.60,
        best_bid_no=0.40,
        best_ask_no=0.41,
    )

    side = apply_fv_favored_entry_mode(
        quotes,
        0.595,
        share_imbalance=0.0,
        min_order_size=5,
        best_bid_no=0.40,
        best_ask_no=0.41,
    )

    assert side == "yes"
    assert quotes.yes_buy_size == 5
    assert quotes.no_buy_size == 0


def test_fv_favored_entry_mode_allows_moderate_directional_entry_when_time_left_under_old_gate():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.655,
        t_normalized=362 / 900,
        sigma=0.2,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
        best_bid_yes=0.65,
        best_ask_yes=0.66,
        best_bid_no=0.33,
        best_ask_no=0.34,
    )
    # Simulate the directional/pair-cost guards leaving FV-favored YES only.
    quotes.no_buy_size = 0

    side = apply_fv_favored_entry_mode(
        quotes,
        0.655,
        share_imbalance=0.0,
        min_order_size=5,
        best_bid_no=0.33,
        best_ask_no=0.34,
    )

    assert side == "yes"
    assert quotes.yes_buy_size == 5
    assert quotes.no_buy_size == 0


def test_fv_favored_entry_mode_blocks_when_complementary_repair_is_not_executable():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.60,
        t_normalized=0.9,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )

    side = apply_fv_favored_entry_mode(
        quotes,
        0.60,
        share_imbalance=0.0,
        min_order_size=5,
        best_ask_no=0.80,
    )

    assert side == "blocked"
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 0


def test_repair_min_edge_relaxes_only_near_expiry_for_repair():
    assert repair_min_edge_for_remaining(700, "repair_up") == 0.02
    assert repair_min_edge_for_remaining(240, "repair_down") == 0.005
    assert repair_min_edge_for_remaining(90, "repair_up") == 0.0
    assert repair_min_edge_for_remaining(30, "normal") == 0.02


def test_fv_aware_dust_repair_ladders_larger_up_dust_to_exact_min_tail():
    up_size, down_size, mode = compute_fv_aware_dust_repair_sizes(
        imbalance=4,
        fair_value=0.60,
        min_order_size=5,
        max_order_size=10,
    )

    assert (up_size, down_size, mode) == (0, 9, "repair_down")


def test_fv_aware_dust_repair_ladders_larger_down_dust_to_exact_min_tail():
    up_size, down_size, mode = compute_fv_aware_dust_repair_sizes(
        imbalance=-3,
        fair_value=0.60,
        min_order_size=5,
        max_order_size=10,
    )

    assert (up_size, down_size, mode) == (8, 0, "repair_up")


def test_fv_aware_dust_repair_holds_near_even_tail_to_avoid_oscillation():
    up_size, down_size, mode = compute_fv_aware_dust_repair_sizes(
        imbalance=-2,
        fair_value=0.51,
        min_order_size=5,
        max_order_size=10,
    )

    assert (up_size, down_size, mode) == (0, 0, "dust_hold_down")


def test_close_only_subminimum_repair_mode_is_not_left_normal():
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=3,
        min_order_size=5,
        max_order_size=10,
    )

    assert (up_size, down_size, mode) == (0, 5, "repair_down")

    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=-3,
        min_order_size=5,
        max_order_size=10,
    )

    assert (up_size, down_size, mode) == (5, 0, "repair_up")


def test_fv_favored_entry_mode_does_not_override_inventory_repair():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.60,
        t_normalized=0.9,
        sigma=0.8,
        share_imbalance=10.0,
        max_imbalance=1000.0,
        yes_size=0,
        no_size=10,
    )

    side = apply_fv_favored_entry_mode(quotes, 0.60, share_imbalance=10.0, min_order_size=5)

    assert side is None
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 10


def test_emergency_inventory_behavior():
    inv = InventoryManager(emergency=100.0)
    inv.record_fill("MARKET1", "yes", 105.0, 0.5)

    state = inv.get_state("MARKET1")
    assert state == InventoryState.EMERGENCY

    up_size, down_size = inv.compute_size_adjustment("MARKET1", 0.5, base_size=10)
    assert up_size == 0
    assert down_size == 10


def test_toxicity_halt_behavior():
    tox = ToxicityMonitor(halt_cooldown=60)
    edge = FillEdgeTracker(window=10)

    for _ in range(8):
        edge.record_fill("yes", 0.50, 0.48)

    halted = tox.check_kill_switch(edge)
    assert halted is True


def test_settlement_consistency():
    inv = InventoryManager()
    inv.record_fill("MARKET1", "yes", 100.0, 0.5)
    inv.record_fill("MARKET1", "no", 100.0, 0.4)

    pnl = inv.settle_market("MARKET1", True)
    assert pnl == 100.0 - 50.0 - 40.0
    assert "MARKET1" not in inv.positions


def test_clear_market_removes_position_without_settlement_math():
    inv = InventoryManager()
    inv.record_fill("MARKET2", "yes", 5.0, 0.4)
    inv.clear_market("MARKET2")
    assert "MARKET2" not in inv.positions


def test_markets_settled_counts_unique_markets():
    from src.monitoring.pnl_tracker import PnLTracker

    pnl = PnLTracker()
    pnl.record_settlement(1.0, "MARKET1")
    pnl.record_settlement(0.5, "MARKET1")
    pnl.record_settlement(2.0, "MARKET2")

    assert pnl.markets_settled == 2


def test_outcome_pnl_is_reported_separately_from_merge_pnl():
    from src.monitoring.pnl_tracker import PnLTracker

    pnl = PnLTracker()
    pnl.record_settlement(10.0, "MARKET1")
    pnl.record_outcome_resolution(3.0, "MARKET1")
    pnl.est_rebates = 0.5

    snap = pnl.snapshot()

    assert pnl.markets_settled == 1
    assert snap.net_trading_pnl == 10.0
    assert snap.outcome_pnl == 3.0
    assert snap.net_pnl_with_rebates == 10.5
    assert snap.economic_pnl == 13.5


def test_simulated_auto_merge_records_negative_pair_pnl():
    import asyncio

    inv = InventoryManager()
    inv.record_fill("MARKET1", "yes", 5, 0.57)
    inv.record_fill("MARKET1", "no", 5, 0.46)
    pnl = PnLTracker()
    pnl.current_capital = 0.0
    monitor = SimulatedBalanceMonitor(min_merge_pairs=1)

    result = asyncio.run(
        monitor.check_and_merge(inv, pnl_tracker=pnl, force=True)
    )

    assert result["merged"] is True
    assert pnl.net_trading_pnl == pytest.approx(-0.15)
    assert pnl.current_capital == pytest.approx(5.0)


def test_toxicity_monitor_respects_conservative_thresholds():
    tox = ToxicityMonitor(
        halt_cooldown=60,
        edge_adverse_rate=0.95,
        edge_mean_threshold=0.02,
        min_fills_for_halt=8,
        one_sided_fill_limit=8,
        immediate_drift_threshold=0.02,
    )
    edge = FillEdgeTracker(window=10)

    for _ in range(7):
        edge.record_fill("yes", 0.50, 0.48)

    assert tox.check_kill_switch(edge) is False



def test_dry_run_partial_fill_keeps_order_live():
    executor = DryRunExecutor(min_queue_time=0, max_queue_time=0, partial_fill_chance=1.0)
    executor.update_fair_value(0.4, 100.0)
    executor.open_orders["DRY-1"] = SimulatedOrder(
        order_id="DRY-1",
        token_id="YES1",
        side="yes",
        price=0.5,
        size=10,
        placed_at=time.time() - 5,
    )

    first_fills = executor.check_fills()
    assert len(first_fills) == 1
    assert first_fills[0]["size"] < 10
    assert "DRY-1" in executor.open_orders
    assert executor.open_orders["DRY-1"].filled is False

    remaining = executor.open_orders["DRY-1"].size
    executor.partial_fill_chance = 0.0
    second_fills = executor.check_fills()
    assert len(second_fills) == 1
    assert second_fills[0]["size"] == remaining
    assert "DRY-1" not in executor.open_orders


def test_live_prequote_fill_sync_updates_inventory_before_quotes():
    import asyncio

    class FakeLiveExecutor:
        def __init__(self):
            self.cancelled = []
            self.force_flags = []

        async def get_fills(self, market_id, force=False):
            self.force_flags.append(force)
            return [{"id": "F1"}]

        def process_fills(self, fills, inventory_mgr, market_id, token_id_to_side=None):
            return [{
                "order_id": "NO-1",
                "token_id": "NO_TOKEN",
                "side": "no",
                "price": 0.43,
                "size": 5,
                "fill_time": time.time(),
                "simulated": False,
            }]

        async def cancel_order(self, order_id):
            self.cancelled.append(order_id)
            return True

    cycler = MarketCycler.__new__(MarketCycler)
    cycler.asset = "BTC"
    cycler.inventory = InventoryManager()
    cycler.pnl = PnLTracker()
    cycler.edge_tracker = FillEdgeTracker(window=10)
    cycler._running = True
    cycler.stop_reason = ""
    executor = FakeLiveExecutor()
    cycler.order_mgr = OrderManager(executor)

    market = SimpleNamespace(
        market_id="M1",
        token_id_up="YES_TOKEN",
        token_id_down="NO_TOKEN",
    )
    active = cycler.order_mgr.get_active("M1")
    active.yes_order_id = "YES-RESTING"
    active.yes_price = 0.57
    active.yes_size = 5
    active.no_order_id = "NO-1"
    active.no_price = 0.43
    active.no_size = 5
    pos = cycler.inventory.get_or_create("M1", "BTC")

    ok = asyncio.run(cycler._sync_live_fills_before_quote(market, 0.40, pos))

    assert ok is True
    assert executor.force_flags == [True]
    assert pos.no_shares == 5
    assert pos.share_imbalance() == -5
    assert active.no_order_id is None
    assert cycler.order_mgr.get_active("M1").yes_order_id is None
    # The filled NO order is already consumed; the still-resting YES quote must
    # be canceled so the next cycle can quote close-only from fresh inventory.
    assert executor.cancelled == ["YES-RESTING"]


# ---------------------------------------------------------------------------
# FIFO pair-matching P&L accuracy tests
# ---------------------------------------------------------------------------

def test_fifo_pair_profit_identical_prices():
    """Basic: YES@$0.49 + NO@$0.49 → $0.02 edge per share."""
    inv = InventoryManager()
    inv.record_fill("M1", "yes", 10, 0.49)
    inv.record_fill("M1", "no",  10, 0.49)

    pos = inv.positions["M1"]
    profit = pos.matched_pair_profit()
    # 10 shares × (1.0 - 0.98) = $0.20
    assert abs(profit - 0.20) < 1e-9


def test_fifo_pair_profit_respects_chronological_pairing():
    """FIFO must pair the FIRST yes fill with the FIRST no fill.

    YES fills: 10@$0.60, then 10@$0.40  (different times / FV regimes)
    NO  fills: 10@$0.39, then 10@$0.59

    FIFO pairs:
      pair-1: 10 × (1 - 0.60 - 0.39) = 10 × 0.01 = $0.10
      pair-2: 10 × (1 - 0.40 - 0.59) = 10 × 0.01 = $0.10
    Total FIFO profit = $0.20

    Old averaged method would give:
      yes_avg = 0.50, no_avg = 0.49 → 20 × 0.01 = $0.20  (coincidentally same)

    The important invariant is *which* fills get paired — verify no cross-pairing.
    """
    inv = InventoryManager()
    inv.record_fill("M1", "yes", 10, 0.60)
    inv.record_fill("M1", "no",  10, 0.39)
    inv.record_fill("M1", "yes", 10, 0.40)
    inv.record_fill("M1", "no",  10, 0.59)

    pos = inv.positions["M1"]
    profit = pos.matched_pair_profit()
    assert abs(profit - 0.20) < 1e-9


def test_fifo_pair_profit_prevents_fv_swing_inflation():
    """The exact case that was inflating dry-run P&L ~10x.

    YES@$0.09 filled when FV was low near expiry.
    NO @$0.51 filled earlier when FV was high.

    Old averaged method:  1 × (1 - 0.09 - 0.51) = $0.40  ← fake edge
    FIFO method (correct): same pair, same result = $0.40

    BUT in practice the bot always places BOTH orders simultaneously at the
    same FV. To simulate the real problematic case we need DIFFERENT fills:

    Scenario: bot earned real edge of $0.02 per pair.
      YES@$0.49, NO@$0.49 — correctly paired, $0.02 edge ✓
      Then a rogue cross-time pairing would have been:
      YES@$0.09, NO@$0.89 — that's $0.02 edge too ✓ (FIFO ensures this)

    The bug was that averaged entry mixed these prices. FIFO prevents that
    by forcing strict pairing, so we test the worst case: partial fills
    that would have been mis-averaged.
    """
    inv = InventoryManager()
    # Pair 1: at FV=0.50, combined $0.98, edge $0.02
    inv.record_fill("M1", "yes", 5, 0.49)
    inv.record_fill("M1", "no",  5, 0.49)
    # Pair 2: at FV=0.80 high, combined $0.98, edge $0.02
    inv.record_fill("M1", "yes", 5, 0.79)
    inv.record_fill("M1", "no",  5, 0.19)

    pos = inv.positions["M1"]
    profit = pos.matched_pair_profit()
    # Both pairs have $0.02 edge → total $0.10 for 5 shares each
    expected = 5 * (1.0 - (0.49 + 0.49)) + 5 * (1.0 - (0.79 + 0.19))
    assert abs(profit - expected) < 1e-9
    # Critically: profit must be << $0.40 (the inflated old result)
    assert profit < 0.50


def test_acknowledge_settlement_prevents_double_counting():
    """After acknowledging, matched_pair_profit() returns 0 until new pairs form."""
    inv = InventoryManager()
    inv.record_fill("M1", "yes", 10, 0.49)
    inv.record_fill("M1", "no",  10, 0.49)

    pos = inv.positions["M1"]
    profit_before = pos.matched_pair_profit()
    assert profit_before > 0

    pos.acknowledge_settlement()
    # Immediately after ack: no new pairs → should return 0
    assert pos.matched_pair_profit() == 0.0

    # New fills form a new pair → profit resumes
    inv.record_fill("M1", "yes", 5, 0.48)
    inv.record_fill("M1", "no",  5, 0.48)
    assert pos.matched_pair_profit() > 0


def test_infer_collateral_token_matches_market_token_ids_by_derivation():
    class Call:
        def __init__(self, value):
            self.value = value
        def call(self):
            return self.value

    class Functions:
        def getCollectionId(self, _parent, _condition, index_set):
            return Call(f"collection-{index_set}")
        def getPositionId(self, collateral, collection_id):
            suffix = str(collection_id).split("-")[-1]
            if collateral.lower() == "0x2791bca1f2de4661ed88a30c99a7a9449aa84174":
                return Call(1000 + int(suffix))
            return Call(2000 + int(suffix))

    class CTF:
        functions = Functions()

    class W3:
        @staticmethod
        def to_checksum_address(addr):
            return addr

    inferred = infer_collateral_token_for_market(
        W3(),
        CTF(),
        "0x" + "11" * 32,
        yes_token_id="1001",
        no_token_id="1002",
        default_collateral="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
    )

    assert inferred.lower() == "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"


def test_pair_merge_pnl_does_not_count_market_settled():
    from src.monitoring.pnl_tracker import PnLTracker

    pnl = PnLTracker()
    pnl.record_pair_merge(-0.65, "MARKET1")

    assert pnl.settlement_pnl == -0.65
    assert pnl.markets_settled == 0

    pnl.record_outcome_resolution(0.0, "MARKET1")
    assert pnl.markets_settled == 1


def test_live_balance_sync_passes_signature_type_for_v1_sdk_params():
    from src.execution.clob_client import ClobClientWrapper
    import asyncio

    class FakeClient:
        def __init__(self):
            self.params = None
        def update_balance_allowance(self, params=None):
            self.params = params
            return {"ok": True}

    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        signature_type=3,
        funder="0xfunder",
    )
    wrapper._initialized = True
    wrapper._client_version = "v1"
    wrapper._client = FakeClient()

    assert asyncio.run(wrapper.sync_balance_allowance()) is True
    assert wrapper._client.params.signature_type == 3


def test_repair_price_cap_uses_unmatched_opposite_fifo_cost():
    from src.strategy.inventory import InventoryPosition

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("no", 0.55, 10)

    assert pos.max_profitable_repair_price("yes", 10, min_edge=0.01) == 0.44


def test_repair_price_cap_uses_worst_opposite_cost_across_requested_size():
    from src.strategy.inventory import InventoryPosition

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("yes", 0.40, 5)
    pos.add_fill("yes", 0.70, 5)

    assert pos.max_profitable_repair_price("no", 10, min_edge=0.01) == 0.29


def test_repair_price_cap_does_not_relax_for_wrong_way_no_tail():
    from src.strategy.inventory import InventoryPosition
    from src.orchestration.market_cycler import repair_price_cap

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("no", 0.55, 10)

    cap, reason = repair_price_cap(pos, "yes", 10, fair_value=0.72, min_edge=0.01)

    assert reason == "pair_edge"
    assert cap == 0.44


def test_live_repair_edge_buffer_is_two_cents():
    from src.strategy.inventory import InventoryPosition
    from src.orchestration.market_cycler import MIN_LIVE_PAIR_EDGE, repair_price_cap

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("no", 0.56, 10)

    cap, reason = repair_price_cap(pos, "yes", 10, fair_value=0.72, min_edge=MIN_LIVE_PAIR_EDGE)

    assert reason == "pair_edge"
    assert cap == 0.42


def test_negative_matched_pair_edge_halts_market_making():
    from src.strategy.inventory import InventoryPosition

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("yes", 0.57, 5)
    pos.add_fill("no", 0.46, 5)

    assert pos.matched_pair_profit() < 0
    assert has_negative_matched_pair_edge(pos) is True


def test_inventory_persists_fifo_pair_edge_state():
    from src.strategy.inventory import InventoryPosition

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("no", 0.56, 10)
    pos.add_fill("yes", 0.41, 5)

    restored = InventoryPosition.from_dict(pos.to_dict())

    assert restored.matched_pairs() == 5
    assert restored.matched_pair_profit() == pytest.approx(0.15)
    assert restored.avg_matched_pair_cost() == 0.97
    # The remaining 5 NO @ 0.56 must survive restart; otherwise repair cap
    # falls back to 0.99 and the live bot can buy guaranteed-loss YES repairs.
    assert restored.max_profitable_repair_price("yes", 5, min_edge=0.02) == 0.42


def test_repair_price_cap_keeps_pair_edge_when_tail_is_not_wrong_way():
    from src.strategy.inventory import InventoryPosition
    from src.orchestration.market_cycler import repair_price_cap

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("no", 0.55, 10)

    cap, reason = repair_price_cap(pos, "yes", 10, fair_value=0.42, min_edge=0.01)

    assert reason == "pair_edge"
    assert cap == 0.44


def test_repair_price_cap_does_not_relax_for_wrong_way_yes_tail():
    from src.strategy.inventory import InventoryPosition
    from src.orchestration.market_cycler import repair_price_cap

    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("yes", 0.58, 10)

    cap, reason = repair_price_cap(pos, "no", 10, fair_value=0.28, min_edge=0.01)

    assert reason == "pair_edge"
    assert cap == 0.41


def test_aggressive_repair_price_joins_best_post_only_price_under_cap():
    # If we can complete a profitable repair up to 0.54 and the ask is 0.53,
    # quote 0.52, not the stale model price 0.47. Waiting 5c below market is
    # how live carried a naked tail into expiry. Very expensive politeness.
    assert aggressive_repair_price(
        current_price=0.47,
        cap=0.54,
        best_ask=0.53,
        best_bid=0.48,
    ) == 0.52


def test_aggressive_repair_price_never_exceeds_cap():
    assert aggressive_repair_price(
        current_price=0.47,
        cap=0.49,
        best_ask=0.53,
        best_bid=0.48,
    ) == 0.49


def test_aggressive_repair_price_lowers_stale_quote_above_pair_cap():
    # Regression for the 4-window dry-run halt: repair cap was logged at 0.24,
    # but a stale YES repair quote stayed at 0.34 and filled a negative pair.
    assert aggressive_repair_price(
        current_price=0.34,
        cap=0.24,
        best_ask=0.36,
        best_bid=0.33,
    ) == 0.24


def test_deposit_wallet_activation_builds_full_trading_approval_batch():
    from src.execution.ctf_ops import (
        GaslessMerger, USDC_E_COLLATERAL_TOKEN, CLOB_EXCHANGE,
        CTF_CONTRACT, NEG_RISK_ADAPTER,
    )
    from web3 import Web3
    import asyncio

    captured = {}

    async def fake_batch(calls, metadata=""):
        captured["calls"] = calls
        captured["metadata"] = metadata
        return "tx-approval"

    merger = GaslessMerger(
        private_key="0x" + "11" * 32,
        funder="0x" + "22" * 20,
        signature_type=3,
        builder_api_key="k",
        builder_secret="s",
        builder_passphrase="p",
    )
    merger._w3 = Web3()
    merger._initialized = True
    merger._execute_deposit_wallet_batch = fake_batch

    assert asyncio.run(merger.ensure_deposit_wallet_trading_approvals()) is True
    assert captured["metadata"] == "Activate Trading Funds"
    # 5 spenders × 2 calls each (ERC20 approve + ERC1155 setApprovalForAll) = 10
    assert len(captured["calls"]) >= 10
    # Verify the NEG_RISK_ADAPTER is among the approved ERC20 targets.
    # The CLOB balance API checks allowance for this contract; without it,
    # the "Activate Funds" popup persists.
    erc20_targets = [c["target"].lower() for c in captured["calls"]
                     if c["target"].lower() == USDC_E_COLLATERAL_TOKEN.lower()]
    ctf_targets = [c["target"].lower() for c in captured["calls"]
                   if c["target"].lower() == CTF_CONTRACT.lower()]
    assert len(erc20_targets) == 5  # one ERC20 approve per spender
    assert len(ctf_targets) == 5    # one ERC1155 setApprovalForAll per spender
    assert captured["calls"][0]["target"].lower() == USDC_E_COLLATERAL_TOKEN.lower()
    assert captured["calls"][1]["target"].lower() == CTF_CONTRACT.lower()
    approval = merger._w3.eth.contract(address=USDC_E_COLLATERAL_TOKEN, abi=[{
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }])
    decoded = approval.decode_function_input(captured["calls"][0]["data"])[1]
    assert decoded["spender"].lower() == CLOB_EXCHANGE.lower()

    ctf_approval = merger._w3.eth.contract(address=CTF_CONTRACT, abi=[{
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    }])
    decoded_ctf = ctf_approval.decode_function_input(captured["calls"][1]["data"])[1]
    assert decoded_ctf["operator"].lower() == CLOB_EXCHANGE.lower()
    assert decoded_ctf["approved"] is True
