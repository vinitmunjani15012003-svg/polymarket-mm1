"""Websocket feed facade.

The current PriceFeed remains the live implementation; this wrapper gives the
new architecture an explicit market_data boundary without changing behavior.
"""

from __future__ import annotations

from typing import Callable


class WebsocketFeed:
    def __init__(self, price_feed):
        self.price_feed = price_feed

    async def start(self):
        return await self.price_feed.start()

    async def stop(self):
        if hasattr(self.price_feed, "stop"):
            return await self.price_feed.stop()
        return None

    def on_price_update(self, callback: Callable):
        return self.price_feed.on_price_update(callback)

    def get_price(self, symbol: str):
        return self.price_feed.get_price(symbol)

    def get_price_age(self, symbol: str) -> float:
        return self.price_feed.get_price_age(symbol)

    def get_price_source(self, symbol: str) -> str:
        if hasattr(self.price_feed, "get_price_source"):
            return self.price_feed.get_price_source(symbol)
        return "unknown"

    def freshness(self, symbol: str, max_age_seconds: float):
        if hasattr(self.price_feed, "get_feed_freshness"):
            return self.price_feed.get_feed_freshness(symbol, max_age_seconds)
        from .feed_health import freshness

        return freshness(self.get_price_age(symbol), max_age_seconds, self.get_price_source(symbol))
