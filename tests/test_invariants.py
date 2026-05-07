from types import SimpleNamespace

import pytest

from src.config import AssetConfig, BotConfig
import time

from src.execution.dry_run import DryRunExecutor, SimulatedOrder
from src.execution.order_manager import OrderManager
from src.orchestration.market_cycler import (
    apply_dust_price_guardrails,
    compute_inventory_repair_sizes,
)
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


class DummyBatchExecutor(DummyExecutor):
    def __init__(self):
        super().__init__()
        self.cancel_batches = []
        self.place_batches = []

    async def place_buy_orders(self, orders):
        self.place_batches.append(orders)
        return {order["side"]: f"OID-{order['side']}" for order in orders}

    async def cancel_orders(self, order_ids):
        self.cancel_batches.append(order_ids)
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


def test_dust_normalization_quotes_down_tail_to_ten_each():
    # Current inventory: DOWN=3, UP=0 -> imbalance=-3.
    # Quote DOWN 7 and UP 10 so, if both fill, totals become 10/10.
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=-3,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "dust_down"
    assert up_size == 10
    assert down_size == 7


def test_dust_normalization_quotes_up_tail_to_ten_each():
    # Current inventory: UP=3, DOWN=0 -> imbalance=+3.
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=3,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "dust_up"
    assert up_size == 7
    assert down_size == 10


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
