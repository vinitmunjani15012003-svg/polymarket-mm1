"""CLOB balances facade."""

from __future__ import annotations


class ClobBalances:
    def __init__(self, client):
        self.client = client

    async def sync(self):
        fn = getattr(self.client, "sync_balance_allowance", None)
        if callable(fn):
            return await fn()
        return True
