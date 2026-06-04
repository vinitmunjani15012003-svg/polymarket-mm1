"""Order tracker service and compatibility state."""

from __future__ import annotations

from src.core.models.orders import OrderIntent, OrderState
from src.execution.order_manager import ActiveQuotes


class OrderTracker:
    def __init__(self):
        self.pending: dict[str, OrderIntent] = {}
        self.orders: dict[str, OrderState] = {}

    def add_intent(self, intent: OrderIntent) -> str:
        self.pending[intent.intent_id] = intent
        return intent.intent_id

    def mark_order(self, state: OrderState):
        self.orders[state.order_id] = state
        self.pending.pop(state.intent_id, None)
