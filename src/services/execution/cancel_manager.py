"""Centralized cancel service wrapper.

The execution layer has both single-order and batch-capable executors.  This
adapter gives order lifecycle code one cancellation surface while preserving the
executor's existing semantics.
"""

from __future__ import annotations


class CancelManager:
    def __init__(self, target):
        self.target = target

    async def cancel_order(self, order_id: str) -> bool:
        cancel_one = getattr(self.target, "cancel_order", None)
        if not callable(cancel_one):
            return False
        return bool(await cancel_one(order_id))

    async def cancel_orders(self, order_ids: list[str]) -> bool:
        order_ids = [oid for oid in order_ids if oid]
        if not order_ids:
            return True

        cancel_many = getattr(self.target, "cancel_orders", None)
        if callable(cancel_many):
            return bool(await cancel_many(order_ids))

        ok = True
        for order_id in order_ids:
            ok = bool(await self.cancel_order(order_id)) and ok
        return ok

    async def cancel_all(self) -> bool:
        cancel_all = getattr(self.target, "cancel_all", None)
        if not callable(cancel_all):
            return False
        return bool(await cancel_all())

    async def cancel_market(self, market_id: str) -> bool:
        cancel_market_quotes = getattr(self.target, "cancel_market_quotes", None)
        if not callable(cancel_market_quotes):
            return False
        return bool(await cancel_market_quotes(market_id))

    async def replace_order(self, *args, **kwargs):
        update_quotes = getattr(self.target, "update_quotes", None)
        if not callable(update_quotes):
            return False
        return await update_quotes(*args, **kwargs)
