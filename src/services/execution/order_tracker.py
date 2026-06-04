"""Order tracker service and compatibility state."""

from __future__ import annotations

import time

from src.core.models.orders import OrderIntent, OrderState
from src.execution.order_state import ActiveQuotes


class OrderTracker:
    def __init__(self):
        self.pending: dict[str, OrderIntent] = {}
        self.orders: dict[str, OrderState] = {}
        self.failed_intents: dict[str, OrderIntent] = {}
        self._submitted_versions: set[tuple[str, str, int]] = set()

    @staticmethod
    def _version_key(intent: OrderIntent) -> tuple[str, str, int]:
        return (intent.market_id, intent.side, int(intent.quote_version))

    def add_intent(self, intent: OrderIntent) -> str:
        self.pending[intent.intent_id] = intent
        return intent.intent_id

    def should_submit(self, intent: OrderIntent) -> bool:
        """Claim a market/side/quote-version submit slot once.

        The claim is intentionally coarser than intent_id (which includes price,
        size and token) so accidental duplicate PLACE specs for the same quote
        version cannot submit twice on a side.
        """
        key = self._version_key(intent)
        if key in self._submitted_versions:
            return False
        self._submitted_versions.add(key)
        self.add_intent(intent)
        return True

    def mark_order(self, state: OrderState):
        self.orders[state.order_id] = state
        self.pending.pop(state.intent_id, None)
        self.failed_intents.pop(state.intent_id, None)

    def release_intent(self, intent: OrderIntent):
        """Release a claimed submit slot so the same quote version can retry."""
        self.pending.pop(intent.intent_id, None)
        self._submitted_versions.discard(self._version_key(intent))

    def mark_rejected(self, intent: OrderIntent, *, release: bool = True):
        self.failed_intents[intent.intent_id] = intent
        if release:
            self.release_intent(intent)
            return
        self.pending.pop(intent.intent_id, None)

    @staticmethod
    def state_from_active_side(active: ActiveQuotes, market_id: str, side: str,
                               *, quote_version: int = 0,
                               token_id: str = "") -> OrderState | None:
        if side == "yes":
            order_id = active.yes_order_id
            price = active.yes_price
            size = active.yes_size
        else:
            order_id = active.no_order_id
            price = active.no_price
            size = active.no_size

        if not order_id:
            return None

        intent = OrderIntent(
            market_id=market_id,
            quote_version=int(quote_version),
            side=side,  # type: ignore[arg-type]
            price=price,
            size=float(size or 0),
            token_id=str(token_id),
        )
        return OrderState(
            order_id=order_id,
            intent_id=intent.intent_id,
            market_id=market_id,
            side=side,
            price=price,
            size=float(size or 0),
            status="open",
            updated_ts=time.time(),
            metadata={"quote_version": int(quote_version), "token_id": str(token_id)},
        )

    def reconstruct_from_active_quotes(self, market_id: str, active: ActiveQuotes,
                                       *, quote_version: int = 0,
                                       token_id_yes: str = "",
                                       token_id_no: str = "") -> dict[str, OrderState]:
        """Rebuild tracker order states from ActiveQuotes-like persisted data."""
        rebuilt: dict[str, OrderState] = {}
        for side, token_id in (("yes", token_id_yes), ("no", token_id_no)):
            state = self.state_from_active_side(
                active,
                market_id,
                side,
                quote_version=quote_version,
                token_id=token_id,
            )
            if state:
                self.orders[state.order_id] = state
                rebuilt[state.order_id] = state
        return rebuilt
