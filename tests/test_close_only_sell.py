import asyncio
from types import SimpleNamespace

from src.execution.clob_client import ClobClientWrapper
from src.execution.order_manager import OrderManager
from src.monitoring.pnl_tracker import PnLTracker
from src.services.inventory.close_only_sell import plan_close_only_sell
from src.strategy.inventory import InventoryPosition


class Book:
    def __init__(self, bid=0.50, ask=0.55):
        self.best_bid = bid
        self.best_ask = ask
        self.tick_size = "0.01"
        self.neg_risk = False


class SellExecutor:
    def __init__(self):
        self.cancelled = []
        self.sell_orders = []
        self.open_orders = {}

    async def cancel_orders(self, order_ids):
        self.cancelled.extend(order_ids)
        return True

    async def place_sell_orders(self, orders):
        self.sell_orders.append(orders)
        return {order["side"]: f"sell-{order['side']}" for order in orders}


def _position_with_unmatched_yes() -> InventoryPosition:
    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("yes", 0.60, 10)
    pos.add_fill("no", 0.30, 5)
    return pos


def test_close_only_sell_planner_requires_wallet_truth_and_context():
    pos = _position_with_unmatched_yes()
    cfg = SimpleNamespace(enabled=True, max_order_size=10, min_edge=0.01, max_loss_per_share=0.20, min_seconds_remaining=30)

    no_wallet = plan_close_only_sell(
        pos,
        fair_value=0.65,
        wallet_snapshot=None,
        yes_book=Book(0.68, 0.72),
        no_book=Book(),
        min_order_size=5,
        max_order_size=10,
        config=cfg,
        close_only_context=True,
    )
    assert no_wallet.active is False
    assert no_wallet.reason == "NO_WALLET_TRUTH"

    no_context = plan_close_only_sell(
        pos,
        fair_value=0.65,
        wallet_snapshot={"yes_shares": 10, "no_shares": 5},
        yes_book=Book(0.68, 0.72),
        no_book=Book(),
        min_order_size=5,
        max_order_size=10,
        config=cfg,
        close_only_context=False,
    )
    assert no_context.active is False
    assert no_context.reason == "NOT_CLOSE_ONLY_CONTEXT"


def test_close_only_sell_planner_sells_only_fifo_unmatched_with_loss_floor():
    pos = _position_with_unmatched_yes()
    cfg = SimpleNamespace(enabled=True, max_order_size=10, min_edge=0.01, max_loss_per_share=0.20, min_seconds_remaining=30)

    plan = plan_close_only_sell(
        pos,
        fair_value=0.65,
        wallet_snapshot={"yes_shares": 10, "no_shares": 5},
        yes_book=Book(0.68, 0.72),
        no_book=Book(),
        min_order_size=5,
        max_order_size=10,
        config=cfg,
        close_only_context=True,
    )

    assert plan.active is True
    assert plan.side == "yes"
    assert plan.size == 5
    assert plan.price == 0.69  # one tick over bid, not the stale/wide ask
    assert plan.metadata["local_unmatched"] == 5
    assert plan.metadata["avg_entry"] == 0.6
    assert plan.price >= plan.metadata["max_loss_price"]


def test_inventory_unmatched_sell_drains_only_unmatched_fifo_lots():
    pos = InventoryPosition("M1", "BTC")
    pos.add_fill("yes", 0.40, 10)
    pos.add_fill("no", 0.50, 6)

    sale = pos.record_unmatched_sell("yes", price=0.55, size=3)

    assert sale["size"] == 3.0
    assert round(sale["proceeds"], 6) == 1.65
    assert round(sale["cost_basis"], 6) == 1.2
    assert round(sale["realized_pnl"], 6) == 0.45
    assert pos.yes_shares == 7
    assert pos.no_shares == 6
    assert pos.matched_pairs() == 6
    assert pos.unmatched_shares("yes") == 1


def test_pnl_unmatched_sale_recovers_capital_and_tracks_realized_unwind():
    pnl = PnLTracker()
    pnl.current_capital = 10

    pnl.record_unmatched_sale(size=5, price=0.60, side="yes", asset="BTC", market_id="M1", cost_basis=2.0, realized_pnl=1.0)

    assert pnl.current_capital == 13
    assert pnl.unmatched_unwind_pnl == 1.0
    assert pnl.total_volume == 3.0
    assert pnl.total_shares == 5
    assert pnl.total_fills == 1


def test_order_manager_close_only_sell_cancels_buys_and_tracks_sell_state():
    executor = SellExecutor()
    manager = OrderManager(executor)
    active = manager.get_active("M1")
    active.yes_order_id = "buy-yes"
    active.yes_price = 0.41
    active.yes_size = 5

    updated = asyncio.run(manager.update_close_only_sell(
        "M1",
        "UP",
        side="yes",
        price=0.55,
        size=5,
        book_snapshot=Book(0.50, 0.56),
    ))

    assert updated is True
    assert executor.cancelled == ["buy-yes"]
    assert executor.sell_orders[0][0]["execution_side"] == "SELL"
    assert executor.sell_orders[0][0]["close_only"] is True
    assert active.yes_order_id is None
    assert active.yes_sell_order_id == "sell-yes"
    assert active.yes_sell_price == 0.55
    assert active.yes_sell_size == 5


def test_clob_process_fills_preserves_sell_execution_side_and_close_only_context():
    client = ClobClientWrapper("host", "pk", 137, "key", "secret", "pass")
    client.open_orders["sell-1"] = {
        "token_id": "UP",
        "price": 0.55,
        "size": 5,
        "side": "SELL",
        "execution_side": "SELL",
        "close_only": True,
        "token_side": "yes",
    }

    processed = client.process_fills(
        [{"order_id": "sell-1", "asset_id": "UP", "price": "0.55", "size": "5"}],
        inventory_mgr=None,
        market_id="M1",
        token_id_to_side={"UP": "yes"},
    )

    assert processed == [{
        "order_id": "sell-1",
        "token_id": "UP",
        "side": "yes",
        "execution_side": "SELL",
        "close_only": True,
        "price": 0.55,
        "size": 5.0,
        "fill_time": processed[0]["fill_time"],
        "simulated": False,
    }]
    assert "sell-1" not in client.open_orders
