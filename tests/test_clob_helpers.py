import asyncio
from types import SimpleNamespace

from src.execution.clob import ClobBalances, ClobOrders
from src.execution.clob.balances import parse_balance_allowance, zero_allowance_spenders
from src.execution.clob.fill_ids import fill_dedupe_key
from src.execution.clob.fills import maker_order_id, maker_orders_for_fill
from src.execution.clob.order_context import (
    cache_open_order_context,
    get_order_context,
    normalize_open_order_record,
    normalize_open_orders,
    normalize_orders_response,
    order_is_closed,
    token_side_from_outcome,
)
from src.execution.clob.sdk_compat import ensure_builder_code, normalize_post_orders_response, post_order_compat
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


def test_post_order_compat_retries_without_post_only_for_sdk_variants():
    class Client:
        calls = []

        def post_order(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if kwargs.get("post_only"):
                raise TypeError("unexpected post_only")
            return {"id": "ok"}

    client = Client()

    assert post_order_compat(client, "signed", "GTC") == {"id": "ok"}
    assert client.calls == [
        (("signed", "GTC"), {"post_only": True}),
        (("signed", "GTC"), {}),
    ]


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


def test_v2_deposit_wallet_order_signing_keeps_poly_1271_mode():
    assert ClobClientWrapper._order_signature_type_for_client("v2", 3, "0xfunder") == 3
    assert ClobClientWrapper._order_signature_type_for_client("v2", 1, "0xfunder") == 1
    assert ClobClientWrapper._order_signature_type_for_client("v2", 3, "") == 3
    assert ClobClientWrapper._order_signature_type_for_client("v1", 3, "0xfunder") == 3


async def _temporary_signature_type_roundtrip():
    class Builder:
        signature_type = 1

    class Client:
        builder = Builder()

    wrapper = ClobClientWrapper(
        host="https://clob.polymarket.com",
        private_key="0xabc",
        chain_id=137,
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        signature_type=3,
        funder="0xfunder",
    )
    wrapper._client = Client()
    observed = await wrapper._run_client_call(
        lambda: wrapper._client.builder.signature_type,
        signature_type=3,
    )
    return observed, wrapper._client.builder.signature_type


def test_client_call_temporarily_switches_and_restores_signature_type():
    observed, restored = asyncio.run(_temporary_signature_type_roundtrip())

    assert observed == 3
    assert restored == 1


def test_balance_allowance_helper_and_clob_facade_imports_remain_available():
    assert zero_allowance_spenders({"spender-a": "0", "spender-b": 5}) == ["spender-a"]
    parsed = parse_balance_allowance({"balance": "12", "allowances": {"spender-a": "0", "spender-b": "9"}})
    assert parsed == {
        "balance": "12",
        "allowances": {"spender-a": "0", "spender-b": "9"},
        "zero_allowances": ["spender-a"],
        "verified": False,
    }
    assert ClobBalances is not None
    assert ClobOrders is not None


def test_order_context_helpers_normalize_open_orders_and_cache_recent_context():
    record = {
        "orderID": "oid-1",
        "asset_id": "token-1",
        "price": "0.51",
        "original_size": "10",
        "size_matched": "4",
        "outcome": "Up",
        "created_at": "123",
    }

    assert normalize_orders_response({"data": [record, "bad"]}) == [record]
    assert token_side_from_outcome("Down") == "no"
    assert order_is_closed({"status": "filled"}) is True
    assert order_is_closed({"original_size": "5", "size_matched": "5"}) is True
    assert normalize_open_order_record(record) == (
        "oid-1",
        {
            "token_id": "token-1",
            "price": 0.51,
            "size": 6.0,
            "side": "BUY",
            "token_side": "yes",
            "placed_at": 123.0,
        },
    )
    assert normalize_open_orders([record])["oid-1"]["size"] == 6.0

    open_orders = {"oid-1": {"token_id": "token-1", "size": 6}}
    recent = {}
    cache_open_order_context(open_orders, recent, "oid-1", now=1000)
    assert get_order_context({}, recent, "oid-1") == {
        "token_id": "token-1",
        "size": 6,
        "closed_at": 1000,
    }
