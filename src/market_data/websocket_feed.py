"""Websocket feed facade.

The current PriceFeed remains the live implementation; this wrapper gives the
new architecture an explicit market_data boundary without changing behavior.
"""

from __future__ import annotations


class WebsocketFeed:
    def __init__(self, price_feed):
        self.price_feed = price_feed

    async def start(self):
        return await self.price_feed.start()

    def on_price_update(self, callback):
        return self.price_feed.on_price_update(callback)
