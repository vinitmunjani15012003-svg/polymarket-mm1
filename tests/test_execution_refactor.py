import asyncio
from types import SimpleNamespace

from src.core.models.orders import OrderIntent
from src.execution.order_manager import OrderManager
from src.execution.order_state import ActiveQuotes
from src.execution.repricing import RepricePolicy
from src.services.execution.cancel_manager import CancelManager
from src.services.execution.order_submitter import OrderSubmitter
from src.services.execution.reconciliation import find_stray_order_ids


class SideAwareExecutor:
    def __init__(self):
        self.calls = []

    async def place_buy_order(self, token_id, price, size, side=None, book_snapshot=None):
        self.calls.append((token_id, price, size, side, book_snapshot))
        return f"{side}-order"


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
