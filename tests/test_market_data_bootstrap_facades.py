import time
from types import SimpleNamespace

import pytest

from src.bootstrap import build_container, reconcile_on_startup, run_startup_checks, validate_credentials
from src.core.lifecycle import LifecycleManager
from src.core.models import LifecycleState
from src.data.price_feed import PriceFeed
from src.market_data import FeedCache, WebsocketFeed, freshness, recovery_decision


def test_feed_freshness_normalizes_age_and_missing_values():
    fresh = freshness(0.25, 1.0, source="exness_mt5")
    missing = freshness(float("inf"), 1.0, source="missing")

    assert fresh.healthy is True
    assert fresh.age_ms == pytest.approx(250.0)
    assert fresh.age_seconds == pytest.approx(0.25)
    assert missing.healthy is False
    assert missing.as_dict()["source"] == "missing"


def test_feed_cache_reports_freshness_from_cached_timestamp():
    cache = FeedCache()
    tick = cache.set("btcusdt", 100.0, "aggTrade", ts=10.0)

    assert tick.price == 100.0
    assert cache.get("BTCUSDT") == tick
    assert cache.freshness("BTCUSDT", max_age_seconds=2.0, now=11.0).healthy is True
    assert cache.freshness("BTCUSDT", max_age_seconds=0.5, now=11.0).healthy is False
    assert cache.freshness("ETHUSDT", max_age_seconds=2.0, now=11.0).source == "missing"


def test_exness_configured_before_first_tick_fails_closed_even_with_binance_cache():
    feed = PriceFeed(
        ws_url="wss://example.test/ws",
        symbols=["BTCUSDT"],
        mt5_bridge_url="http://mt5.test",
        mt5_bridge_stale_seconds=1.0,
    )
    feed.prices["BTCUSDT"] = 101.0
    feed.timestamps["BTCUSDT"] = time.time()
    feed.price_sources["BTCUSDT"] = "aggTrade"

    assert feed.get_price("BTCUSDT") is None
    assert feed.get_price_source("BTCUSDT") == "exness_mt5_unavailable"
    assert feed.get_feed_freshness("BTCUSDT").source == "exness_mt5_unavailable"


def test_exness_stale_tick_remains_active_source_but_unhealthy():
    feed = PriceFeed(
        ws_url="wss://example.test/ws",
        symbols=["BTCUSDT"],
        mt5_bridge_url="http://mt5.test",
        mt5_bridge_stale_seconds=0.01,
    )
    feed._store_mt5_bridge_price("BTCUSDT", 100.5, ts=time.time(), received_ts=time.time() - 1.0)

    assert feed.get_price("BTCUSDT") == pytest.approx(100.5)
    assert feed.get_price_source("BTCUSDT") == "exness_mt5_stale"
    assert feed.get_feed_freshness("BTCUSDT").healthy is False


def test_websocket_feed_delegates_read_only_helpers():
    class FakeFeed:
        def __init__(self):
            self.callbacks = []

        def on_price_update(self, callback):
            self.callbacks.append(callback)

        def get_price(self, symbol):
            return 42.0

        def get_price_age(self, symbol):
            return 0.2

        def get_price_source(self, symbol):
            return "fake"

    facade = WebsocketFeed(FakeFeed())

    assert facade.get_price("BTCUSDT") == 42.0
    assert facade.freshness("BTCUSDT", 1.0).healthy is True
    assert facade.freshness("BTCUSDT", 1.0).source == "fake"


def test_lifecycle_transitions_through_recovery_path():
    manager = LifecycleManager()
    for state in (
        LifecycleState.DISCOVERING,
        LifecycleState.INITIALIZING,
        LifecycleState.QUOTING,
        LifecycleState.REPAIRING,
        LifecycleState.WINDDOWN,
        LifecycleState.SETTLING,
        LifecycleState.RESETTING,
        LifecycleState.DISCOVERING,
    ):
        assert manager.transition(state) == state


def test_recovery_decision_is_fail_closed_at_threshold():
    fresh = recovery_decision(age_seconds=0.9, reconnect_stale_seconds=1.0, current_backoff=2.0)
    stale = recovery_decision(age_seconds=1.0, reconnect_stale_seconds=1.0, current_backoff=2.0)

    assert fresh.reconnect is False
    assert fresh.backoff_seconds == 2.0
    assert stale.reconnect is True
    assert stale.backoff_seconds == 4.0


@pytest.mark.asyncio
async def test_bootstrap_recovery_summary_and_dependency_container():
    executor = SimpleNamespace(open_orders={"OID": "open"}, positions={"M1": 1})
    summary = await reconcile_on_startup(executor=executor, state_manager=object())
    container = build_container(price_feed="pf")

    assert summary["open_orders"] == {"OID": "open"}
    assert summary["positions"] == {"M1": 1}
    assert summary["state_loaded"] is True
    assert container.require("price_feed") == "pf"
    assert tuple(container.names()) == ("price_feed",)
    with pytest.raises(KeyError):
        container.require("missing")


def test_startup_checks_capture_success_and_failures():
    live_missing_key = SimpleNamespace(mode="live", credentials=SimpleNamespace(private_key=""))
    results = run_startup_checks(
        [
            ("ok", lambda: True),
            ("creds", lambda: validate_credentials(live_missing_key)),
        ]
    )

    assert results[0].ok is True
    assert results[1].ok is False
    assert "private_key" in results[1].error
