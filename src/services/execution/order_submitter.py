"""Order submission service wrapper."""

from __future__ import annotations

from src.core.models.orders import OrderIntent


class OrderSubmitter:
    def __init__(self, executor):
        self.executor = executor

    async def submit_order(self, intent: OrderIntent):
        if intent.action != "PLACE":
            return None
        return await self.executor.place_buy_order(
            intent.token_id,
            intent.price,
            intent.size,
            side=intent.side,
        )

    async def place_order(self, *args, **kwargs):
        return await self.executor.place_buy_order(*args, **kwargs)
