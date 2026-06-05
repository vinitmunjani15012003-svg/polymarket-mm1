"""CLOB balances facade and small balance/allowance helpers."""

from __future__ import annotations


def zero_allowance_spenders(allowances: dict) -> list:
    """Return spender addresses whose allowance value is exactly zero."""
    return [addr for addr, val in allowances.items() if str(val) == "0"]


def parse_balance_allowance(result: dict) -> dict:
    """Extract balance/allowance verification fields from a CLOB response."""
    allowances = result.get("allowances", {}) if isinstance(result, dict) else {}
    balance = result.get("balance", "0") if isinstance(result, dict) else "0"
    zero_allowances = zero_allowance_spenders(allowances if isinstance(allowances, dict) else {})
    return {
        "balance": balance,
        "allowances": allowances if isinstance(allowances, dict) else {},
        "zero_allowances": zero_allowances,
        "verified": not zero_allowances,
    }


class ClobBalances:
    def __init__(self, client):
        self.client = client

    async def sync(self):
        fn = getattr(self.client, "sync_balance_allowance", None)
        if callable(fn):
            return await fn()
        return True
