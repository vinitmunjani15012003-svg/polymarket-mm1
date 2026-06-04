from types import SimpleNamespace

import pytest

from src.core.lifecycle import LifecycleManager
from src.core.models import DecisionResult, LifecycleState, OrderIntent
from src.services.fair_value import FairValueEngine, FairValueInputs, UpDownFairValue
from src.services.quoting import (
    QuotePolicy,
    apply_directional_market_guard,
    apply_pair_cost_precheck,
    normalize_quote_sizes,
    repair_size_or_zero,
)
from src.services.inventory import InventoryBook, inventory_diverged
from src.services.risk import feed_freshness_decision
from src.market_data import freshness


def test_decision_result_factories_are_serializable_shape():
    allowed = DecisionResult.allow("QUOTE", "OK", market="M1")
    blocked = DecisionResult.block("CANCEL", "STALE_SPOT", age=2.0)

    assert allowed.allowed is True
    assert allowed.metadata["market"] == "M1"
    assert blocked.allowed is False
    assert blocked.reason == "STALE_SPOT"


def test_order_intent_idempotency_key_is_stable_and_versioned():
    one = OrderIntent("M1", 7, "yes", price=0.52, size=5, token_id="UP")
    same = OrderIntent("M1", 7, "yes", price=0.52, size=5, token_id="UP")
    next_version = OrderIntent("M1", 8, "yes", price=0.52, size=5, token_id="UP")

    assert one.intent_id == same.intent_id
    assert one.intent_id != next_version.intent_id


def test_lifecycle_manager_rejects_invalid_transition():
    manager = LifecycleManager()
    manager.transition(LifecycleState.DISCOVERING)
    with pytest.raises(ValueError):
        manager.transition(LifecycleState.SETTLING)


def test_fair_value_engine_returns_explainable_result():
    model = UpDownFairValue(event_start_ts=0, resolve_ts=900, start_price=100)
    result = FairValueEngine(model).compute(
        FairValueInputs(
            spot=101,
            sigma=0.8,
            now_ts=300,
            elapsed_fraction=1 / 3,
            standardized_move=0.5,
            market_fv=0.55,
            price_source="exness_mt5",
        )
    )

    assert 0.01 <= result.raw_fv <= 0.99
    assert result.market_fv == 0.55
    assert 0.01 <= result.tradable_fv <= 0.99
    assert result.confidence >= 0


def test_quote_policy_constructs_orders_from_quote_like_object():
    quotes = SimpleNamespace(yes_buy_price=0.52, yes_buy_size=5, no_buy_price=0.43, no_buy_size=0)
    orders = QuotePolicy().construct_orders("M1", "UP", "DOWN", quotes)

    assert orders == [{"market_id": "M1", "token_id": "UP", "side": "yes", "price": 0.52, "size": 5}]


def test_quote_policy_helpers_apply_directional_and_pair_cost_guards():
    quotes = SimpleNamespace(yes_buy_price=0.78, yes_buy_size=10, no_buy_price=0.24, no_buy_size=10)

    action = apply_directional_market_guard(quotes, fair_value=0.70, repair_mode="normal")

    assert action == "halve_cheap_side"
    assert quotes.yes_buy_size == 10
    assert quotes.no_buy_size == 5

    blocked = apply_pair_cost_precheck(quotes, fair_value=0.70, repair_mode="normal", max_combined_cost=0.99)

    assert blocked is True
    assert quotes.yes_buy_size == 10
    assert quotes.no_buy_size == 0


def test_quote_size_policy_normalizes_and_rejects_sub_min_repairs():
    assert normalize_quote_sizes(2, 0, min_order_size=5, allow_round_up=True) == (5, 0)
    assert normalize_quote_sizes(2, 7, min_order_size=5, allow_round_up=False) == (0, 7)
    assert repair_size_or_zero(4, min_order_size=5) == 0
    assert repair_size_or_zero(5, min_order_size=5) == 5


def test_inventory_book_wraps_existing_inventory_manager():
    class InventoryManager:
        def get_or_create(self, market_id, asset):
            return SimpleNamespace(yes_shares=5, no_shares=2, yes_avg_price=0.5, no_avg_price=0.4)

    snapshot = InventoryBook(InventoryManager()).get_snapshot("M1", "BTC")

    assert snapshot.share_imbalance == 3
    assert snapshot.matched_pairs == 2
    assert snapshot.source == "local"


def test_data_risk_and_feed_health_share_freshness_semantics():
    fresh = freshness(age_seconds=0.5, max_age_seconds=1.0, source="exness_mt5")
    stale_decision = feed_freshness_decision(age_seconds=1.5, max_age_seconds=1.0, source="exness_mt5")

    assert fresh.healthy is True
    assert stale_decision.action == "CANCEL"
    assert inventory_diverged(0, 1.0) is True
