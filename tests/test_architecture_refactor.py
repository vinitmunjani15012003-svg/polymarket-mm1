from types import SimpleNamespace

import pytest

from src.core.lifecycle import LifecycleManager
from src.core.models import DecisionResult, LifecycleState, OrderIntent, RiskDecision
from src.services.fair_value import FairValueEngine, FairValueInputs, UpDownFairValue
from src.services.quoting import (
    QuotePolicy,
    apply_directional_market_guard,
    apply_pair_cost_precheck,
    normalize_quote_sizes,
    repair_size_or_zero,
)
from src.core.models.inventory import InventorySnapshot
from src.services.inventory import (
    InventoryBook,
    balanced_repair_debt_eligible,
    inventory_diverged,
    matched_pair_edge_status,
    plan_balanced_negative_edge_repair,
    plan_inventory_repair,
    plan_repair_price_cap,
    reconciliation_delta,
    saved_repair_cap_from_state,
    emergency_hedge_cap_from_state,
)
from src.services.risk import (
    RiskCoordinator,
    capital_available_decision,
    feed_freshness_decision,
    imbalance_decision,
    negative_pair_edge_decision,
)
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


def test_quote_policy_final_validation_ignores_inactive_stale_side_prices():
    quotes = SimpleNamespace(yes_buy_price=1.25, yes_buy_size=0, no_buy_price=0.40, no_buy_size=5)

    decision = QuotePolicy().validate_final(quotes)

    assert decision.allowed is True
    assert decision.reason == "OK"


def test_quote_policy_final_validation_blocks_active_pair_cost():
    quotes = SimpleNamespace(yes_buy_price=0.60, yes_buy_size=5, no_buy_price=0.40, no_buy_size=5)

    decision = QuotePolicy().validate_final(quotes, max_combined_cost=0.99)

    assert decision.allowed is False
    assert decision.reason == "PAIR_COST_TOO_HIGH"
    assert decision.metadata["combined_cost"] == 1.0


def test_quote_policy_normalizes_dust_repairs_atomically():
    quotes = SimpleNamespace(yes_buy_price=0.50, yes_buy_size=3, no_buy_price=0.45, no_buy_size=6)

    decision = QuotePolicy().normalize_sizes(quotes, min_order_size=5, allow_round_up=False, repair_mode="dust_up")

    assert decision.allowed is False
    assert decision.reason == "DUST_REPAIR_NOT_ATOMIC"
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 0


def test_quote_policy_normal_atomicity_allows_explicit_entry_modes_only():
    quotes = SimpleNamespace(yes_buy_price=0.50, yes_buy_size=5, no_buy_price=0.45, no_buy_size=0)
    policy = QuotePolicy()

    blocked = policy.enforce_normal_atomicity(
        quotes,
        repair_mode="normal",
        abs_imbalance=0,
        min_order_size=5,
    )

    assert blocked.allowed is False
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 0

    quotes = SimpleNamespace(yes_buy_price=0.50, yes_buy_size=5, no_buy_price=0.45, no_buy_size=0)
    allowed = policy.enforce_normal_atomicity(
        quotes,
        repair_mode="normal",
        abs_imbalance=0,
        min_order_size=5,
        fv_entry_side="yes",
    )

    assert allowed.allowed is True
    assert quotes.yes_buy_size == 5
    assert quotes.no_buy_size == 0


def test_quote_policy_balanced_repair_must_remain_atomic_after_scaling():
    policy = QuotePolicy()
    unequal = SimpleNamespace(yes_buy_price=0.15, yes_buy_size=5, no_buy_price=0.82, no_buy_size=3)

    blocked = policy.apply_post_capital_safety(
        unequal,
        min_order_size=5,
        allow_round_up=False,
        repair_mode="balanced_repair",
        abs_imbalance=0,
    )

    assert blocked.allowed is False
    assert blocked.reason == "BALANCED_REPAIR_NOT_ATOMIC"
    assert unequal.yes_buy_size == 0
    assert unequal.no_buy_size == 0

    equalizable = SimpleNamespace(yes_buy_price=0.15, yes_buy_size=8, no_buy_price=0.82, no_buy_size=6)
    allowed = policy.apply_post_capital_safety(
        equalizable,
        min_order_size=5,
        allow_round_up=False,
        repair_mode="balanced_repair",
        abs_imbalance=0,
    )

    assert allowed.allowed is True
    assert equalizable.yes_buy_size == 6
    assert equalizable.no_buy_size == 6


