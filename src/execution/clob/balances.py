"""CLOB balances facade and small balance/allowance helpers."""

from __future__ import annotations


def zero_allowance_spenders(allowances: dict) -> list:
    """Return spender addresses whose allowance value is exactly zero."""
    return [addr for addr, val in allowances.items() if str(val) == "0"]


class ClobBalances:
    def __init__(self, client):
        self.client = client

    async def sync(self):
        fn = getattr(self.client, "sync_balance_allowance", None)
        if callable(fn):
            return await fn()
        return True
