"""Centralized cancel service wrapper."""

from __future__ import annotations


class CancelManager:
    def __init__(self, order_manager):
        self.order_manager = order_manager

    async def cancel_order(self, order_id: str):
        return await self.order_manager.cancel_order(order_id)

    async def cancel_all(self):
        return await self.order_manager.cancel_all()

    async def cancel_market(self, market_id: str):
        return await self.order_manager.cancel_market_quotes(market_id)

    async def replace_order(self, *args, **kwargs):
        return await self.order_manager.update_quotes(*args, **kwargs)
