"""
Order manager — single enforcement point for all order operations.

RULES:
  1. Every order is BUY only
  2. Every order has post_only=True
  3. Smart reprice: only cancel+replace if price moved > threshold
"""

import time
from typing import Optional
from dataclasses import dataclass

from src.strategy.quote_engine import QuoteResult
from src.monitoring.logger import get_logger

log = get_logger("order_manager")


@dataclass
class ActiveQuotes:
    """Currently active quotes for a market."""
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    yes_size: int = 0
    no_size: int = 0
    last_update: float = 0.0


class OrderManager:
    """
    Manages order lifecycle for a single market.
    Enforces BUY-only + post_only at this level.
    """

    def __init__(self, executor, reprice_threshold: float = 0.005,
                 min_update_interval: float = 0.0):
        """
        Args:
            executor: Either ClobClientWrapper (live) or DryRunExecutor (dry-run).
            reprice_threshold: Minimum price change to trigger cancel+replace.
                               Default 0.005 (half a cent) to stay competitive
                               in 1-cent tick markets.
        """
        self.executor = executor
        self.reprice_threshold = reprice_threshold
        self.min_update_interval = min_update_interval
        # Repair quotes are intentionally sticky. The bot is buy-only/post-only,
        # so imbalance repair depends on resting the light-side bid long enough
        # to earn queue priority. Chasing every FV/book wiggle cancels exactly
        # the order we need filled and leaves one-sided inventory into expiry.
        self.repair_reprice_threshold = max(0.05, reprice_threshold)
        self.repair_min_update_interval = max(10.0, min_update_interval)
        # Active quotes per market
        self.active: dict[str, ActiveQuotes] = {}

    def get_active(self, market_id: str) -> ActiveQuotes:
        if market_id not in self.active:
            self.active[market_id] = ActiveQuotes()
        return self.active[market_id]

    async def update_quotes(self, market_id: str,
                             token_id_yes: str, token_id_no: str,
                             quotes: QuoteResult,
                             book_snapshot=None,
                             yes_book_snapshot=None,
                             no_book_snapshot=None,
                             repair_mode: str = "normal") -> bool:
        """
        Update quotes for a market. Only cancel+replace if materially different.

        Returns:
            True if quotes were updated.
        """
        active = self.get_active(market_id)
        updated = False

        # Allow per-side book snapshots (preferred). Fall back to shared
        # book_snapshot for legacy callers.
        if yes_book_snapshot is None:
            yes_book_snapshot = book_snapshot
        if no_book_snapshot is None:
            no_book_snapshot = book_snapshot

        # Check if quotes need repricing. Urgent changes are adverse-risk
        # reductions/removals/crossing-book fixes and are never delayed.
        sticky_repair = repair_mode in ("repair_up", "repair_down")
        yes_repair_side = repair_mode == "repair_up"
        no_repair_side = repair_mode == "repair_down"

        yes_needs, yes_urgent = self._reprice_decision(
            active.yes_price, quotes.yes_buy_price,
            active.yes_size, quotes.yes_buy_size,
            yes_book_snapshot,
            sticky_repair=yes_repair_side,
        )

        no_needs, no_urgent = self._reprice_decision(
            active.no_price, quotes.no_buy_price,
            active.no_size, quotes.no_buy_size,
            no_book_snapshot,
            sticky_repair=no_repair_side,
        )

        min_interval = self.repair_min_update_interval if sticky_repair else self.min_update_interval
        if min_interval > 0 and active.last_update > 0:
            elapsed = time.time() - active.last_update
            if elapsed < min_interval:
                # Do not cancel/repost just to improve a bid or increase size
                # too frequently; that burns queue priority. Still allow
                # adverse reprices and quote removals immediately.
                if yes_needs and not yes_urgent:
                    yes_needs = False
                if no_needs and not no_urgent:
                    no_needs = False

        if not yes_needs and not no_needs:
            return False  # No change needed

        # Cancel existing orders if they need repricing
        cancel_ids = []
        if yes_needs and active.yes_order_id:
            cancel_ids.append(active.yes_order_id)
            active.yes_order_id = None
            active.yes_price = None
            active.yes_size = 0

        if no_needs and active.no_order_id:
            cancel_ids.append(active.no_order_id)
            active.no_order_id = None
            active.no_price = None
            active.no_size = 0

        total_start = time.perf_counter()
        cancel_ms = 0.0
        place_ms = 0.0

        if cancel_ids:
            cancel_start = time.perf_counter()
            if hasattr(self.executor, 'cancel_orders'):
                await self.executor.cancel_orders(cancel_ids)
            else:
                for order_id in cancel_ids:
                    await self.executor.cancel_order(order_id)
            cancel_ms = (time.perf_counter() - cancel_start) * 1000

        place_specs = []
        if yes_needs and quotes.yes_buy_price and quotes.yes_buy_size > 0:
            place_specs.append({
                "token_id": token_id_yes,
                "price": quotes.yes_buy_price,
                "size": quotes.yes_buy_size,
                "side": "yes",
                "book_snapshot": yes_book_snapshot,
            })
        elif yes_needs:
            active.yes_order_id = None
            active.yes_price = None
            active.yes_size = 0

        if no_needs and quotes.no_buy_price and quotes.no_buy_size > 0:
            place_specs.append({
                "token_id": token_id_no,
                "price": quotes.no_buy_price,
                "size": quotes.no_buy_size,
                "side": "no",
                "book_snapshot": no_book_snapshot,
            })
        elif no_needs:
            active.no_order_id = None
            active.no_price = None
            active.no_size = 0

        placed = {}
        if place_specs:
            place_start = time.perf_counter()
            placed = await self._place_buys(place_specs)
            place_ms = (time.perf_counter() - place_start) * 1000

        yes_order_id = placed.get("yes")
        if yes_needs and yes_order_id:
            active.yes_order_id = yes_order_id
            active.yes_price = quotes.yes_buy_price
            active.yes_size = quotes.yes_buy_size
            updated = True

        no_order_id = placed.get("no")
        if no_needs and no_order_id:
            active.no_order_id = no_order_id
            active.no_price = quotes.no_buy_price
            active.no_size = quotes.no_buy_size
            updated = True

        if updated:
            active.last_update = time.time()

        if cancel_ids or place_specs:
            log.info("order_update_latency",
                     market=market_id[:8],
                     mode=repair_mode,
                     cancels=len(cancel_ids),
                     placements=len(place_specs),
                     yes_price=quotes.yes_buy_price,
                     yes_size=quotes.yes_buy_size,
                     no_price=quotes.no_buy_price,
                     no_size=quotes.no_buy_size,
                     cancel_ms=round(cancel_ms, 1),
                     place_ms=round(place_ms, 1),
                     total_ms=round((time.perf_counter() - total_start) * 1000, 1))

        return updated

    async def cancel_market_quotes(self, market_id: str):
        """Cancel all quotes for a specific market."""
        active = self.active.get(market_id)
        if not active:
            return

        if active.yes_order_id:
            await self.executor.cancel_order(active.yes_order_id)
        if active.no_order_id:
            await self.executor.cancel_order(active.no_order_id)

        self.active[market_id] = ActiveQuotes()

    async def cancel_all(self):
        """Cancel all orders across all markets."""
        await self.executor.cancel_all()
        self.active.clear()

    async def _place_buy(self, token_id: str, price: float,
                          size: float, side: str,
                          book_snapshot=None) -> Optional[str]:
        """
        Place a BUY order. This is the single enforcement point.
        """
        # Dry-run executor accepts side parameter
        if hasattr(self.executor, 'place_buy_order'):
            # Check if executor is DryRunExecutor (accepts side param)
            import inspect
            sig = inspect.signature(self.executor.place_buy_order)
            if 'side' in sig.parameters:
                return await self.executor.place_buy_order(
                    token_id, price, size, side=side,
                    book_snapshot=book_snapshot
                )
            else:
                return await self.executor.place_buy_order(
                    token_id, price, size
                )
        return None

    async def _place_buys(self, orders: list[dict]) -> dict[str, Optional[str]]:
        """Place one or more BUY orders, using executor batch API when available."""
        if hasattr(self.executor, 'place_buy_orders'):
            return await self.executor.place_buy_orders(orders)

        placed = {}
        for order in orders:
            placed[order["side"]] = await self._place_buy(
                order["token_id"], order["price"], order["size"],
                order["side"], order.get("book_snapshot")
            )
        return placed

    def _needs_reprice(self, existing_price: Optional[float],
                       new_price: Optional[float],
                       existing_size: int,
                       new_size: int) -> bool:
        """Check if a quote needs to be repriced.

        Uses >= (not >) so that half-cent changes trigger repricing in a
        1-cent tick market.  Also reprices on significant size changes in
        EITHER direction — stale large orders accumulate adverse fills.
        """
        # Always reprice if no existing quote
        if existing_price is None:
            return new_price is not None and new_size > 0

        # Remove quote if new is None or zero size
        if new_price is None or new_size <= 0:
            return True

        # Reprice if price moved more than threshold (>= not >)
        if abs(new_price - existing_price) >= self.reprice_threshold:
            return True

        # Reprice on significant size change in EITHER direction (>50%)
        if existing_size > 0:
            ratio = new_size / existing_size
            if ratio < 0.5 or ratio > 1.5:
                return True

        return False

    def _reprice_decision(self, existing_price: Optional[float],
                          new_price: Optional[float],
                          existing_size: int,
                          new_size: int,
                          book_snapshot=None,
                          sticky_repair: bool = False) -> tuple[bool, bool]:
        """Return (needs_reprice, urgent)."""
        # Always place if no existing quote; not urgent because there is no
        # stale risk, but no cooldown applies before first placement anyway.
        if existing_price is None:
            return (new_price is not None and new_size > 0), False

        # Remove quote if new is None or zero size. This is urgent because a
        # risk phase/capital guard decided the quote should not exist.
        if new_price is None or new_size <= 0:
            return True, True

        # If our BUY bid crosses/touches best ask, cancel immediately.
        if book_snapshot is not None and existing_price >= book_snapshot.best_ask:
            return True, True

        price_delta = new_price - existing_price

        if sticky_repair:
            # In repair mode, queue priority is the product. Keep the existing
            # light-side bid resting unless it is dangerously stale. Small FV
            # wiggles should not cancel the only order that can flatten us.
            if abs(price_delta) > self.repair_reprice_threshold:
                return True, price_delta < 0
            if existing_size > 0 and new_size > existing_size * 2.0:
                return True, False
            return False, False

        if abs(price_delta) > self.reprice_threshold:
            # Lowering a BUY bid reduces adverse selection / overpaying risk.
            # Raising a BUY bid is just chasing/improving and can be throttled.
            return True, price_delta < 0

        # To preserve queue position, DO NOT reprice if size merely decreases.
        # Only reprice if we need significantly MORE size (>50% increase), and
        # treat that as non-urgent so it can be rate-limited.
        if existing_size > 0 and new_size > existing_size * 1.5:
            return True, False

        return False, False

    def check_stale_quotes(self, market_id: str,
                            yes_book=None, no_book=None) -> bool:
        """
        Check if our quotes are stale (book moved past them).
        Returns True if quotes were cancelled.
        """
        active = self.active.get(market_id)
        if not active:
            return False

        stale = False

        # For BUY orders: stale if our bid is above the best ask
        # (we'd buy at a loss)
        if active.yes_price and yes_book:
            if active.yes_price >= yes_book.best_ask:
                log.warning("stale_yes_buy",
                           our_price=active.yes_price,
                           best_ask=yes_book.best_ask)
                stale = True

        if active.no_price and no_book:
            if active.no_price >= no_book.best_ask:
                log.warning("stale_no_buy",
                           our_price=active.no_price,
                           best_ask=no_book.best_ask)
                stale = True

        return stale
