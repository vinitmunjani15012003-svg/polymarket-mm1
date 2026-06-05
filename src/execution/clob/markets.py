"""CLOB markets facade."""

from __future__ import annotations


class ClobMarkets:
    def __init__(self, client):
        self.client = client

    async def fetch(self, slug: str | None = None):
        fn = getattr(self.client, "get_market", None)
        if callable(fn):
            return await fn(slug)
        return None
