"""CLOB order facade."""

from __future__ import annotations


class ClobOrders:
    def __init__(self, client):
        self.client = client

    async def create(self, *args, **kwargs):
        return await self.client.place_buy_order(*args, **kwargs)

    async def cancel(self, order_id: str):
        return await self.client.cancel_order(order_id)

    async def fetch(self, market_id: str | None = None):
        if hasattr(self.client, "get_open_orders"):
            return await self.client.get_open_orders(market_id=market_id)
        return getattr(self.client, "open_orders", {})
