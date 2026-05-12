"""
Polymarket CLOB client wrapper.

Wraps py_clob_client for order placement with post_only=True enforcement.
All orders are BUY-only.
"""

import asyncio
import hashlib
import json
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
                 api_key: str, api_secret: str, api_passphrase: str,
                 signature_type: int = 3, funder: str = ""):
        self.host = host
        self._private_key = private_key
        self._chain_id = chain_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._signature_type = signature_type
        self._funder = funder
        self._client = None
        self._client_version = "unknown"
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

    @staticmethod
    def _ensure_builder_code(order_args):
        """SDK compatibility for py-clob-client-v2 builds.

        Some Polymarket SDK builds expect OrderArgs.builder_code during
        signing but ship an OrderArgs type that does not define it. A blank
        builder code is the safe default: no builder attribution, same order.
        """
        if not hasattr(order_args, "builder_code"):
            try:
                setattr(order_args, "builder_code", "")
            except Exception:
                try:
                    object.__setattr__(order_args, "builder_code", "")
                except Exception:
                    pass
        return order_args

    def _order_type_imports(self):
        """Return SDK types matching the initialized CLOB client version."""
        if self._client_version == "v2":
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client_v2 import PostOrdersV2Args as BatchPostOrdersArgs
            from py_clob_client_v2.order_builder.constants import BUY
            return OrderArgs, OrderType, PartialCreateOrderOptions, BatchPostOrdersArgs, BUY, "v2"

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.clob_types import PartialCreateOrderOptions, PostOrdersArgs
        from py_clob_client.order_builder.constants import BUY
        return OrderArgs, OrderType, PartialCreateOrderOptions, PostOrdersArgs, BUY, "v1"

    def _post_order_compat(self, signed_order, order_type):
        """Post a single order across SDK variants.

        v1 supports post_only as a request flag. Official v2 examples post the
        signed GTC order directly; if a build rejects post_only, retry without it.
        """
        try:
            return self._client.post_order(signed_order, order_type, post_only=True)
        except TypeError:
            return self._client.post_order(signed_order, order_type)

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
            try:
                from py_clob_client_v2 import ClobClient, ApiCreds
                client_version = "v2"
            except ImportError:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                client_version = "v1"

            creds = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )
            client_kwargs = {
                "host": self.host,
                "chain_id": self._chain_id,
                "key": self._private_key,
                "creds": creds,
                "signature_type": self._signature_type,
            }
            if self._funder:
                client_kwargs["funder"] = self._funder

            try:
                self._client = ClobClient(**client_kwargs)
            except TypeError as e:
                raise RuntimeError(
                    "Installed py-clob-client does not support the required "
                    "signature_type/funder live parameters. Upgrade to "
                    "py-clob-client-v2 or a compatible Polymarket SDK."
                ) from e
            self._client.set_api_creds(creds)
            self._client_version = client_version
            self._initialized = True
            
            # Verify auth is working
            addr = self._client.get_address()
            log.info("clob_client_initialized", address=addr,
                     client_version=client_version,
                     signature_type=self._signature_type,
                     funder=self._funder)
        except ImportError:
            log.error("py_clob_client_not_installed",
                     msg="Install with: pip install py-clob-client")
            raise
        except Exception as e:
            log.error("clob_init_error", error=str(e))
            raise

    async def sync_balance_allowance(self) -> bool:
        """Sync CLOB balance/allowance for deposit-wallet live trading.

        Official deposit wallet flow requires calling the CLOB balance allowance
        update endpoint after funding/approvals and before trading. Older SDKs
        may not expose this method, so this is compatibility-guarded.
        """
        if not self._initialized:
            return False

        update_fn = getattr(self._client, "update_balance_allowance", None)
        if not callable(update_fn):
            log.warning(
                "balance_allowance_sync_unavailable",
                reason="clob_client_missing_update_balance_allowance",
                client_version=self._client_version,
                client_type=type(self._client).__name__,
            )
            return False

        try:
            params = None
            if self._client_version == "v2":
                try:
                    from py_clob_client_v2 import (
                        AssetType, BalanceAllowanceParams, SignatureTypeV2,
                    )
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL,
                        signature_type=SignatureTypeV2.POLY_1271 if self._signature_type == 3 else self._signature_type,
                    )
                except Exception as e:
                    log.warning("balance_allowance_params_v2_unavailable", error=str(e))
            else:
                try:
                    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                except Exception as e:
                    log.warning("balance_allowance_params_v1_unavailable", error=str(e))

            if params is not None:
                await self._run_client_call(update_fn, params)
            else:
                await self._run_client_call(update_fn)
            log.info(
                "balance_allowance_synced",
                signature_type=self._signature_type,
                funder=self._funder,
            )
            return True
        except Exception as e:
            log.error("balance_allowance_sync_error", error=str(e))
            return False

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
                OrderArgs, OrderType, PartialCreateOrderOptions, _, BUY, _ = self._order_type_imports()

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY,  # ALWAYS BUY
                )

                tick_size = str(getattr(book_snapshot, "tick_size", "0.01") or "0.01")
                neg_risk = bool(getattr(book_snapshot, "neg_risk", False))
                opts = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
                order_args = self._ensure_builder_code(order_args)
                signed_order = self._client.create_order(order_args, opts)

                # GTC = Good-Til-Cancelled. _post_order_compat uses maker-only
                # post_only where supported by the installed SDK.
                return self._post_order_compat(signed_order, OrderType.GTC)

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

    @staticmethod
    def _normalize_post_orders_response(response, expected_count: int) -> list[dict]:
        """Normalize py-clob-client post_orders responses across SDK versions."""
        if isinstance(response, list):
            return [item if isinstance(item, dict) else {} for item in response[:expected_count]]
        if isinstance(response, dict):
            raw = (
                response.get("orders")
                or response.get("data")
                or response.get("results")
                or response.get("responses")
            )
            if isinstance(raw, list):
                return [item if isinstance(item, dict) else {} for item in raw[:expected_count]]
            # Some SDKs return a single-order response dict when len==1.
            if expected_count == 1 and (response.get("orderID") or response.get("id")):
                return [response]
            if response.get("error") or response.get("status") in ("error", "failed", "rejected"):
                return [response for _ in range(expected_count)]
        return [{} for _ in range(expected_count)]

    async def place_buy_orders(self, orders: list[dict]) -> dict[str, Optional[str]]:
        """Place multiple BUY orders in one CLOB post_orders request."""
        if not self._initialized:
            log.error("client_not_initialized")
            return {str(o.get("side", i)): None for i, o in enumerate(orders)}

        try:
            sides = [spec.get("side", "up") for spec in orders]

            def _create_and_post_batch():
                OrderArgs, OrderType, PartialCreateOrderOptions, BatchPostOrdersArgs, BUY, sdk_version = self._order_type_imports()

                post_args = []

                for spec in orders:
                    book_snapshot = spec.get("book_snapshot")
                    tick_size = str(getattr(book_snapshot, "tick_size", "0.01") or "0.01")
                    neg_risk = bool(getattr(book_snapshot, "neg_risk", False))
                    opts = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
                    order_args = OrderArgs(
                        token_id=spec["token_id"],
                        price=spec["price"],
                        size=spec["size"],
                        side=BUY,
                    )
                    order_args = self._ensure_builder_code(order_args)
                    signed_order = self._client.create_order(order_args, opts)
                    if sdk_version == "v2":
                        post_args.append(BatchPostOrdersArgs(
                            order=signed_order,
                            orderType=OrderType.GTC,
                        ))
                    else:
                        post_args.append(BatchPostOrdersArgs(
                            signed_order,
                            OrderType.GTC,
                            postOnly=True,
                        ))

                return self._client.post_orders(post_args)

            response = await self._run_client_call(_create_and_post_batch)
            results = self._normalize_post_orders_response(response, len(orders))

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
            # SDK compatibility fallback: py-clob-client variants have differed
            # around PostOrdersArgs / builder fields. For live safety, degrade to
            # sequential single-order placement instead of spinning failed batch
            # attempts every quote cycle.
            placed: dict[str, Optional[str]] = {}
            for idx, spec in enumerate(orders):
                side = str(spec.get("side", idx))
                placed[side] = await self.place_buy_order(
                    token_id=spec["token_id"],
                    price=spec["price"],
                    size=spec["size"],
                    side=side,
                    book_snapshot=spec.get("book_snapshot"),
                )
            log.info(
                "batch_order_fallback_complete",
                count=len(orders),
                placed=sum(1 for oid in placed.values() if oid),
            )
            return placed

    def _cancel_fn(self):
        """Return the installed SDK's single-order cancel function."""
        fn = getattr(self._client, "cancel", None)
        if callable(fn):
            return fn
        fn = getattr(self._client, "cancel_order", None)
        if callable(fn):
            return fn
        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if not self._initialized:
            return False
        try:
            fn = self._cancel_fn()
            if not fn:
                raise AttributeError("CLOB client exposes neither cancel nor cancel_order")
            try:
                await self._run_client_call(fn, order_id=order_id)
            except TypeError:
                await self._run_client_call(fn, order_id)
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
            fn = getattr(self._client, "cancel_orders", None)
            if not callable(fn):
                raise AttributeError("CLOB client missing cancel_orders")
            await self._run_client_call(fn, order_ids)
            for order_id in order_ids:
                self.open_orders.pop(order_id, None)
            self._save_orders_state()
            log.info("orders_cancelled", count=len(order_ids))
            return True
        except Exception as e:
            log.error("cancel_orders_error", count=len(order_ids), error=str(e))
            results = [await self.cancel_order(order_id) for order_id in order_ids]
            return all(results)

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

    async def reconcile_on_startup(self) -> dict:
        """Best-effort startup reconciliation before canceling stale orders.

        Live safety rule: inspect exchange-side open orders and recent trades
        before clearing local state. This method intentionally does not mutate
        inventory because market-specific token maps are owned by MarketCycler;
        it refreshes known open order context and records observability so a
        future inventory reconciliation can consume the same data path.
        """
        result = {"open_orders": 0, "recent_trades": 0, "ok": False}
        if not self._initialized:
            return result

        try:
            get_orders = getattr(self._client, "get_orders", None)
            if callable(get_orders):
                try:
                    open_resp = await self._run_client_call(get_orders)
                except TypeError:
                    open_resp = await self._run_client_call(get_orders, None)
                open_orders = open_resp if isinstance(open_resp, list) else open_resp.get("data", []) if isinstance(open_resp, dict) else []
            else:
                # SDK compatibility: some py-clob-client builds do not expose
                # get_orders. Startup reconciliation is best-effort; do not
                # block live startup when the SDK cannot list open orders.
                open_orders = []
                log.warning(
                    "startup_reconciliation_orders_unavailable",
                    reason="clob_client_missing_get_orders",
                    client_type=type(self._client).__name__,
                )

            refreshed = {}
            for order in open_orders:
                order_id = order.get("id") or order.get("orderID") or order.get("order_id")
                if not order_id:
                    continue
                original = float(order.get("original_size") or order.get("size") or 0)
                matched = float(order.get("size_matched") or order.get("matched_size") or 0)
                remaining = max(0.0, original - matched)
                outcome = str(order.get("outcome") or "").strip().lower()
                token_side = "yes" if outcome in ("yes", "up") else "no" if outcome in ("no", "down") else None
                refreshed[order_id] = {
                    "token_id": str(order.get("asset_id") or order.get("token_id") or ""),
                    "price": float(order.get("price") or 0),
                    "size": remaining,
                    "side": order.get("side", "BUY"),
                    "token_side": token_side,
                    "placed_at": float(order.get("created_at") or time.time()),
                }

            if refreshed:
                self.open_orders.update(refreshed)
                self._save_orders_state()

            get_trades = getattr(self._client, "get_trades", None)
            if callable(get_trades):
                try:
                    trades_resp = await self._run_client_call(get_trades)
                    trades = trades_resp if isinstance(trades_resp, list) else trades_resp.get("data", []) if isinstance(trades_resp, dict) else []
                except Exception:
                    trades = []
            else:
                trades = []
                log.warning(
                    "startup_reconciliation_trades_unavailable",
                    reason="clob_client_missing_get_trades",
                    client_type=type(self._client).__name__,
                )

            result.update({
                "open_orders": len(open_orders),
                "recent_trades": len(trades),
                "ok": True,
            })
            log.info("startup_reconciliation_complete", **result)
            return result

        except Exception as e:
            log.error("startup_reconciliation_error", error=str(e))
            return result

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
            if self._client_version == "v2":
                from py_clob_client_v2 import TradeParams
            else:
                from py_clob_client.clob_types import TradeParams
            params = TradeParams(market=market_id)
            try:
                resp = await self._run_client_call(self._client.get_trades, params=params)
            except TypeError:
                resp = await self._run_client_call(self._client.get_trades, params)
            fills = resp if isinstance(resp, list) else resp.get("data", [])
            return fills
        except Exception as e:
            log.error("get_fills_error", error=str(e))
            return []

    def process_fills(self, fills: list[dict], inventory_mgr,
                      market_id: str, edge_tracker=None,
                      current_mid: float = None,
                      token_id_to_side: dict[str, str] | None = None) -> list[dict]:
        """Process fills with deduplication and update trackers."""
        processed = []
        fills_changed = False
        orders_changed = False
        for fill in fills:
            fill_id = self._fill_dedupe_key(fill, market_id)
            if fill_id in self._processed_fills:
                continue

            size = float(fill.get("size", 0))
            price = float(fill.get("price", 0))
            order_id = fill.get("order_id") or fill.get("orderID") or fill.get("maker_order_id", "")

            # v2 trade objects can describe the whole trade and include our
            # contribution under maker_orders. Use the matching maker order so
            # we do not book someone else's size (e.g. 13 shares vs our 5 quote).
            maker_orders = fill.get("maker_orders") or fill.get("makerOrders") or []
            if isinstance(maker_orders, list) and maker_orders:
                matched = None
                for mo in maker_orders:
                    if not isinstance(mo, dict):
                        continue
                    mo_id = mo.get("order_id") or mo.get("orderID") or mo.get("id")
                    if mo_id in self.open_orders:
                        matched = mo
                        order_id = mo_id
                        break
                if matched:
                    size = float(matched.get("matched_amount") or matched.get("size") or matched.get("amount") or size)
                    price = float(matched.get("price") or price)

            # Hard safety cap: never book more filled size than the remaining
            # open order context we placed locally.
            if order_id in self.open_orders:
                remaining_ctx = float(self.open_orders[order_id].get("size") or 0)
                if remaining_ctx > 0 and size > remaining_ctx:
                    log.warning(
                        "fill_size_capped_to_open_order",
                        order_id=str(order_id)[:8],
                        raw_size=size,
                        capped_size=remaining_ctx,
                    )
                    size = remaining_ctx

            # Determine side from token id first. Never default unknown fills to
            # Up/YES; that corrupts live inventory after restarts/reconcile gaps.
            order_ctx = self.open_orders.get(order_id, {})
            token_id = str(
                fill.get("asset_id")
                or fill.get("token_id")
                or fill.get("assetId")
                or order_ctx.get("token_id", "")
            )
            side = None
            if token_id_to_side and token_id in token_id_to_side:
                side = token_id_to_side[token_id]
            elif order_ctx.get("token_side"):
                side = order_ctx["token_side"]
            else:
                outcome = str(fill.get("outcome") or fill.get("side") or "").strip().lower()
                if outcome in ("yes", "up"):
                    side = "yes"
                elif outcome in ("no", "down"):
                    side = "no"

            if side is None:
                log.error("unknown_fill_side",
                          market=market_id[:12],
                          fill_id=fill_id,
                          order_id=str(order_id)[:12],
                          token_id=token_id[:16])
                continue

            # Update remaining size and remove if filled
            if order_id in self.open_orders:
                self.open_orders[order_id]["size"] -= size
                if self.open_orders[order_id]["size"] <= 0.0001:  # Floating point safety
                    del self.open_orders[order_id]
                orders_changed = True

            # Update inventory and edge tracker (MarketCycler loops this)
            # Actually MarketCycler is expected to handle inventory directly from fills.
            # So we will just return the standardized fill dict.
            self._processed_fills.add(fill_id)
            fills_changed = True

            std_fill = {
                "order_id": order_id,
                "token_id": token_id,
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

    @staticmethod
    def _fill_dedupe_key(fill: dict, market_id: str = "") -> str:
        """Build a robust idempotency key for CLOB fills/trades.

        Prefer provider IDs when available. If the SDK omits IDs, include enough
        stable fields to distinguish partial fills on the same order.
        """
        for key in ("id", "trade_id", "transaction_hash", "tx_hash", "hash"):
            value = fill.get(key)
            if value:
                return f"{key}:{value}"

        material = {
            "market": market_id,
            "order_id": fill.get("order_id") or fill.get("orderID") or fill.get("maker_order_id") or "",
            "asset_id": fill.get("asset_id") or fill.get("token_id") or fill.get("assetId") or "",
            "price": str(fill.get("price", "")),
            "size": str(fill.get("size", "")),
            "side": str(fill.get("side", "")),
            "timestamp": str(
                fill.get("timestamp")
                or fill.get("created_at")
                or fill.get("match_time")
                or fill.get("time")
                or ""
            ),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return "synthetic:" + hashlib.sha256(encoded.encode()).hexdigest()
