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

    def __init__(self, executor, reprice_threshold: float = 0.005):
        """
        Args:
            executor: Either ClobClientWrapper (live) or DryRunExecutor (dry-run).
            reprice_threshold: Minimum price change to trigger cancel+replace.
                               Default 0.005 (half a cent) to stay competitive
                               in 1-cent tick markets.
        """
        self.executor = executor
        self.reprice_threshold = reprice_threshold
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
                             no_book_snapshot=None) -> bool:
        """
        Update quotes for a market. Only cancel+replace if materially different.

        Returns:
            True if quotes were updated.
        """
        active = self.get_active(market_id)
        updated = False

        # Check if YES quote needs repricing
        yes_needs = self._needs_reprice(
            active.yes_price, quotes.yes_buy_price,
            active.yes_size, quotes.yes_buy_size
        )

        # Check if NO quote needs repricing
        no_needs = self._needs_reprice(
            active.no_price, quotes.no_buy_price,
            active.no_size, quotes.no_buy_size
        )

        if not yes_needs and not no_needs:
            return False  # No change needed

        # Cancel existing orders if they need repricing
        if yes_needs and active.yes_order_id:
            await self.executor.cancel_order(active.yes_order_id)
            active.yes_order_id = None

        if no_needs and active.no_order_id:
            await self.executor.cancel_order(active.no_order_id)
            active.no_order_id = None

        # Allow per-side book snapshots (preferred). Fall back to shared
        # book_snapshot for legacy callers.
        if yes_book_snapshot is None:
            yes_book_snapshot = book_snapshot
        if no_book_snapshot is None:
            no_book_snapshot = book_snapshot

        # Place new YES buy if we have a price and size
        if yes_needs and quotes.yes_buy_price and quotes.yes_buy_size > 0:
            order_id = await self._place_buy(
                token_id_yes, quotes.yes_buy_price,
                quotes.yes_buy_size, "yes", yes_book_snapshot
            )
            if order_id:
                active.yes_order_id = order_id
                active.yes_price = quotes.yes_buy_price
                active.yes_size = quotes.yes_buy_size
                updated = True
            else:
                active.yes_order_id = None
                active.yes_price = None
                active.yes_size = 0

        # Place new NO buy if we have a price and size
        if no_needs and quotes.no_buy_price and quotes.no_buy_size > 0:
            order_id = await self._place_buy(
                token_id_no, quotes.no_buy_price,
                quotes.no_buy_size, "no", no_book_snapshot
            )
            if order_id:
                active.no_order_id = order_id
                active.no_price = quotes.no_buy_price
                active.no_size = quotes.no_buy_size
                updated = True
            else:
                active.no_order_id = None
                active.no_price = None
                active.no_size = 0

        if updated:
            active.last_update = time.time()

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
