import asyncio
import importlib
from types import SimpleNamespace

import pytest

from src.core.lifecycle import LifecycleManager
from src.core.models import LifecycleState, RiskDecision
from src.core.models.orders import OrderIntent, OrderState
from src.execution.dry_run import DryRunExecutor
from src.execution.order_manager import OrderManager
from src.services.execution.order_intents import attach_place_intent, next_quote_version
from src.services.execution.order_tracker import OrderTracker
from src.services.quoting import QuotePolicy
from src.services.risk import RiskCoordinator, basis_gap_decision, feed_freshness_decision
from src.strategy.quote_engine import QuoteEngine


def test_risk_coordinator_stop_and_halt_outrank_cancel_with_full_audit_trail():
    decision = RiskCoordinator().evaluate(
        data=feed_freshness_decision(age_seconds=3.0, max_age_seconds=1.0, source="exness_mt5"),
        market=basis_gap_decision(0.30, threshold=0.25),
        stops=RiskDecision("HALT", "DAILY_LOSS_LIMIT", "critical", {"current_pnl": -42.0}),
    )

    assert decision.action == "HALT"
    assert decision.reason == "DAILY_LOSS_LIMIT"
    assert decision.metadata["current_pnl"] == -42.0
    assert decision.metadata["blocking_reasons"] == ["STALE_SPOT", "BASIS_GAP", "DAILY_LOSS_LIMIT"]
    assert [item["reason"] for item in decision.metadata["decisions"]] == [
        "STALE_SPOT",
        "BASIS_GAP",
        "DAILY_LOSS_LIMIT",
    ]


def test_order_tracker_idempotency_survives_metadata_only_retry_then_clears_on_mark():
    tracker = OrderTracker()
    first = OrderIntent("M1", 4, "no", price=0.47, size=5, token_id="DOWN", metadata=(("attempt", 1),))
    retry = OrderIntent("M1", 4, "no", price=0.47, size=5, token_id="DOWN", metadata=(("attempt", 2),))

    assert tracker.add_intent(first) == tracker.add_intent(retry)
    assert list(tracker.pending) == [first.intent_id]

    tracker.mark_order(OrderState(order_id="live-no", intent_id=first.intent_id, market_id="M1", side="no"))

    assert tracker.pending == {}
    assert tracker.orders["live-no"].intent_id == first.intent_id


def test_clob_and_settlement_split_modules_preserve_legacy_import_boundaries():
    clob_client = importlib.import_module("src.execution.clob_client")
    clob_pkg = importlib.import_module("src.execution.clob")
    settlement_pkg = importlib.import_module("src.execution.settlement")
    ctf_ops = importlib.import_module("src.execution.ctf_ops")

    assert clob_client.ClobClientWrapper is not None
    assert clob_pkg.ClobOrders is importlib.import_module("src.execution.clob.orders").ClobOrders
    assert settlement_pkg.SettlementManager is importlib.import_module(
        "src.execution.settlement.settlement_manager"
    ).SettlementManager
    assert ctf_ops.BalanceMonitor is settlement_pkg.BalanceMonitor


def test_lifecycle_state_machine_allows_expected_happy_path_and_blocks_skip_to_settlement():
    manager = LifecycleManager()

    for state in [
        LifecycleState.DISCOVERING,
        LifecycleState.INITIALIZING,
        LifecycleState.QUOTING,
        LifecycleState.REPAIRING,
        LifecycleState.QUOTING,
        LifecycleState.WINDDOWN,
        LifecycleState.SETTLING,
        LifecycleState.RESETTING,
        LifecycleState.DISCOVERING,
    ]:
        assert manager.transition(state) == state

    manager = LifecycleManager(LifecycleState.QUOTING)
    with pytest.raises(ValueError, match="QUOTING -> RESETTING"):
        manager.transition(LifecycleState.RESETTING)


def test_lifecycle_halt_is_recoverable_only_through_resetting():
    manager = LifecycleManager(LifecycleState.REPAIRING)

    assert manager.transition(LifecycleState.HALTED) == LifecycleState.HALTED
    with pytest.raises(ValueError, match="HALTED -> QUOTING"):
        manager.transition(LifecycleState.QUOTING)
    assert manager.transition(LifecycleState.RESETTING) == LifecycleState.RESETTING


