"""
Polymarket CLOB client wrapper.

Wraps py_clob_client for order placement with post_only=True enforcement.
All orders are BUY-only.
"""

import asyncio
import time
from typing import Optional
from src.monitoring.logger import get_logger

log = get_logger("clob_client")


class ClobClientWrapper:
    """
    Wraps the Polymarket py_clob_client.
    ENFORCES: all orders are BUY + post_only=True.
    """

    def __init__(self, host: str, private_key: str, chain_id: int,
                 api_key: str, api_secret: str, api_passphrase: str):
        self.host = host
        self._private_key = private_key
        self._chain_id = chain_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._client = None
        self._initialized = False
        # Track open orders: order_id -> {token_id, price, size, side}
        self.open_orders: dict[str, dict] = {}
        self._processed_fills: set = set()
        self._last_fill_check_ts_by_market: dict[str, float] = {}
        self.state_manager = None
        # py-clob-client is synchronous. Keep those calls off the event loop,
        # but serialize access because the underlying client/signing state is
        # not guaranteed to be thread-safe.
        self._client_lock = asyncio.Lock()

    async def _run_client_call(self, fn, *args, **kwargs):
        """Run a blocking py-clob-client call in a worker thread."""
        async with self._client_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    def set_state_manager(self, state_manager):
        self.state_manager = state_manager
        
        # Load open orders
        loaded_orders = self.state_manager.state.get("open_orders", {})
        if loaded_orders:
            self.open_orders = loaded_orders
            log.info("loaded_open_orders", count=len(self.open_orders))
            
        # Load processed fills
        loaded_fills = self.state_manager.state.get("processed_fills", [])
        if loaded_fills:
            self._processed_fills = set(loaded_fills)
            log.info("loaded_processed_fills", count=len(self._processed_fills))

    def _save_orders_state(self):
        if self.state_manager:
            self.state_manager.update_open_orders(self.open_orders)
            
    def _save_fills_state(self):
        if self.state_manager:
            self.state_manager.update_processed_fills(list(self._processed_fills))

    async def initialize(self):
        """Initialize the CLOB client with credentials."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )
            self._client = ClobClient(
                host=self.host,
                chain_id=self._chain_id,
                key=self._private_key,
                creds=creds,
            )
            self._client.set_api_creds(creds)
            self._initialized = True
            
            # Verify auth is working
            addr = self._client.get_address()
            log.info("clob_client_initialized", address=addr)
        except ImportError:
            log.error("py_clob_client_not_installed",
                     msg="Install with: pip install py-clob-client")
            raise
        except Exception as e:
            log.error("clob_init_error", error=str(e))
            raise

    async def place_buy_order(self, token_id: str, price: float,
                               size: float, side: str = "up", book_snapshot=None) -> Optional[str]:
        """
        Place a BUY order with post_only=True.

        This is the ONLY way to place orders. No sells. No taker orders.

        Returns:
            Order ID if placed, None if rejected (post_only rejection is expected).
        """
        if not self._initialized:
            log.error("client_not_initialized")
            return None

        try:
            def _create_and_post():
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.clob_types import PartialCreateOrderOptions
                from py_clob_client.order_builder.constants import BUY

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY,  # ALWAYS BUY
                )

                # Crypto up/down markets are 1-cent tick. If we don't pass tick_size,
                # the API may reject with order_version_mismatch.
                opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
                signed_order = self._client.create_order(order_args, opts)

                # GTC = Good-Til-Cancelled, maker-only on Polymarket CLOB
                return self._client.post_order(
                    signed_order,
                    OrderType.GTC,
                    post_only=True,
                )

            response = await self._run_client_call(_create_and_post)

            order_id = response.get("orderID") or response.get("id")

            if order_id:
                self.open_orders[order_id] = {
                    "token_id": token_id,
                    "price": price,
                    "size": size,
                    "side": "BUY",
                    "token_side": side,  # "up" or "down"
                    "placed_at": time.time(),
                }
                self._save_orders_state()
                log.info("order_placed", order_id=order_id[:8],
                         price=price, size=size, token=token_id[:8], token_side=side)
                return order_id
            else:
                # post_only rejected — order would have crossed spread
                status = response.get("status", "unknown")
                log.info("post_only_rejected", status=status,
                         price=price, token=token_id[:8])
                return None

        except Exception as e:
            log.error("order_place_error", error=str(e),
                     price=price, size=size)
            return None

    async def place_buy_orders(self, orders: list[dict]) -> dict[str, Optional[str]]:
        """Place multiple BUY orders in one CLOB post_orders request."""
        if not self._initialized:
            log.error("client_not_initialized")
            return {str(o.get("side", i)): None for i, o in enumerate(orders)}

        try:
            sides = [spec.get("side", "up") for spec in orders]

            def _create_and_post_batch():
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.clob_types import PartialCreateOrderOptions, PostOrdersArgs
                from py_clob_client.order_builder.constants import BUY

                opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
                post_args = []

                for spec in orders:
                    order_args = OrderArgs(
                        token_id=spec["token_id"],
                        price=spec["price"],
                        size=spec["size"],
                        side=BUY,
                    )
                    signed_order = self._client.create_order(order_args, opts)
                    post_args.append(PostOrdersArgs(
                        signed_order,
                        OrderType.GTC,
                        postOnly=True,
                    ))

                return self._client.post_orders(post_args)

            response = await self._run_client_call(_create_and_post_batch)
            results = response if isinstance(response, list) else response.get("orders", []) if isinstance(response, dict) else []

            placed: dict[str, Optional[str]] = {side: None for side in sides}
            for idx, side in enumerate(sides):
                item = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {}
                order_id = item.get("orderID") or item.get("id")
                if order_id:
                    spec = orders[idx]
                    self.open_orders[order_id] = {
                        "token_id": spec["token_id"],
                        "price": spec["price"],
                        "size": spec["size"],
                        "side": "BUY",
                        "token_side": side,
                        "placed_at": time.time(),
                    }
                    placed[side] = order_id
                    log.info("order_placed", order_id=order_id[:8],
                             price=spec["price"], size=spec["size"],
                             token=spec["token_id"][:8], token_side=side,
                             batch=True)
                else:
                    status = item.get("status", "unknown")
                    spec = orders[idx]
                    log.info("post_only_rejected", status=status,
                             price=spec["price"], token=spec["token_id"][:8],
                             token_side=side, batch=True)

            self._save_orders_state()
            return placed

        except Exception as e:
            log.error("batch_order_place_error", error=str(e), count=len(orders))
            return {str(o.get("side", i)): None for i, o in enumerate(orders)}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if not self._initialized:
            return False
        try:
            await self._run_client_call(self._client.cancel, order_id)
            self.open_orders.pop(order_id, None)
            self._save_orders_state()
            return True
        except Exception as e:
            log.error("cancel_error", order_id=order_id[:8], error=str(e))
            return False

    async def cancel_orders(self, order_ids: list[str]) -> bool:
        """Cancel multiple orders in one CLOB cancel_orders request."""
        if not self._initialized:
            return False
        if not order_ids:
            return True
        try:
            await self._run_client_call(self._client.cancel_orders, order_ids)
            for order_id in order_ids:
                self.open_orders.pop(order_id, None)
            self._save_orders_state()
            log.info("orders_cancelled", count=len(order_ids))
            return True
        except Exception as e:
            log.error("cancel_orders_error", count=len(order_ids), error=str(e))
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if not self._initialized:
            return False
        try:
            await self._run_client_call(self._client.cancel_all)
            self.open_orders.clear()
            self._save_orders_state()
            log.info("all_orders_cancelled")
            return True
        except Exception as e:
            log.error("cancel_all_error", error=str(e))
            return False

    async def get_fills(self, market_id: str) -> list[dict]:
        """Fetch recent fills for a market using TradeParams.
        
        Throttled to max once per 2 seconds to avoid API spam.
        """
        if not self._initialized:
            return []
        
        # Throttle: max 1 request per 2 seconds per market
        now = time.time()
        last_check = self._last_fill_check_ts_by_market.get(market_id, 0.0)
        if now - last_check < 2.0:
            return []
        self._last_fill_check_ts_by_market[market_id] = now
        
        try:
            from py_clob_client.clob_types import TradeParams
            params = TradeParams(market=market_id)
            resp = await self._run_client_call(self._client.get_trades, params=params)
            fills = resp if isinstance(resp, list) else resp.get("data", [])
            return fills
        except Exception as e:
            log.error("get_fills_error", error=str(e))
            return []

    def process_fills(self, fills: list[dict], inventory_mgr,
                      market_id: str, edge_tracker=None,
                      current_mid: float = None) -> list[dict]:
        """Process fills with deduplication and update trackers."""
        processed = []
        fills_changed = False
        orders_changed = False
        for fill in fills:
            fill_id = fill.get("id", f"{fill.get('order_id', '')}_{fill.get('size', '')}")
            if fill_id in self._processed_fills:
                continue
            self._processed_fills.add(fill_id)
            fills_changed = True

            size = float(fill.get("size", 0))
            price = float(fill.get("price", 0))
            order_id = fill.get("order_id", "")

            # Determine if this was YES or NO based on our order tracker
            order_ctx = self.open_orders.get(order_id, {})
            side = order_ctx.get("token_side", "up")

            # Update remaining size and remove if filled
            if order_id in self.open_orders:
                self.open_orders[order_id]["size"] -= size
                if self.open_orders[order_id]["size"] <= 0.0001:  # Floating point safety
                    del self.open_orders[order_id]
                orders_changed = True

            # Update inventory and edge tracker (MarketCycler loops this)
            # Actually MarketCycler is expected to handle inventory directly from fills.
            # So we will just return the standardized fill dict.
            std_fill = {
                "order_id": order_id,
                "token_id": order_ctx.get("token_id", ""),
                "side": side,
                "price": price,
                "size": size,
                "fill_time": time.time(),
                "simulated": False
            }
            processed.append(std_fill)

        if fills_changed:
            self._save_fills_state()
        if orders_changed:
            self._save_orders_state()
        return processed