def test_quote_policy_post_capital_safety_enforces_repair_and_atomicity():
    policy = QuotePolicy()
    repair_quotes = SimpleNamespace(yes_buy_price=0.50, yes_buy_size=5, no_buy_price=0.45, no_buy_size=5)

    repair_decision = policy.apply_post_capital_safety(
        repair_quotes,
        min_order_size=5,
        allow_round_up=False,
        repair_mode="repair_up",
        abs_imbalance=10,
    )

    assert repair_decision.allowed is True
    assert repair_quotes.yes_buy_size == 5
    assert repair_quotes.no_buy_size == 0

    normal_quotes = SimpleNamespace(yes_buy_price=0.50, yes_buy_size=5, no_buy_price=0.45, no_buy_size=0)
    normal_decision = policy.apply_post_capital_safety(
        normal_quotes,
        min_order_size=5,
        allow_round_up=False,
        repair_mode="normal",
        abs_imbalance=0,
        atomic_reason="NORMAL_QUOTE_NOT_ATOMIC_FINAL",
    )

    assert normal_decision.allowed is False
    assert normal_decision.reason == "NORMAL_QUOTE_NOT_ATOMIC_FINAL"
    assert normal_quotes.yes_buy_size == 0
    assert normal_quotes.no_buy_size == 0


def test_quote_policy_final_inventory_safety_blocks_heavy_side_then_validates_active_sides():
    quotes = SimpleNamespace(yes_buy_price=1.25, yes_buy_size=5, no_buy_price=0.40, no_buy_size=5)

    decision = QuotePolicy().apply_final_inventory_safety(
        quotes,
        imbalance=10,
        min_order_size=5,
        repair_mode="normal",
        max_combined_cost=0.99,
    )

    assert decision.allowed is True
    assert decision.metadata["repair_mode"] == "repair_down"
    assert quotes.yes_buy_size == 0
    assert quotes.no_buy_size == 5


def test_quote_policy_pair_cost_guard_supports_quote_result_like_objects():
    class QuoteResultLike:
        yes_buy_price = 0.70
        yes_buy_size = 5
        no_buy_price = 0.20
        no_buy_size = 5

    quotes = QuoteResultLike()

    decision = QuotePolicy().apply_pair_cost_side_guard(
        quotes,
        side_label="yes",
        repair_mode="normal",
        cap=0.64,
        pair_edge=0.02,
        best_ask=0.72,
        best_bid=0.68,
        aggressive_price_fn=lambda price, cap, best_ask=None, best_bid=None: min(price, cap),
    )

    assert decision.allowed is True
    assert decision.reason == "PAIR_COST_CLAMPED"
    assert quotes.yes_buy_price == 0.64
    assert quotes.yes_buy_size == 5


def test_quote_policy_repair_pair_cost_guard_caps_or_blocks_side():
    quotes = SimpleNamespace(yes_buy_price=0.70, yes_buy_size=5, no_buy_price=0.20, no_buy_size=5)

    capped = QuotePolicy().apply_pair_cost_side_guard(
        quotes,
        side_label="yes",
        repair_mode="repair_up",
        cap=0.65,
        pair_edge=0.02,
        best_ask=0.72,
        best_bid=0.68,
        aggressive_price_fn=lambda price, cap, best_ask=None, best_bid=None: min(price, cap),
    )

    assert capped.reason == "REPAIR_QUOTE_CAPPED_FOR_PAIR_EDGE"
    assert quotes.yes_buy_price == 0.65

    blocked = QuotePolicy().apply_pair_cost_side_guard(
        quotes,
        side_label="yes",
        repair_mode="normal",
        cap=0.005,
        pair_edge=0.02,
        best_ask=None,
        best_bid=None,
        aggressive_price_fn=lambda price, cap, best_ask=None, best_bid=None: None,
    )

    assert blocked.allowed is False
    assert blocked.reason == "PAIR_COST_BLOCKED"
    assert quotes.yes_buy_size == 0


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


