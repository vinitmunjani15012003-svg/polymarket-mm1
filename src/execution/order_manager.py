"""
Order manager — single enforcement point for all order operations.

RULES:
  1. Every order is BUY only
  2. Every order has post_only=True
  3. Smart reprice: only cancel+replace if price moved > threshold
"""

import time
from typing import Optional

from src.strategy.quote_engine import QuoteResult
from src.execution.order_state import ActiveQuotes
from src.execution.repricing import RepricePolicy, is_crossed_buy, is_crossed_sell
from src.monitoring.logger import get_logger
from src.services.execution.cancel_manager import CancelManager
from src.services.execution.order_intents import attach_place_intent, next_quote_version
from src.services.execution.order_submitter import OrderSubmitter
from src.services.execution.order_tracker import OrderTracker
from src.services.execution.reconciliation import find_stray_order_ids, find_token_order_ids

log = get_logger("order_manager")


class OrderManager:
    """
    Manages order lifecycle for a single market.
    Enforces BUY-only + post_only at this level.
    """

    def __init__(self, executor, reprice_threshold: float = 0.005,
                 min_update_interval: float = 0.0,
                 crossed_bid_grace_seconds: float = 0.0,
                 repair_crossed_bid_grace_seconds: float = 1.0):
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
        self.order_submitter = OrderSubmitter(executor)
        self.cancel_manager = CancelManager(executor)
        self.order_tracker = OrderTracker()
        self._quote_versions: dict[str, int] = {}
        # Repair quotes are intentionally sticky. The bot is buy-only/post-only,
        # so imbalance repair depends on resting the light-side bid long enough
        # to earn queue priority. Chasing every FV/book wiggle cancels exactly
        # the order we need filled and leaves one-sided inventory into expiry.
        self.repair_reprice_threshold = max(0.05, reprice_threshold)
        self.reprice_policy = RepricePolicy(
            reprice_threshold=self.reprice_threshold,
            repair_reprice_threshold=self.repair_reprice_threshold,
        )
        self.repair_min_update_interval = max(10.0, min_update_interval)
        # Normal BUY maker orders that touch/cross the best ask are adverse-risk
        # candidates; cancel immediately. Repair quotes can still use a short
        # grace to avoid cancelling the one order that may flatten inventory.
        self.crossed_bid_grace_seconds = max(0.0, crossed_bid_grace_seconds)
        self.repair_crossed_bid_grace_seconds = max(
            self.crossed_bid_grace_seconds,
            repair_crossed_bid_grace_seconds,
        )
        self.last_order_error: Optional[str] = None
        self.last_order_warning: Optional[str] = None
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
        self.last_order_error = None
        self.last_order_warning = None

        # Allow per-side book snapshots (preferred). Fall back to shared
        # book_snapshot for legacy callers.
        if yes_book_snapshot is None:
            yes_book_snapshot = book_snapshot
        if no_book_snapshot is None:
            no_book_snapshot = book_snapshot

        # Live safety: py-clob-client startup reconciliation may not list all
        # open orders, and batch/fill paths can leave extras outside ActiveQuotes.
        # Before each quote update, cancel any locally-known extra order on this
        # market's tokens so live behavior stays one-order-per-side like dry-run.
        if not await self._cancel_stray_live_orders(
            market_id, token_id_yes, token_id_no, active
        ):
            self.last_order_error = "stray_live_order_cancel_failed"
            return False

        if not await self._cancel_active_sells_before_buy(market_id, active):
            self.last_order_error = "active_sell_cancel_failed_before_buy"
            return False

        # In repair modes, only the LIGHT side may be quoted. Enforce this at the
        # order manager too so upstream sizing bugs cannot keep buying the heavy
        # side with real funds.
        if repair_mode == "repair_up":
            quotes.no_buy_size = 0
        elif repair_mode == "repair_down":
            quotes.yes_buy_size = 0

        # Check if quotes need repricing. Urgent changes are adverse-risk
        # reductions/removals/crossing-book fixes and are never delayed.
        sticky_repair = repair_mode in ("repair_up", "repair_down")
        yes_repair_side = repair_mode == "repair_up"
        no_repair_side = repair_mode == "repair_down"

        yes_needs, yes_urgent = self.reprice_policy.decision(
            active.yes_price, quotes.yes_buy_price,
            active.yes_size, quotes.yes_buy_size,
            yes_book_snapshot,
            sticky_repair=yes_repair_side,
        )

        no_needs, no_urgent = self.reprice_policy.decision(
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

        # Cancel existing orders if they need repricing. Do not clear local
        # active state until the exchange confirms cancellation; otherwise a
        # failed cancel leaves live exposure invisible to the bot.
        cancel_ids = []
        cancel_yes = bool(yes_needs and active.yes_order_id)
        cancel_no = bool(no_needs and active.no_order_id)

        if cancel_yes and is_crossed_buy(active.yes_price, yes_book_snapshot):
            deferred = await self.cancel_manager.crossed_bid_cancel_should_defer(
                market_id=market_id,
                side="yes",
                order_id=active.yes_order_id,
                grace_seconds=self._crossed_bid_grace(yes_repair_side),
                sticky_repair=yes_repair_side,
                clear_active_side=lambda: self._clear_active_side(active, "yes"),
            )
            if deferred:
                cancel_yes = False
                yes_needs = False

        if cancel_no and is_crossed_buy(active.no_price, no_book_snapshot):
            deferred = await self.cancel_manager.crossed_bid_cancel_should_defer(
                market_id=market_id,
                side="no",
                order_id=active.no_order_id,
                grace_seconds=self._crossed_bid_grace(no_repair_side),
                sticky_repair=no_repair_side,
                clear_active_side=lambda: self._clear_active_side(active, "no"),
            )
            if deferred:
                cancel_no = False
                no_needs = False

        if cancel_yes:
            cancel_ids.append(active.yes_order_id)
        if cancel_no:
            cancel_ids.append(active.no_order_id)

        total_start = time.perf_counter()
        cancel_ms = 0.0
        place_ms = 0.0

        if cancel_ids:
            cancel_start = time.perf_counter()
            cancel_ok = await self.cancel_manager.cancel_orders(cancel_ids)
            cancel_ms = (time.perf_counter() - cancel_start) * 1000
            if not cancel_ok:
                log.error("quote_cancel_failed_halt_reprice",
                          market=market_id[:8],
                          order_ids=[oid[:8] for oid in cancel_ids])
                self.last_order_error = "quote_cancel_failed_halt_reprice"
                return False
            if cancel_yes:
                active.yes_order_id = None
                active.yes_price = None
                active.yes_size = 0
            if cancel_no:
                active.no_order_id = None
                active.no_price = None
                active.no_size = 0

        quote_version = None
        place_specs = []
        if yes_needs and quotes.yes_buy_price and quotes.yes_buy_size > 0:
            if quote_version is None:
                quote_version = self._advance_quote_version(market_id)
            place_specs.append(attach_place_intent({
                "token_id": token_id_yes,
                "price": quotes.yes_buy_price,
                "size": quotes.yes_buy_size,
                "side": "yes",
                "book_snapshot": yes_book_snapshot,
            }, market_id=market_id, quote_version=quote_version))
        elif yes_needs:
            active.yes_order_id = None
            active.yes_price = None
            active.yes_size = 0

        if no_needs and quotes.no_buy_price and quotes.no_buy_size > 0:
            if quote_version is None:
                quote_version = self._advance_quote_version(market_id)
            place_specs.append(attach_place_intent({
                "token_id": token_id_no,
                "price": quotes.no_buy_price,
                "size": quotes.no_buy_size,
                "side": "no",
                "book_snapshot": no_book_snapshot,
            }, market_id=market_id, quote_version=quote_version))
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

    async def _cancel_stray_live_orders(self, market_id: str, token_id_yes: str,
                                        token_id_no: str, active: ActiveQuotes) -> bool:
        """Cancel locally-known live orders for this market not in ActiveQuotes."""
        open_orders = getattr(self.executor, "open_orders", None)
        if not isinstance(open_orders, dict):
            return True

        stray_ids = find_stray_order_ids(
            active,
            open_orders,
            {str(token_id_yes), str(token_id_no)},
        )

        if not stray_ids:
            return True

        log.warning(
            "stray_live_orders_cancelled_before_quote",
            market=market_id[:8],
            count=len(stray_ids),
            order_ids=[oid[:8] for oid in stray_ids[:8]],
        )
        ok = await self.cancel_manager.cancel_orders(stray_ids)
        if not ok:
            log.error(
                "stray_live_order_cancel_failed",
                market=market_id[:8],
                order_ids=[oid[:8] for oid in stray_ids[:8]],
            )
        return ok

    async def _cancel_active_sells_before_buy(self, market_id: str, active: ActiveQuotes) -> bool:
        sell_ids = [
            oid for oid in (active.yes_sell_order_id, active.no_sell_order_id) if oid
        ]
        if not sell_ids:
            return True
        ok = await self.cancel_manager.cancel_orders(sell_ids)
        if ok:
            active.yes_sell_order_id = None
            active.no_sell_order_id = None
            active.yes_sell_price = None
            active.no_sell_price = None
            active.yes_sell_size = 0
            active.no_sell_size = 0
            log.warning(
                "active_sells_cancelled_before_buy_quotes",
                market=market_id[:8],
                count=len(sell_ids),
                order_ids=[oid[:8] for oid in sell_ids[:8]],
            )
        else:
            log.error(
                "active_sell_cancel_failed_before_buy",
                market=market_id[:8],
                order_ids=[oid[:8] for oid in sell_ids[:8]],
            )
        return ok

    async def update_close_only_sell(self, market_id: str, token_id: str, *,
                                     side: str, price: float | None, size: int,
                                     book_snapshot=None) -> bool:
        """Maintain one close-only SELL order for unmatched heavy inventory."""
        active = self.get_active(market_id)
        self.last_order_error = None
        self.last_order_warning = None
        side = "yes" if side in ("yes", "up") else "no"

        # SELL and BUY repair must never run simultaneously for the same market.
        buy_ids = [oid for oid in (active.yes_order_id, active.no_order_id) if oid]
        if buy_ids:
            ok = await self.cancel_manager.cancel_orders(buy_ids)
            if not ok:
                self.last_order_error = "buy_cancel_failed_before_sell"
                log.error(
                    "buy_cancel_failed_before_sell",
                    market=market_id[:8],
                    order_ids=[oid[:8] for oid in buy_ids[:8]],
                )
                return False
            active.yes_order_id = None
            active.no_order_id = None
            active.yes_price = None
            active.no_price = None
            active.yes_size = 0
            active.no_size = 0

        current_id = active.yes_sell_order_id if side == "yes" else active.no_sell_order_id
        current_price = active.yes_sell_price if side == "yes" else active.no_sell_price
        current_size = active.yes_sell_size if side == "yes" else active.no_sell_size
        other_id = active.no_sell_order_id if side == "yes" else active.yes_sell_order_id

        cancel_ids = []
        if other_id:
            cancel_ids.append(other_id)

        needs = self.reprice_policy.needs_reprice(current_price, price, int(current_size or 0), int(size or 0))
        if current_id and is_crossed_sell(current_price, book_snapshot):
            needs = True
        if current_id and needs:
            cancel_ids.append(current_id)

        if cancel_ids:
            ok = await self.cancel_manager.cancel_orders(cancel_ids)
            if not ok:
                self.last_order_error = "sell_cancel_failed_halt_reprice"
                log.error(
                    "sell_cancel_failed_halt_reprice",
                    market=market_id[:8],
                    order_ids=[oid[:8] for oid in cancel_ids[:8]],
                )
                return False
            if side == "yes":
                active.no_sell_order_id = None
                active.no_sell_price = None
                active.no_sell_size = 0
                if current_id in cancel_ids:
                    active.yes_sell_order_id = None
                    active.yes_sell_price = None
                    active.yes_sell_size = 0
            else:
                active.yes_sell_order_id = None
                active.yes_sell_price = None
                active.yes_sell_size = 0
                if current_id in cancel_ids:
                    active.no_sell_order_id = None
                    active.no_sell_price = None
                    active.no_sell_size = 0

        if not needs and current_id:
            return False

        if not price or size <= 0:
            return bool(cancel_ids)

        quote_version = self._advance_quote_version(market_id)
        spec = attach_place_intent({
            "token_id": token_id,
            "price": price,
            "size": int(size),
            "side": side,
            "book_snapshot": book_snapshot,
            "execution_side": "SELL",
            "close_only": True,
        }, market_id=market_id, quote_version=quote_version)
        placed = await self._place_sells([spec])
        order_id = placed.get(side)
        if order_id:
            if side == "yes":
                active.yes_sell_order_id = order_id
                active.yes_sell_price = price
                active.yes_sell_size = int(size)
            else:
                active.no_sell_order_id = order_id
                active.no_sell_price = price
                active.no_sell_size = int(size)
            active.last_update = time.time()
            log.info(
                "close_only_sell_updated",
                market=market_id[:8],
                side=side,
                price=price,
                size=size,
                order_id=order_id[:8],
            )
            return True

        # Canceling stale buys/sells is not enough to claim a close-only sell is
        # working. Surface the actual placement miss so the dashboard does not
        # show "sell @ price" when no sell order id exists.
        self.last_order_warning = "close_only_sell_not_placed"
        log.warning(
            "close_only_sell_not_placed",
            market=market_id[:8],
            side=side,
            price=price,
            size=size,
            placed=placed,
            cancelled=len(cancel_ids),
        )
        return False

    async def cancel_side_quotes(self, market_id: str, side: str, token_id: str):
        """Cancel all known quotes for one side/token of a market."""
        active = self.active.get(market_id)
        cancel_ids = []

        if side in ("yes", "up") and active and active.yes_order_id:
            cancel_ids.append(active.yes_order_id)
        if side in ("yes", "up") and active and active.yes_sell_order_id:
            cancel_ids.append(active.yes_sell_order_id)
        if side in ("no", "down") and active and active.no_order_id:
            cancel_ids.append(active.no_order_id)
        if side in ("no", "down") and active and active.no_sell_order_id:
            cancel_ids.append(active.no_sell_order_id)

        open_orders = getattr(self.executor, "open_orders", None)
        if isinstance(open_orders, dict):
            for oid in find_token_order_ids(open_orders, token_id, exclude=set(cancel_ids)):
                cancel_ids.append(oid)

        if not cancel_ids:
            return True

        ok = await self.cancel_manager.cancel_orders(cancel_ids)

        if ok and active:
            if side in ("yes", "up"):
                active.yes_order_id = None
                active.yes_price = None
                active.yes_size = 0
                active.yes_sell_order_id = None
                active.yes_sell_price = None
                active.yes_sell_size = 0
            else:
                active.no_order_id = None
                active.no_price = None
                active.no_size = 0
                active.no_sell_order_id = None
                active.no_sell_price = None
                active.no_sell_size = 0
            log.warning(
                "side_quotes_cancelled",
                market=market_id[:8],
                side=side,
                count=len(cancel_ids),
                order_ids=[oid[:8] for oid in cancel_ids[:8]],
            )
        elif not ok:
            log.error(
                "side_quote_cancel_failed",
                market=market_id[:8],
                side=side,
                order_ids=[oid[:8] for oid in cancel_ids[:8]],
            )
        return ok

    async def cancel_market_quotes(self, market_id: str) -> bool:
        """Cancel all quotes for a specific market."""
        active = self.active.get(market_id)
        if not active:
            return True

        ok = await self.cancel_manager.cancel_orders(
            [oid for oid in (
                active.yes_order_id,
                active.no_order_id,
                active.yes_sell_order_id,
                active.no_sell_order_id,
            ) if oid]
        )

        if ok:
            self.active[market_id] = ActiveQuotes()
        else:
            log.error("cancel_market_quotes_failed", market=market_id[:8])
            self.last_order_error = "cancel_market_quotes_failed"
        return ok

    async def cancel_all(self) -> bool:
        """Cancel all orders across all markets."""
        ok = bool(await self.cancel_manager.cancel_all())
        if ok:
            self.active.clear()
        else:
            log.error("cancel_all_failed_active_preserved")
            self.last_order_error = "cancel_all_failed"
        return ok

    def _advance_quote_version(self, market_id: str) -> int:
        version = next_quote_version(self._quote_versions.get(market_id, 0))
        self._quote_versions[market_id] = version
        return version

    async def _place_buys(self, orders: list[dict]) -> dict[str, Optional[str]]:
        """Place one or more BUY orders, using executor batch API when available."""
        filtered, skipped, intents_by_side = self.order_tracker.claim_place_orders(orders)

        if not filtered:
            return skipped

        try:
            placed = await self.order_submitter.place_buys(filtered)
        except Exception as exc:
            failed = self.order_tracker.mark_submission_exception(intents_by_side)
            log.error(
                "order_placement_failed",
                error=str(exc),
                sides=list(intents_by_side),
            )
            self.last_order_error = "order_placement_failed"
            return {**skipped, **failed}

        failed_submission = self.order_tracker.record_submission_results(placed, intents_by_side)
        if failed_submission:
            await self._handle_unconfirmed_placement(intents_by_side)
        return {**skipped, **placed}

    async def _place_sells(self, orders: list[dict]) -> dict[str, Optional[str]]:
        """Place one or more close-only SELL orders."""
        filtered, skipped, intents_by_side = self.order_tracker.claim_place_orders(orders)
        if not filtered:
            return skipped
        try:
            placed = await self.order_submitter.place_sells(filtered)
        except Exception as exc:
            failed = self.order_tracker.mark_submission_exception(intents_by_side)
            log.error(
                "sell_placement_failed",
                error=str(exc),
                sides=list(intents_by_side),
            )
            self.last_order_error = "sell_placement_failed"
            return {**skipped, **failed}
        failed_submission = self.order_tracker.record_submission_results(placed, intents_by_side)
        if failed_submission:
            await self._handle_unconfirmed_placement(intents_by_side)
        return {**skipped, **placed}

    async def _handle_unconfirmed_placement(self, intents_by_side: dict) -> None:
        """Handle a placement that returned no order id without killing the bot.

        CLOB can return transient 5xx/timeouts after accepting or rejecting an
        order. Treat those as ambiguous/non-fatal, reconcile open orders, and let
        the next quote cycle decide. Non-transient SDK/auth errors remain fatal.
        Plain no-id results without an executor error are usually post-only
        rejections and are also non-fatal.
        """
        error = str(getattr(self.executor, "last_place_error", "") or "")
        transient = bool(getattr(self.executor, "last_place_error_transient", False))
        market_id = ""
        if intents_by_side:
            first_intent = next(iter(intents_by_side.values()))
            market_id = getattr(first_intent, "market_id", "") or ""

        if error and not transient:
            log.error(
                "order_placement_failed_nontransient",
                market=market_id[:8],
                error=error,
                sides=list(intents_by_side),
            )
            self.last_order_error = "order_placement_failed"
            return

        self.last_order_warning = "order_placement_unconfirmed"
        log.warning(
            "order_placement_unconfirmed_nonfatal",
            market=market_id[:8],
            transient=transient,
            error=error,
            sides=list(intents_by_side),
        )
        if transient:
            await self._reconcile_after_unconfirmed_placement(market_id)

    async def _reconcile_after_unconfirmed_placement(self, market_id: str) -> None:
        reconcile = getattr(self.executor, "reconcile_on_startup", None)
        if not callable(reconcile):
            return
        try:
            result = await reconcile()
            log.info(
                "order_placement_timeout_reconciled",
                market=market_id[:8],
                open_orders=(result or {}).get("open_orders"),
                ok=(result or {}).get("ok"),
            )
        except Exception as exc:
            log.warning(
                "order_placement_timeout_reconcile_failed",
                market=market_id[:8],
                error=str(exc),
            )

    def _crossed_bid_grace(self, sticky_repair: bool) -> float:
        return self.repair_crossed_bid_grace_seconds if sticky_repair else self.crossed_bid_grace_seconds

    def _clear_active_side(self, active: ActiveQuotes, side: str):
        if side == "yes":
            active.yes_order_id = None
            active.yes_price = None
            active.yes_size = 0
        else:
            active.no_order_id = None
            active.no_price = None
            active.no_size = 0

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
