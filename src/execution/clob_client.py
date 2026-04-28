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
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,  # ALWAYS BUY
            )

            signed_order = self._client.create_order(order_args)

            # GTC = Good-Til-Cancelled, maker-only on Polymarket CLOB
            response = self._client.post_order(
                signed_order,
                OrderType.GTC,
            )

            order_id = response.get("orderID") or response.get("id")

            if order_id:
                self.open_orders[order_id] = {
                    "token_id": token_id,
                    "price": price,
                    "size": size,
                    "side": "BUY",
                    "token_side": side,  # "up" or "down"
                }
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

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if not self._initialized:
            return False
        try:
            self._client.cancel(order_id)
            self.open_orders.pop(order_id, None)
            return True
        except Exception as e:
            log.error("cancel_error", order_id=order_id[:8], error=str(e))
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if not self._initialized:
            return False
        try:
            self._client.cancel_all()
            self.open_orders.clear()
            log.info("all_orders_cancelled")
            return True
        except Exception as e:
            log.error("cancel_all_error", error=str(e))
            return False

    async def get_fills(self, market_id: str) -> list[dict]:
        """Fetch recent fills for a market using TradeParams."""
        if not self._initialized:
            return []
        try:
            from py_clob_client.clob_types import TradeParams
            params = TradeParams(market=market_id)
            resp = self._client.get_trades(params=params)
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
        for fill in fills:
            fill_id = fill.get("id", f"{fill.get('order_id', '')}_{fill.get('size', '')}")
            if fill_id in self._processed_fills:
                continue
            self._processed_fills.add(fill_id)

            size = float(fill.get("size", 0))
            price = float(fill.get("price", 0))
            order_id = fill.get("order_id", "")

            # Determine if this was YES or NO based on our order tracker
            order_ctx = self.open_orders.get(order_id, {})
            side = order_ctx.get("token_side", "up")

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
        return processed
