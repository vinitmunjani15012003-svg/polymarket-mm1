"""Order tracker service and compatibility state."""

from __future__ import annotations

import time
from typing import Optional

from src.core.models.orders import OrderIntent, OrderState
from src.execution.order_state import ActiveQuotes
from src.monitoring.logger import get_logger


log = get_logger("order_tracker")


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

    def claim_place_orders(
        self,
        orders: list[dict],
    ) -> tuple[list[dict], dict[str, Optional[str]], dict[str, OrderIntent]]:
        """Filter duplicate PLACE specs and claim submit slots.

        Returns ``(filtered_orders, skipped_by_side, intents_by_side)`` so the
        caller can submit only newly-claimed orders while preserving the
        historical side->None result for duplicate suppressions.
        """
        filtered = []
        skipped: dict[str, Optional[str]] = {}
        intents_by_side: dict[str, OrderIntent] = {}
        for order in orders:
            intent = order.get("intent")
            side = str(order.get("side", ""))
            if intent is not None:
                if not self.should_submit(intent):
                    skipped[side] = None
                    log.warning(
                        "duplicate_order_intent_suppressed",
                        market=intent.market_id[:8],
                        side=intent.side,
                        quote_version=intent.quote_version,
                    )
                    continue
                intents_by_side[side] = intent
            filtered.append(order)
        return filtered, skipped, intents_by_side

    def mark_submission_exception(self, intents_by_side: dict[str, OrderIntent]) -> dict[str, Optional[str]]:
        """Release claimed submit slots after a placement exception."""
        for intent in intents_by_side.values():
            self.mark_rejected(intent)
        return {side: None for side in intents_by_side}

    def record_submission_results(
        self,
        placed: dict[str, Optional[str]],
        intents_by_side: dict[str, OrderIntent],
    ) -> bool:
        """Persist successful orders and release failed intents.

        Returns True when any submitted side failed placement.
        """
        failed = False
        for side in intents_by_side:
            if side not in placed:
                placed[side] = None

        for side, order_id in placed.items():
            intent = intents_by_side.get(side)
            if intent is None:
                continue
            if order_id:
                self.mark_order(OrderState(
                    order_id=order_id,
                    intent_id=intent.intent_id,
                    market_id=intent.market_id,
                    side=intent.side,
                    price=intent.price,
                    size=intent.size,
                    status="open",
                    updated_ts=time.time(),
                    metadata={"quote_version": intent.quote_version, "token_id": intent.token_id},
                ))
            else:
                self.mark_rejected(intent)
                failed = True
        return failed

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