def test_risk_coordinator_aggregates_domain_decisions_with_audit_trail():
    decision = RiskCoordinator().evaluate(
        data=feed_freshness_decision(age_seconds=0.1, max_age_seconds=1.0, source="mt5"),
        market=RiskDecision("CANCEL", "BASIS_GAP", "critical", {"basis_gap": 0.2}),
        inventory=imbalance_decision(12, hard_limit=10),
        capital=capital_available_decision(3, required=5),
    )

    assert decision.action == "CANCEL"
    assert decision.reason == "BASIS_GAP"
    assert decision.metadata["basis_gap"] == 0.2
    assert decision.metadata["blocking_reasons"] == [
        "BASIS_GAP",
        "HARD_INVENTORY_LIMIT",
        "INSUFFICIENT_CAPITAL",
    ]
    assert [d["action"] for d in decision.metadata["decisions"]] == [
        "ALLOW",
        "CANCEL",
        "REPAIR",
        "REDUCE_SIZE",
    ]


def test_inventory_repair_planner_returns_explicit_subminimum_plan():
    plan = plan_inventory_repair(imbalance=-2.0, min_order_size=5, max_order_size=10)

    assert plan.yes_size == 5
    assert plan.no_size == 0
    assert plan.mode == "repair_up"
    assert plan.reason == "SUB_MINIMUM_TAIL"


def test_balanced_repair_plans_equal_profitable_pairs_for_negative_debt():
    class Position:
        def matched_pairs(self):
            return 5

        def matched_pair_profit(self):
            return -1.30

    cfg = SimpleNamespace(
        enabled=True,
        min_repair_debt=0.01,
        max_repair_debt=5.0,
        min_pair_edge=0.02,
        max_pair_cost=0.98,
        target_net_profit=0.0,
        max_order_size=10,
        max_abs_imbalance=0.5,
        min_seconds_remaining=90,
    )

    eligible, reason, meta = balanced_repair_debt_eligible(Position(), cfg)
    plan = plan_balanced_negative_edge_repair(
        Position(),
        yes_price=0.15,
        no_price=0.82,
        min_order_size=5,
        max_order_size=30,
        config=cfg,
        remaining_seconds=300,
        abs_imbalance=0,
    )

    assert eligible is True
    assert reason == "ELIGIBLE"
    assert meta["debt"] == 1.3
    assert plan.mode == "balanced_repair"
    assert plan.yes_size == 10
    assert plan.no_size == 10
    assert plan.metadata["pair_cost"] == 0.97
    assert plan.metadata["needed_pairs"] == 44


def test_balanced_repair_rejects_thin_or_imbalanced_pairs():
    class Position:
        def matched_pairs(self):
            return 5

        def matched_pair_profit(self):
            return -1.30

    cfg = SimpleNamespace(
        enabled=True,
        min_repair_debt=0.01,
        max_repair_debt=5.0,
        min_pair_edge=0.02,
        max_pair_cost=0.98,
        max_order_size=10,
        max_abs_imbalance=0.5,
        min_seconds_remaining=90,
    )

    thin = plan_balanced_negative_edge_repair(
        Position(),
        yes_price=0.15,
        no_price=0.84,
        min_order_size=5,
        max_order_size=30,
        config=cfg,
        remaining_seconds=300,
        abs_imbalance=0,
    )
    imbalanced = plan_balanced_negative_edge_repair(
        Position(),
        yes_price=0.15,
        no_price=0.82,
        min_order_size=5,
        max_order_size=30,
        config=cfg,
        remaining_seconds=300,
        abs_imbalance=3,
    )

    assert thin.mode == "normal"
    assert thin.reason == "PAIR_EDGE_TOO_THIN"
    assert imbalanced.mode == "normal"
    assert imbalanced.reason == "IMBALANCE_REPAIR_FIRST"


def test_repair_price_cap_planner_owns_fifo_and_saved_small_cap_caps():
    class Position:
        def max_profitable_repair_price(self, side, size, min_edge=0.01):
            return 0.99

    state = {"initial_side": "yes", "initial_yes_price": 0.51}

    fifo = plan_repair_price_cap(Position(), "no", 5, 0.4, min_edge=0.02, repair_mode="repair_down")
    saved = plan_repair_price_cap(
        Position(),
        "no",
        5,
        0.4,
        min_edge=0.02,
        repair_mode="repair_down",
        small_capital_opening_spent=True,
        small_capital_state=state,
        abs_imbalance=5,
    )

    assert fifo.cap == 0.99
    assert fifo.source == "fifo"
    assert saved.cap == 0.47
    assert saved.source == "small_capital_saved_entry"
    assert saved_repair_cap_from_state(state, "no", 0.02) == 0.47


