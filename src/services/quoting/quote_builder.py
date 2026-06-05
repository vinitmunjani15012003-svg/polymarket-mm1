"""QuotePlan construction helpers."""

from __future__ import annotations

from src.core.models.decision import QuotePlan
from .quote_sanity import validate_quote_pair


def build_quote(bid_price: float | None,
                bid_size: float,
                action: str = "QUOTE",
                ask_price: float | None = None,
                ask_size: float = 0.0,
                **metadata) -> QuotePlan:
    return QuotePlan(
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=float(bid_size or 0.0),
        ask_size=float(ask_size or 0.0),
        action=action,
        metadata=metadata,
    )


def construct_orders(market_id: str, token_id_yes: str, token_id_no: str, quotes) -> list[dict]:
    orders = []
    if getattr(quotes, "yes_buy_price", None) and getattr(quotes, "yes_buy_size", 0) > 0:
        orders.append({"market_id": market_id, "token_id": token_id_yes, "side": "yes", "price": quotes.yes_buy_price, "size": quotes.yes_buy_size})
    if getattr(quotes, "no_buy_price", None) and getattr(quotes, "no_buy_size", 0) > 0:
        orders.append({"market_id": market_id, "token_id": token_id_no, "side": "no", "price": quotes.no_buy_price, "size": quotes.no_buy_size})
    return orders


def quote_pair_decision(quotes):
    return validate_quote_pair(getattr(quotes, "yes_buy_price", None), getattr(quotes, "no_buy_price", None))
