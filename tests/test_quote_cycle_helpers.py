from types import SimpleNamespace

from src.orchestration.quote_cycle import (
    QuoteCycleContext,
    decide_basis_risk,
    decide_stale_spot,
    package_book_snapshot,
    package_fair_value_result,
)


def test_quote_cycle_context_captures_market_and_remaining():
    market = SimpleNamespace(market_id="M1", time_remaining=37.5)

    ctx = QuoteCycleContext.from_market(market, now=101.25)

    assert ctx.market is market
    assert ctx.now == 101.25
    assert ctx.remaining == 37.5


def test_stale_spot_decision_packages_missing_and_stale_paths():
    missing = decide_stale_spot(None, price_age=float("inf"), max_spot_age=2.0)
    stale = decide_stale_spot(50000.0, price_age=2.5, max_spot_age=2.0)
    fresh = decide_stale_spot(50000.0, price_age=1.5, max_spot_age=2.0)

    assert missing.should_stop is True
    assert missing.dashboard_reason == "NO_SPOT"
    assert missing.event_reason == "NO_SPOT_PRICE"
    assert stale.should_stop is True
    assert stale.dashboard_reason == "STALE_SPOT"
    assert stale.event_message == "age 2.50s > max 2.00s"
    assert fresh.is_ok is True


def test_book_snapshot_packages_books_and_polymarket_mid():
    market = SimpleNamespace(token_id_up="UP", token_id_down="DOWN")
    up_book = SimpleNamespace(best_bid=0.40, best_ask=0.44)
    down_book = SimpleNamespace(best_bid=0.54, best_ask=0.58)

    snapshot = package_book_snapshot({"UP": up_book, "DOWN": down_book}, market)

    assert snapshot.book_up is up_book
    assert snapshot.book_down is down_book
    assert snapshot.best_bid_yes == 0.40
    assert snapshot.best_ask_yes == 0.44
    assert snapshot.best_bid_no == 0.54
    assert snapshot.best_ask_no == 0.58
    assert round(snapshot.polymarket_mid_up, 8) == 0.43  # avg(UP mid .42, 1 - DOWN mid .44)


def test_fair_value_package_preserves_engine_outputs_and_basis_delta():
    fv_result = SimpleNamespace(raw_fv=0.62, confidence=0.7, blended_fv=0.58, tradable_fv=0.56)

    package = package_fair_value_result(fv_result, polymarket_mid_up=0.50)

    assert package.model_fv == 0.62
    assert package.model_confidence == 0.7
    assert package.uncapped_fv == 0.58
    assert package.tradable_fv == 0.56
    assert round(package.basis_delta, 8) == 0.12


def test_basis_risk_decision_selects_close_only_or_stop_quoting():
    close_only = decide_basis_risk(
        repair_mode="normal",
        balance_only=False,
        is_halted=False,
        model_fv=0.80,
        polymarket_mid_up=0.50,
        abs_imbalance=6,
        min_order_size=5,
    )
    stop = decide_basis_risk(
        repair_mode="normal",
        balance_only=False,
        is_halted=False,
        model_fv=0.80,
        polymarket_mid_up=0.50,
        abs_imbalance=4,
        min_order_size=5,
    )
    skipped = decide_basis_risk(
        repair_mode="repair_up",
        balance_only=False,
        is_halted=False,
        model_fv=0.80,
        polymarket_mid_up=0.50,
        abs_imbalance=10,
        min_order_size=5,
    )

    assert close_only.triggered is True
    assert close_only.action == "close_only"
    assert stop.triggered is True
    assert stop.action == "stop_quoting"
    assert skipped.triggered is False
