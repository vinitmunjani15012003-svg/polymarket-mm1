"""Order submission service wrapper.

This is the small adapter boundary between order lifecycle code and concrete
executors.  Live and dry-run executors are intentionally similar but not quite
identical, so keep signature probing and batch fallbacks here instead of in the
OrderManager state machine.
"""

from __future__ import annotations

import inspect
from typing import Optional

from src.core.models.orders import OrderIntent
from src.services.execution.order_intents import strip_execution_metadata


class OrderSubmitter:
    def __init__(self, executor):
        self.executor = executor
        self._accepts_side = False
        if hasattr(executor, "place_buy_order"):
            sig = inspect.signature(executor.place_buy_order)
            self._accepts_side = "side" in sig.parameters
        self._accepts_sell_side = False
        if hasattr(executor, "place_sell_order"):
            sig = inspect.signature(executor.place_sell_order)
            self._accepts_sell_side = "side" in sig.parameters

    async def submit_order(self, intent: OrderIntent, book_snapshot=None):
        if intent.action != "PLACE":
            return None
        if intent.price is None or intent.size <= 0:
            return None
        if str(getattr(intent, "execution_side", "BUY") or "BUY").upper() == "SELL":
            return await self.place_sell(
                intent.token_id,
                intent.price,
                intent.size,
                side=intent.side,
                book_snapshot=book_snapshot,
                close_only=bool(getattr(intent, "close_only", True)),
            )
        return await self.place_buy(
            intent.token_id,
            intent.price,
            intent.size,
            side=intent.side,
            book_snapshot=book_snapshot,
        )

    async def place_buy(self, token_id: str, price: float, size: float,
                        side: str, book_snapshot=None) -> Optional[str]:
        if not hasattr(self.executor, "place_buy_order"):
            return None
        if self._accepts_side:
            return await self.executor.place_buy_order(
                token_id, price, size, side=side, book_snapshot=book_snapshot
            )
        return await self.executor.place_buy_order(token_id, price, size)

    async def place_sell(self, token_id: str, price: float, size: float,
                         side: str, book_snapshot=None,
                         close_only: bool = True) -> Optional[str]:
        if not hasattr(self.executor, "place_sell_order"):
            return None
        if self._accepts_sell_side:
            return await self.executor.place_sell_order(
                token_id, price, size, side=side,
                book_snapshot=book_snapshot, close_only=close_only
            )
        return await self.executor.place_sell_order(token_id, price, size)

    async def place_buys(self, orders: list[dict]) -> dict[str, Optional[str]]:
        executor_orders = [strip_execution_metadata(order) for order in orders]
        if hasattr(self.executor, "place_buy_orders"):
            return await self.executor.place_buy_orders(executor_orders)

        placed: dict[str, Optional[str]] = {}
        for order in executor_orders:
            placed[order["side"]] = await self.place_buy(
                order["token_id"],
                order["price"],
                order["size"],
                order["side"],
                order.get("book_snapshot"),
            )
        return placed

    async def place_sells(self, orders: list[dict]) -> dict[str, Optional[str]]:
        executor_orders = [strip_execution_metadata(order) for order in orders]
        if hasattr(self.executor, "place_sell_orders"):
            return await self.executor.place_sell_orders(executor_orders)

        placed: dict[str, Optional[str]] = {}
        for order in executor_orders:
            placed[order["side"]] = await self.place_sell(
                order["token_id"],
                order["price"],
                order["size"],
                order["side"],
                order.get("book_snapshot"),
                close_only=bool(order.get("close_only", True)),
            )
        return placed

    async def place_order(self, *args, **kwargs):
        return await self.executor.place_buy_order(*args, **kwargs)