def test_repair_price_cap_planner_blocks_saved_cap_when_entry_unknown():
    class Position:
        def max_profitable_repair_price(self, side, size, min_edge=0.01):
            return 0.99

    decision = plan_repair_price_cap(
        Position(),
        "no",
        5,
        0.4,
        min_edge=0.02,
        repair_mode="repair_down",
        small_capital_opening_spent=True,
        small_capital_state={"initial_side": "yes"},
        abs_imbalance=5,
    )

    assert decision.blocked is True
    assert decision.reason == "SMALL_CAPITAL_REPAIR_MISSING_ENTRY_PRICE"


def test_repair_price_cap_planner_owns_emergency_hedge_caps():
    class Position:
        def max_profitable_repair_price(self, side, size, min_edge=0.01):
            return 0.99

    cfg = SimpleNamespace(
        emergency_hedge_enabled=True,
        emergency_hedge_after_seconds=20.0,
        emergency_hedge_max_pair_loss=0.20,
    )
    state = {
        "initial_filled": True,
        "initial_fill_ts": 100.0,
        "initial_side": "yes",
        "initial_yes_price": 0.51,
    }

    waiting = emergency_hedge_cap_from_state(state, "no", config=cfg, now=110.0)
    active_cap = plan_repair_price_cap(
        Position(),
        "no",
        5,
        0.4,
        min_edge=0.02,
        repair_mode="repair_down",
        small_capital_opening_spent=True,
        small_capital_state=state,
        small_capital_config=cfg,
        abs_imbalance=5,
        now=130.0,
    )

    assert waiting == (None, False, 10.0)
    assert active_cap.cap == 0.69
    assert active_cap.source == "small_capital_emergency_hedge"
    assert active_cap.metadata["emergency_elapsed"] == 30.0


def test_inventory_book_repair_and_reconciliation_seams():
    class Position:
        yes_shares = 1
        no_shares = 4
        yes_avg_price = 0.40
        no_avg_price = 0.50

        def max_profitable_repair_price(self, side, size, min_edge=0.01):
            return 0.47 if side == "yes" else 0.43

    class InventoryManager:
        def get_or_create(self, market_id, asset):
            return Position()

    book = InventoryBook(InventoryManager())
    wallet = InventorySnapshot("M1", yes_shares=5, no_shares=4, source="wallet")

    assert book.plan_repair("M1", min_order_size=5, max_order_size=10).mode == "repair_up"
    assert book.repair_price_cap("M1", "yes", 5, fair_value=0.6) == (0.47, "pair_edge")
    assert book.reconciliation_needed("M1", wallet) is True
    delta = reconciliation_delta(book.get_snapshot("M1"), wallet)
    assert delta["diverged"] is True
    assert delta["imbalance_delta"] == 4


def test_negative_pair_edge_decision_halts_with_pair_metadata():
    class Position:
        def matched_pairs(self):
            return 3

        def matched_pair_profit(self):
            return -0.04

    decision = negative_pair_edge_decision(Position(), tolerance=0.005)

    assert decision.action == "HALT"
    assert decision.reason == "NEGATIVE_PAIR_EDGE"
    assert decision.metadata["matched_pairs"] == 3
    assert decision.metadata["source"] == "pair_tracker"
    status = matched_pair_edge_status(Position(), tolerance=0.005)
    assert status.triggered is True
    assert status.pair_pnl == -0.04


def test_data_risk_and_feed_health_share_freshness_semantics():
    fresh = freshness(age_seconds=0.5, max_age_seconds=1.0, source="exness_mt5")
    stale_decision = feed_freshness_decision(age_seconds=1.5, max_age_seconds=1.0, source="exness_mt5")

    assert fresh.healthy is True
    assert stale_decision.action == "CANCEL"
    assert inventory_diverged(0, 1.0) is True
