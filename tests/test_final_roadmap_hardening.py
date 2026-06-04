import importlib

import pytest

from src.core.lifecycle import LifecycleManager
from src.core.models import LifecycleState, RiskDecision
from src.core.models.orders import OrderIntent, OrderState
from src.services.execution.order_tracker import OrderTracker
from src.services.risk import RiskCoordinator, basis_gap_decision, feed_freshness_decision


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
