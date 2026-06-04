from types import SimpleNamespace

from src.execution.clob import ClobBalances, ClobOrders
from src.execution.clob.balances import zero_allowance_spenders
from src.execution.clob.fill_ids import fill_dedupe_key
from src.execution.clob.fills import maker_order_id, maker_orders_for_fill
from src.execution.clob.sdk_compat import ensure_builder_code, normalize_post_orders_response
from src.execution.clob_client import ClobClientWrapper


def test_fill_dedupe_key_prefers_provider_id_and_synthesizes_stable_key():
    assert fill_dedupe_key({"trade_id": "t1"}, "m1") == "trade_id:t1"

    fill = {
        "orderID": "o1",
        "assetId": "asset1",
        "price": "0.42",
        "size": "5",
        "side": "BUY",
        "timestamp": "123",
    }
    same_fields_different_order = {
        "timestamp": "123",
        "side": "BUY",
        "size": "5",
        "price": "0.42",
        "assetId": "asset1",
        "orderID": "o1",
    }
    other_market = dict(fill)

    assert fill_dedupe_key(fill, "m1") == fill_dedupe_key(same_fields_different_order, "m1")
    assert fill_dedupe_key(fill, "m1") != fill_dedupe_key(other_market, "m2")


def test_maker_order_helpers_cover_sdk_field_variants():
    assert maker_order_id({"orderId": "abc"}) == "abc"
    assert maker_orders_for_fill({"makerOrders": [{"id": "1"}]}) == [{"id": "1"}]
    assert maker_orders_for_fill({"makerOrders": "bad"}) == []


def test_normalize_post_orders_response_covers_common_sdk_shapes():
    assert normalize_post_orders_response([{"id": "a"}, "bad"], 2) == [{"id": "a"}, {}]
    assert normalize_post_orders_response({"orders": [{"orderID": "a"}]}, 2) == [{"orderID": "a"}]
    assert normalize_post_orders_response({"id": "single"}, 1) == [{"id": "single"}]
    assert normalize_post_orders_response({"status": "rejected"}, 2) == [
        {"status": "rejected"},
        {"status": "rejected"},
    ]
    assert normalize_post_orders_response(None, 2) == [{}, {}]


def test_ensure_builder_code_mutates_when_missing_and_facade_delegates_static_helpers():
    order_args = SimpleNamespace(token_id="t")

    assert ensure_builder_code(order_args) is order_args
    assert order_args.builder_code == ""
    assert ClobClientWrapper._ensure_builder_code(order_args) is order_args
    assert ClobClientWrapper._normalize_post_orders_response({"data": [{"id": "x"}]}, 1) == [{"id": "x"}]
    assert ClobClientWrapper._fill_dedupe_key({"id": "fill"}, "m") == "id:fill"


def test_balance_allowance_helper_and_clob_facade_imports_remain_available():
    assert zero_allowance_spenders({"spender-a": "0", "spender-b": 5}) == ["spender-a"]
    assert ClobBalances is not None
    assert ClobOrders is not None
