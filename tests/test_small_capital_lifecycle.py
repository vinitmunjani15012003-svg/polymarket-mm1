import asyncio
from types import SimpleNamespace

from src.orchestration.quote_cycle import QuoteCycleContext
from src.orchestration.small_capital import SmallCapitalLifecycle


class MemorySmallCapitalState:
    def __init__(self, initial=None):
        self.state = dict(initial or {})

    def get_small_capital_window(self, market_id):
        return self.state

    def update_small_capital_window(self, market_id, state):
        self.state = dict(state)


class CancelRecorder:
    def __init__(self):
        self.cancelled = []

    async def cancel_market_quotes(self, market_id):
        self.cancelled.append(market_id)
        return True

    def get_active(self, market_id):
        return SimpleNamespace(yes_order_id="", no_order_id="")


def owner_with_state(state):
    order_mgr = CancelRecorder()
    owner = SimpleNamespace(
        asset="BTC",
        ac=SimpleNamespace(symbol="BTCUSDT"),
        small_capital_config=SimpleNamespace(
            enabled=True,
            one_cycle_per_window=True,
            retry_unfilled_opening=True,
            stop_after_balanced_fill=True,
            cancel_remaining_orders_on_stop=True,
        ),
        inventory=SimpleNamespace(state_manager=MemorySmallCapitalState(state)),
        order_mgr=order_mgr,
        price_feed=SimpleNamespace(prices={"BTCUSDT": 50000}),
        last_fair_value=0.5,
        last_sigma=0.1,
        dashboard_updates=[],
    )
    owner._update_dashboard = lambda *args: owner.dashboard_updates.append(args)
    owner._set_dashboard_event = lambda *args: None
    owner.small_capital = SmallCapitalLifecycle(owner)
    return owner


def test_small_capital_lifecycle_forces_one_sided_opening_directly():
    owner = owner_with_state({"quote_cycle_started": False, "opening_attempt_spent": False})
    quotes = SimpleNamespace(yes_buy_size=5, no_buy_size=5, yes_buy_price=0.44, no_buy_price=0.46)

    side = owner.small_capital._apply_small_capital_opening_one_side("M1", quotes, "normal", 0.55)

    assert side == "yes"
    assert quotes.yes_buy_size == 5
    assert quotes.no_buy_size == 0


def test_small_capital_lifecycle_marks_done_from_state_and_cancels():
    owner = owner_with_state({
        "quote_cycle_started": True,
        "initial_filled": True,
        "balancing_filled": True,
    })
    market = SimpleNamespace(market_id="M1", slug="slug", time_remaining=10)
    pos = SimpleNamespace(matched_pairs=lambda: 0, share_imbalance=lambda: 1)

    stopped = asyncio.run(owner.small_capital._small_capital_maybe_stop_completed(market, pos, "state_completed"))

    assert stopped is True
    assert owner.inventory.state_manager.state["stopped_for_window"] is True
    assert owner.order_mgr.cancelled == ["M1"]
    assert owner.dashboard_updates[-1][4] == "SMALL_CAP_DONE"


def test_small_capital_lifecycle_repairs_stale_unfilled_opening_for_retry():
    owner = owner_with_state({})
    state = {"quote_cycle_started": True, "opening_attempt_spent": True, "initial_filled": False}

    repaired = owner.small_capital._repair_small_capital_unfilled_opening_state("M1", state, False, 0)

    assert repaired is True
    assert state["quote_cycle_started"] is False
    assert state["opening_attempt_spent"] is False
    assert state["stale_quote_cycle_repaired"] is True
    assert owner.inventory.state_manager.state["stale_quote_cycle_repaired"] is True


def test_quote_cycle_context_captures_stable_market_lifecycle_inputs():
    market = SimpleNamespace(market_id="M1", time_remaining=42.0)

    ctx = QuoteCycleContext.from_market(market, now=123.45)

    assert ctx.market is market
    assert ctx.now == 123.45
    assert ctx.remaining == 42.0
