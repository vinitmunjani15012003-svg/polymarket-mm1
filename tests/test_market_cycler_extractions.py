from types import SimpleNamespace

from src.orchestration import dashboard_state, settlement
from src.orchestration.market_cycler import MarketCycler


class _Pnl:
    net_trading_pnl = 1
    outcome_pnl = 2
    est_rebates = 3
    net_pnl = 4
    economic_pnl = 5
    total_volume = 6
    total_shares = 7
    markets_settled = 8
    total_fills = 9
    starting_capital = 10
    current_capital = 11

    def rebates_per_hour(self):
        return 12


class _PriceFeed:
    prices = {"BTC": 100.0}
    ticks = 3

    def get_price_age(self, symbol):
        return 1.5

    def get_price_source(self, symbol):
        return "test"


def test_market_cycler_compatibility_wrappers_delegate_to_extracted_helpers():
    cycler = MarketCycler.__new__(MarketCycler)
    cycler._dashboard_event = {}

    cycler._set_dashboard_event("skip", "PRE_TRADE_FAILED", "risk check")
    assert cycler._dashboard_event["event_level"] == "skip"
    assert cycler._dashboard_event["event_reason"] == "PRE_TRADE_FAILED"
    assert cycler._dashboard_event["event_detail"] == "risk check"
    assert cycler._dashboard_event["event_ts"] > 0

    cycler._clear_dashboard_event()
    assert cycler._dashboard_event == {}

    assert MarketCycler._settle_market.__module__ == "src.orchestration.market_cycler"
    assert callable(settlement.settle_market)
    assert callable(settlement.wait_and_settle_unmatched_by_fields)
    assert callable(dashboard_state.update_dashboard)


def test_waiting_dashboard_helper_keeps_existing_payload_shape():
    states = []
    cycler = MarketCycler.__new__(MarketCycler)
    cycler.asset = "BTC"
    cycler.ac = SimpleNamespace(symbol="BTC")
    cycler.price_feed = _PriceFeed()
    cycler.pnl = _Pnl()
    cycler.balance_monitor = None
    cycler._dashboard_cb = states.append

    cycler._update_dashboard_waiting()

    assert len(states) == 1
    state = states[0]
    assert state["asset"] == "BTC"
    assert state["market_id"] == "waiting..."
    assert state["phase"] == "WAITING"
    assert state["spot_price"] == 100.0
    assert state["price_age"] == 1.5
    assert state["price_source"] == "test"
    assert state["markets_settled"] == 8
