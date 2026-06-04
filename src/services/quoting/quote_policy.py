"""QuotePolicy facade for future full policy orchestration."""

from __future__ import annotations

from src.core.models.decision import DecisionResult
from .quote_builder import construct_orders
from .quote_sanity import validate_quote_pair


class QuotePolicy:
    def validate(self, quotes) -> DecisionResult:
        return validate_quote_pair(getattr(quotes, "yes_buy_price", None), getattr(quotes, "no_buy_price", None))

    def construct_orders(self, market_id: str, token_id_yes: str, token_id_no: str, quotes) -> list[dict]:
        return construct_orders(market_id, token_id_yes, token_id_no, quotes)
