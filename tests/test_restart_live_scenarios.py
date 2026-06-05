import asyncio
import time
from types import SimpleNamespace

import pytest

from src.core.models.orders import OrderIntent, OrderState
from src.orchestration.small_capital import SmallCapitalLifecycle
from src.services.execution.fill_processor import FillProcessor
from src.services.execution.order_tracker import OrderTracker
from src.services.fair_value.basis_protection import basis_check
from src.services.risk import RiskCoordinator, feed_freshness_decision


class MemorySmallCapitalState:
    """State-manager facade that survives owner/cycler reconstruction."""

    def __init__(self, initial=None):
        self.state = dict(initial or {})
        self.saved = []

    def get_small_capital_window(self, market_id):
        return self.state

    def update_small_capital_window(self, market_id, state):
        self.state = dict(state)
        self.saved.append((market_id, dict(state)))


class RecordingOrderManager:
    def __init__(self, active=None):
        self.active = active or SimpleNamespace(yes_order_id="", no_order_id="")
        self.cancelled_markets = []

    def get_active(self, market_id):
        return self.active

    async def cancel_market_quotes(self, market_id):
        self.cancelled_markets.append(market_id)
        self.active.yes_order_id = ""
        self.active.no_order_id = ""
        return True


class FillSink:
    def __init__(self):
        self.fills = []

    def process_fills(self, fills, *args, **kwargs):
        self.fills.extend(fills)
        return {"processed": len(fills)}


def small_cap_owner(state_manager, active=None):
    owner = SimpleNamespace(
        asset="BTC",
        ac=SimpleNamespace(symbol="BTCUSDT"),
        small_capital_config=SimpleNamespace(
            enabled=True,
            one_cycle_per_window=True,
            retry_unfilled_opening=True,
            stop_after_balanced_fill=True,
            cancel_remaining_orders_on_stop=True,
            emergency_hedge_enabled=True,
        ),
        inventory=SimpleNamespace(state_manager=state_manager),
        order_mgr=RecordingOrderManager(active),
        price_feed=SimpleNamespace(prices={"BTCUSDT": 50000.0}),
        last_fair_value=0.5,
        last_sigma=0.1,
        dashboard_updates=[],
    )
    owner._update_dashboard = lambda *args: owner.dashboard_updates.append(args)
    owner._set_dashboard_event = lambda *args: None
    owner.small_capital = SmallCapitalLifecycle(owner)
    return owner


def market(market_id="M1"):
    return SimpleNamespace(market_id=market_id, slug="btc-window", time_remaining=120)


def flat_pos():
    return SimpleNamespace(matched_pairs=lambda: 0, share_imbalance=lambda: 0)


def test_restart_mid_cycle_allows_same_side_opening_quote_reprice():
    state = MemorySmallCapitalState({
        "quote_cycle_started": True,
        "opening_attempt_spent": True,
        "initial_filled": False,
        "initial_order_id": "yes-live",
        "initial_side": "yes",
    })
    restarted = small_cap_owner(
        state,
        active=SimpleNamespace(yes_order_id="yes-live", no_order_id=""),
    )

    blocked = asyncio.run(restarted.small_capital._small_capital_fail_closed_before_quotes(
        market(), flat_pos(), wallet_snapshot=None, fv=0.53, sigma=0.2, remaining=100
    ))

    assert blocked is False
    assert restarted.order_mgr.cancelled_markets == []
    assert state.state["opening_attempt_spent"] is True
    assert restarted.dashboard_updates == []


def test_partial_fill_observed_after_cancel_still_forces_opposite_balancing_side():
    state = MemorySmallCapitalState({
        "quote_cycle_started": True,
        "opening_attempt_spent": True,
        "initial_filled": False,
        "initial_order_id": "yes-open",
        "initial_side": "yes",
    })
    owner = small_cap_owner(state, active=SimpleNamespace(yes_order_id="", no_order_id=""))
    processor = FillProcessor(FillSink())

    fill = processor.handle_partial_fill({"order_id": "yes-open", "side": "yes", "price": 0.44, "size": 2})
    owner.small_capital._small_capital_record_fills(market(), [fill])
    quotes = SimpleNamespace(yes_buy_size=5, no_buy_size=0)
    pos = SimpleNamespace(matched_pairs=lambda: 0, share_imbalance=lambda: 2)

    mode = owner.small_capital._apply_small_capital_balancing_override(
        "M1", pos, quotes, repair_mode="normal", min_order_size=5
    )

    assert state.state["initial_filled"] is True
    assert state.state["initial_price"] == pytest.approx(0.44)
    assert fill["partial"] is True
    assert mode == "repair_down"
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 5


def test_stale_exness_price_is_fail_closed_cancel_decision():
    stale = feed_freshness_decision(age_seconds=1.25, max_age_seconds=0.75, source="exness_mt5")
    decision = RiskCoordinator().evaluate(data=stale)

    assert decision.action == "CANCEL"
    assert decision.reason == "STALE_SPOT"
    assert decision.severity == "critical"
    assert decision.metadata["source"] == "exness_mt5"


def test_fv_book_divergence_surfaces_basis_gap_for_halt_or_cancel_layer():
    check = basis_check(fair_value=0.82, market_fv=0.54, threshold=0.25)

    assert check["triggered"] is True
    assert check["reason"] == "BASIS_GAP"
    assert check["basis_gap"] == pytest.approx(0.28)


def test_small_cap_completed_cycle_cancels_and_never_requotes_same_window():
    state = MemorySmallCapitalState({
        "quote_cycle_started": True,
        "opening_attempt_spent": True,
        "initial_filled": True,
        "balancing_filled": True,
    })
    owner = small_cap_owner(state, active=SimpleNamespace(yes_order_id="old-yes", no_order_id="old-no"))
    pos = SimpleNamespace(matched_pairs=lambda: 1, share_imbalance=lambda: 0)

    stopped = asyncio.run(owner.small_capital._small_capital_maybe_stop_completed(market(), pos, "balanced"))
    blocked_next_tick = asyncio.run(owner.small_capital._small_capital_fail_closed_before_quotes(
        market(), pos, wallet_snapshot=None, fv=0.50, sigma=0.2, remaining=90
    ))

    assert stopped is True
    assert blocked_next_tick is True
    assert state.state["stopped_for_window"] is True
    assert owner.order_mgr.cancelled_markets == ["M1", "M1"]
    assert owner.dashboard_updates[-1][4] == "SMALL_CAP_DONE"


def test_duplicate_order_intents_are_idempotent_across_retries_until_order_marked():
    tracker = OrderTracker()
    first = OrderIntent("M1", 11, "yes", price=0.51, size=5, token_id="UP", metadata=(("trace", "a"),))
    retry = OrderIntent("M1", 11, "yes", price=0.51, size=5, token_id="UP", metadata=(("trace", "b"),))

    first_id = tracker.add_intent(first)
    retry_id = tracker.add_intent(retry)

    assert first_id == retry_id
    assert len(tracker.pending) == 1

    tracker.mark_order(OrderState(order_id="live-1", intent_id=first_id, market_id="M1", side="yes", updated_ts=time.time()))

    assert tracker.pending == {}
    assert tracker.orders["live-1"].intent_id == first_id
