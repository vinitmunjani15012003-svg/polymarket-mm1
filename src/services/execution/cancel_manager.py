"""Centralized cancel service wrapper.

The execution layer has both single-order and batch-capable executors.  This
adapter gives order lifecycle code one cancellation surface while preserving the
executor's existing semantics.
"""

from __future__ import annotations

import asyncio
import inspect

from src.monitoring.logger import get_logger


log = get_logger("cancel_manager")


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

    async def order_still_open(self, order_id: str) -> bool:
        """Best-effort exchange/local check for whether an order is still open."""
        checker = getattr(self.target, "is_order_open", None)
        if callable(checker):
            result = checker(order_id)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)

        open_orders = getattr(self.target, "open_orders", None)
        if isinstance(open_orders, dict):
            return order_id in open_orders

        # Unknown executor: fail conservative and assume it is still open.
        return True

    async def crossed_bid_cancel_should_defer(
        self,
        *,
        market_id: str,
        side: str,
        order_id: str,
        grace_seconds: float,
        sticky_repair: bool = False,
        clear_active_side=None,
    ) -> bool:
        """Return True when crossed-bid cancel/repost should be skipped.

        A BUY maker bid at/above best ask may already have filled while local
        state lags.  Check open-state before and after an optional grace window;
        if the order disappeared, let reconciliation/fill sync run before new
        exposure is placed.
        """
        if not await self.order_still_open(order_id):
            if callable(clear_active_side):
                clear_active_side()
            log.warning(
                "crossed_bid_already_closed_before_cancel",
                market=market_id[:8],
                side=side,
                order_id=order_id[:8],
            )
            return True

        grace = max(0.0, float(grace_seconds or 0.0))
        if grace > 0:
            log.info(
                "crossed_bid_grace_wait",
                market=market_id[:8],
                side=side,
                order_id=order_id[:8],
                grace_ms=round(grace * 1000),
                repair=sticky_repair,
            )
            await asyncio.sleep(grace)

        if not await self.order_still_open(order_id):
            if callable(clear_active_side):
                clear_active_side()
            log.warning(
                "crossed_bid_closed_during_grace",
                market=market_id[:8],
                side=side,
                order_id=order_id[:8],
                repair=sticky_repair,
            )
            return True

        log.warning(
            "crossed_bid_cancel_after_grace",
            market=market_id[:8],
            side=side,
            order_id=order_id[:8],
            repair=sticky_repair,
        )
        return False