class InstantDryRunExecutor(DryRunExecutor):
    async def _simulate_network_latency(self):
        return None


def test_service_wired_dry_run_quote_probe_places_intent_tracked_orders_without_network():
    """Deterministic smoke probe for the service-wired quote decision path."""
    quotes = QuoteEngine(max_order_size=5, edge_ticks=1).generate_quotes(
        fair_value=0.56,
        t_normalized=0.75,
        sigma=0.2,
        share_imbalance=0,
        max_imbalance=20,
        yes_size=5,
        no_size=5,
        best_ask_yes=0.62,
        best_ask_no=0.50,
        best_bid_yes=0.54,
        best_bid_no=0.42,
    )
    risk = RiskCoordinator().evaluate(
        data=feed_freshness_decision(age_seconds=0.05, max_age_seconds=1.0, source="unit_feed"),
        market=basis_gap_decision(0.02, threshold=0.25),
    )
    policy = QuotePolicy()
    decision = policy.validate(quotes)

    assert risk.action == "ALLOW"
    assert decision.allowed is True
    assert quotes.yes_buy_price is not None
    assert quotes.no_buy_price is not None
    assert quotes.combined_cost <= 0.98

    dry_run = InstantDryRunExecutor(min_queue_time=0, max_queue_time=0, partial_fill_chance=0)
    manager = OrderManager(dry_run)
    yes_book = SimpleNamespace(best_ask=0.62, best_bid=0.54)
    no_book = SimpleNamespace(best_ask=0.50, best_bid=0.42)

    updated = asyncio.run(
        manager.update_quotes(
            "MARKET-SMOKE",
            "UP-TOKEN",
            "DOWN-TOKEN",
            quotes,
            yes_book_snapshot=yes_book,
            no_book_snapshot=no_book,
        )
    )

    assert updated is True
    assert sorted(order.side for order in dry_run.open_orders.values()) == ["no", "yes"]
    active = manager.get_active("MARKET-SMOKE")
    assert active.yes_order_id in dry_run.open_orders
    assert active.no_order_id in dry_run.open_orders
    assert manager._quote_versions["MARKET-SMOKE"] == 1
    assert len(manager.order_tracker.orders) == 2
    tracked = sorted(manager.order_tracker.orders.values(), key=lambda item: item.side)
    assert {order.metadata["quote_version"] for order in tracked} == {1}
    assert {order.metadata["token_id"] for order in tracked} == {"UP-TOKEN", "DOWN-TOKEN"}


def test_coordinator_quote_policy_order_intents_gate_blocked_and_allowed_paths_together():
    policy = QuotePolicy()
    quote_version = next_quote_version(None)
    allowed_quotes = SimpleNamespace(yes_buy_price=0.51, yes_buy_size=5, no_buy_price=0.46, no_buy_size=5)

    risk = RiskCoordinator().evaluate(
        data=feed_freshness_decision(age_seconds=0.2, max_age_seconds=1.0, source="unit_feed"),
        market=basis_gap_decision(0.03, threshold=0.25),
    )
    decision = policy.validate(allowed_quotes)
    orders = policy.construct_orders("MARKET-INTEGRATION", "UP", "DOWN", allowed_quotes)
    intents = [attach_place_intent(order, market_id="MARKET-INTEGRATION", quote_version=quote_version) for order in orders]

    assert risk.action == "ALLOW"
    assert decision.allowed is True
    assert [item["intent"].quote_version for item in intents] == [1, 1]
    assert [item["intent"].side for item in intents] == ["yes", "no"]
    assert len({item["intent"].intent_id for item in intents}) == 2

    blocked_quotes = SimpleNamespace(yes_buy_price=0.77, yes_buy_size=5, no_buy_price=0.25, no_buy_size=5)
    blocked_risk = RiskCoordinator().evaluate(
        data=feed_freshness_decision(age_seconds=3.0, max_age_seconds=1.0, source="unit_feed"),
    )
    blocked_decision = policy.validate(blocked_quotes)

    should_build_intents = blocked_risk.action == "ALLOW" and blocked_decision.allowed

    assert blocked_risk.action == "CANCEL"
    assert blocked_decision.allowed is False
    assert blocked_decision.reason == "PAIR_COST_TOO_HIGH"
    assert should_build_intents is False
