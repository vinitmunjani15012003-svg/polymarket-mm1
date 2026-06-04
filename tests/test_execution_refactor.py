import asyncio
from types import SimpleNamespace

from src.core.models.orders import OrderIntent
from src.services.execution.order_intents import attach_place_intent, next_quote_version
from src.execution.order_manager import OrderManager
from src.execution.order_state import ActiveQuotes
from src.execution.repricing import RepricePolicy
from src.services.execution.cancel_manager import CancelManager
from src.services.execution.order_submitter import OrderSubmitter
from src.services.execution.order_tracker import OrderTracker
from src.services.execution.reconciliation import find_stray_order_ids


class SideAwareExecutor:
    def __init__(self):
        self.calls = []
        self.batch_calls = []

    async def place_buy_order(self, token_id, price, size, side=None, book_snapshot=None):
        self.calls.append((token_id, price, size, side, book_snapshot))
        return f"{side}-order"

    async def place_buy_orders(self, orders):
        self.batch_calls.append(orders)
        return {order["side"]: f"{order['side']}-order" for order in orders}


class LegacyExecutor:
    def __init__(self):
        self.calls = []
        self.cancelled = []

    async def place_buy_order(self, token_id, price, size):
        self.calls.append((token_id, price, size))
        return "legacy-order"

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return order_id != "bad"


class BatchCancelExecutor:
    def __init__(self):
        self.cancelled = []

    async def cancel_orders(self, order_ids):
        self.cancelled.extend(order_ids)
        return True


class OpenOrderExecutor:
    def __init__(self, open_orders):
        self.open_orders = open_orders

    async def cancel_all(self):
        return True


class Book:
    best_ask = 0.50


def test_order_intent_ignores_metadata_for_retry_idempotency():
    first = OrderIntent("M1", 3, "yes", price=0.42, size=10, token_id="UP", metadata=(("trace", "a"),))
    retry = OrderIntent("M1", 3, "yes", price=0.42, size=10, token_id="UP", metadata=(("trace", "b"),))
    changed_action = OrderIntent("M1", 3, "yes", action="CANCEL", price=0.42, size=10, token_id="UP")

    assert first.intent_id == retry.intent_id
    assert first.intent_id != changed_action.intent_id
    assert next_quote_version(3) == 4


def test_attach_place_intent_adds_stable_metadata_without_mutating_spec():
    spec = {"token_id": "UP", "price": 0.42, "size": 10, "side": "yes"}
    enriched = attach_place_intent(spec, market_id="M1", quote_version=7)
    retry = attach_place_intent({**spec, "book_snapshot": object()}, market_id="M1", quote_version=7)

    assert "intent" not in spec
    assert enriched["quote_version"] == 7
    assert enriched["intent"].intent_id == retry["intent"].intent_id


def test_reprice_policy_marks_risk_reductions_urgent_and_sticky_repairs_stable():
    policy = RepricePolicy(reprice_threshold=0.005, repair_reprice_threshold=0.05)

    assert policy.decision(0.51, 0.49, 5, 5, book_snapshot=Book()) == (True, True)
    assert policy.decision(0.40, 0.43, 5, 5, sticky_repair=True) == (False, False)
    assert policy.decision(0.40, 0.46, 5, 5, sticky_repair=True) == (True, False)
    assert policy.decision(0.40, None, 5, 0) == (True, True)


def test_reconciliation_finds_stray_orders_on_market_tokens_only():
    active = ActiveQuotes(yes_order_id="tracked-yes", no_order_id=None)
    open_orders = {
        "tracked-yes": {"token_id": "UP"},
        "stray-up": {"asset_id": "UP"},
        "other-market": {"token_id": "OTHER"},
    }

    assert find_stray_order_ids(active, open_orders, {"UP", "DOWN"}) == ["stray-up"]


def test_order_submitter_preserves_executor_signature_boundaries():
    side_aware = SideAwareExecutor()
    book = object()
    placed = asyncio.run(OrderSubmitter(side_aware).submit_order(
        OrderIntent("M1", 1, "yes", price=0.44, size=7, token_id="UP"),
        book_snapshot=book,
    ))
    assert placed == "yes-order"
    assert side_aware.calls == [("UP", 0.44, 7, "yes", book)]

    legacy = LegacyExecutor()
    placed = asyncio.run(OrderSubmitter(legacy).place_buy("UP", 0.45, 4, side="no", book_snapshot=book))
    assert placed == "legacy-order"
    assert legacy.calls == [("UP", 0.45, 4)]


def test_order_manager_suppresses_duplicate_submit_for_same_side_quote_version():
    executor = SideAwareExecutor()
    manager = OrderManager(executor)
    spec = {
        "token_id": "UP",
        "price": 0.45,
        "size": 5,
        "side": "yes",
        "book_snapshot": object(),
    }
    first = attach_place_intent(spec, market_id="M1", quote_version=9)
    duplicate = attach_place_intent({**spec, "price": 0.46}, market_id="M1", quote_version=9)

    placed = asyncio.run(manager._place_buys([first, duplicate]))

    assert placed == {"yes": "yes-order"}
    assert len(executor.batch_calls) == 1
    assert len(executor.batch_calls[0]) == 1
    submitted = executor.batch_calls[0][0]
    assert submitted["price"] == 0.45
    assert "intent" not in submitted
    assert "quote_version" not in submitted


def test_order_tracker_reconstructs_states_from_active_quotes():
    active = ActiveQuotes(
        yes_order_id="yes-live",
        no_order_id="no-live",
        yes_price=0.41,
        no_price=0.53,
        yes_size=3,
        no_size=4,
    )
    tracker = OrderTracker()

    rebuilt = tracker.reconstruct_from_active_quotes(
        "M1",
        active,
        quote_version=12,
        token_id_yes="UP",
        token_id_no="DOWN",
    )

    assert set(rebuilt) == {"yes-live", "no-live"}
    assert tracker.orders["yes-live"].metadata == {"quote_version": 12, "token_id": "UP"}
    assert tracker.orders["no-live"].side == "no"
    assert tracker.orders["yes-live"].status == "open"


def test_cancel_manager_uses_batch_when_available_and_single_fallback():
    batch = BatchCancelExecutor()
    assert asyncio.run(CancelManager(batch).cancel_orders(["a", "b"])) is True
    assert batch.cancelled == ["a", "b"]

    legacy = LegacyExecutor()
    assert asyncio.run(CancelManager(legacy).cancel_orders(["a", "b"])) is True
    assert legacy.cancelled == ["a", "b"]


def test_crossed_bid_cancel_defers_when_fill_race_closes_order():
    active = ActiveQuotes(yes_order_id="yes-1", yes_price=0.51, yes_size=5)
    executor = OpenOrderExecutor(open_orders={"yes-1": SimpleNamespace(token_id="UP")})
    manager = OrderManager(executor, crossed_bid_grace_seconds=0.0)

    # Simulate a fill/reconciliation race: by the time the crossed-cancel path
    # checks exchange-local state, the order has already disappeared.
    executor.open_orders.clear()
    deferred = asyncio.run(manager._maybe_defer_crossed_bid_cancel(
        "market-1", "yes", "yes-1", active, sticky_repair=False
    ))

    assert deferred is True
    assert active.yes_order_id is None
    assert active.yes_price is None
    assert active.yes_size == 0
