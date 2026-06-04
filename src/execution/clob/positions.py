"""CLOB positions facade."""

from __future__ import annotations


class ClobPositions:
    def __init__(self, client):
        self.client = client

    async def fetch(self, market_id: str | None = None):
        fn = getattr(self.client, "get_positions", None)
        if callable(fn):
            return await fn(market_id=market_id)
        return []
