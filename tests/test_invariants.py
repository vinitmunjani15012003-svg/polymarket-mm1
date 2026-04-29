from types import SimpleNamespace

import pytest

from src.config import AssetConfig, BotConfig
import time

from src.execution.dry_run import DryRunExecutor, SimulatedOrder
from src.execution.order_manager import OrderManager
from src.risk.toxicity import FillEdgeTracker, ToxicityMonitor
from src.strategy.inventory import InventoryManager, InventoryState
from src.strategy.quote_engine import QuoteEngine


class DummyExecutor:
    def __init__(self):
        self.calls = []

    async def place_buy_order(self, token_id, price, size, side="yes", book_snapshot=None):
        self.calls.append((token_id, price, size, side))
        return "OID-1"

    async def cancel_order(self, order_id):
        return True

    async def cancel_all(self):
        return True


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
    assert quotes.yes_buy_price + quotes.no_buy_price < 1.0


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
