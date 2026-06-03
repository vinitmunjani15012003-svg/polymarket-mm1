"""Redundant Polymarket CLOB market-data websocket cache.

Runs multiple parallel Polymarket market websocket connections and exposes the
freshest validated top-of-book snapshot per token. Intended as a low-latency
companion/fallback ahead of REST `/books` polling.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import websockets

from src.data.orderbook import BookSnapshot
from src.monitoring.logger import get_logger

log = get_logger("redundant_poly_ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class ConnectionHealth:
    connection_id: int
    connected: bool = False
    started_ts: float = 0.0
    last_msg_ts: float = 0.0
    last_valid_ts: float = 0.0
    updates_received: int = 0
    valid_updates: int = 0
    rejected_updates: int = 0
    reconnect_count: int = 0
    jitter_ema: Optional[float] = None
    last_delta: Optional[float] = None

    @property
    def seconds_since_valid(self) -> float:
        if not self.last_valid_ts:
            return float("inf")
        return max(0.0, time.time() - self.last_valid_ts)


@dataclass
class TokenHealth:
    token_id: str
    last_valid_ts: float = 0.0
    source_connection_id: Optional[int] = None
    rejected_updates: int = 0
    stale_after_seconds: float = 1.0

    @property
    def age_seconds(self) -> float:
        if not self.last_valid_ts:
            return float("inf")
        return max(0.0, time.time() - self.last_valid_ts)

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > self.stale_after_seconds


class _L2Book:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.tick_size: str = "0.01"

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def apply(self, *, side: str, price: float, size: float) -> None:
        book = self.bids if side == "buy" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def snapshot(self, token_id: str) -> Optional[BookSnapshot]:
        if not self.bids or not self.asks:
            return None
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)
        asks = sorted(self.asks.items(), key=lambda x: x[0])
        best_bid, best_bid_size = bids[0]
        best_ask, best_ask_size = asks[0]
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            return None
        mid = (best_bid + best_ask) / 2
        micro = (
            (best_bid * best_ask_size + best_ask * best_bid_size)
            / (best_bid_size + best_ask_size)
            if best_bid_size + best_ask_size > 0
            else mid
        )
        return BookSnapshot(
            token_id=token_id,
            timestamp=time.time(),
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            mid_price=mid,
            micro_price=micro,
            tick_size=self.tick_size,
        )


class _Connection:
    def __init__(self, parent: "RedundantPolymarketWS", connection_id: int):
        self.parent = parent
        self.connection_id = connection_id
        self.health = ConnectionHealth(connection_id=connection_id)
        self.books: dict[str, _L2Book] = {}
        self._ws = None
        self._tasks: list[asyncio.Task] = []
        self._dropped_first_book: set[str] = set()

    async def start(self) -> None:
        if self.parent._closing:
            return
        await self.connect()
        if self.parent._closing:
            await self.close()
            return
        if self.parent.subscribed:
            await self.subscribe(sorted(self.parent.subscribed), initial_dump=True)
        self._tasks = [
            asyncio.create_task(self._ping_loop()),
            asyncio.create_task(self._recv_loop()),
        ]

    async def connect(self) -> None:
        if self.parent._closing:
            return
        self._ws = await websockets.connect(
            WS_URL,
            ping_interval=None,
            close_timeout=3,
            max_size=2**20,
        )
        now = time.time()
        self.health.connected = True
        self.health.started_ts = now
        self.health.last_msg_ts = now
        self._dropped_first_book.clear()

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        self._tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.health.connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    async def reconnect(self) -> None:
        if self.parent._closing:
            return
        await self.close()
        if self.parent._closing:
            return
        self.health.reconnect_count += 1
        await self.start()

    async def subscribe(self, token_ids: list[str], *, initial_dump: bool = True) -> None:
        if not self._ws:
            return
        msg = {
            "assets_ids": [str(t) for t in token_ids],
            "asset_ids": [str(t) for t in token_ids],
            "type": "market",
            "initial_dump": initial_dump,
            "level": 2,
            "custom_feature_enabled": False,
        }
        await self._ws.send(json.dumps(msg))

    async def _ping_loop(self) -> None:
        while True:
            try:
                await self._ws.send("PING")
            except Exception:
                self.health.connected = False
                return
            await asyncio.sleep(10)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                if raw == "PONG":
                    continue
                self._record_msg_time()
                try:
                    msg = json.loads(raw)
                except Exception:
                    self.health.rejected_updates += 1
                    continue
                events = msg if isinstance(msg, list) else [msg]
                for event in events:
                    await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.health.connected = False
            if not self.parent._closing:
                log.warning("poly_ws_connection_closed", conn=self.connection_id, error=str(exc))

    def _record_msg_time(self) -> None:
        now = time.time()
        prev = self.health.last_msg_ts
        self.health.last_msg_ts = now
        if prev:
            delta = now - prev
            if self.health.last_delta is not None:
                jitter = abs(delta - self.health.last_delta)
                self.health.jitter_ema = jitter if self.health.jitter_ema is None else 0.8 * self.health.jitter_ema + 0.2 * jitter
            self.health.last_delta = delta
        self.health.updates_received += 1

    async def _handle_event(self, msg: dict) -> None:
        et = msg.get("event_type")
        if et == "book":
            token_id = str(msg.get("asset_id"))
            if token_id not in self.parent.subscribed:
                return
            if self.parent.drop_first_book and token_id not in self._dropped_first_book:
                self._dropped_first_book.add(token_id)
                return
            book = self.books.setdefault(token_id, _L2Book())
            book.clear()
            book.tick_size = str(msg.get("tick_size") or msg.get("minimum_tick_size") or book.tick_size)
            for level in msg.get("bids") or []:
                self._apply_level(book, "buy", level)
            for level in msg.get("asks") or []:
                self._apply_level(book, "sell", level)
            await self._publish(token_id, book)
            return

        if et == "price_change":
            for pc in msg.get("price_changes") or []:
                token_id = str(pc.get("asset_id"))
                if token_id not in self.parent.subscribed:
                    continue
                book = self.books.setdefault(token_id, _L2Book())
                try:
                    side = (pc.get("side") or "").lower()
                    price = float(pc.get("price"))
                    size = float(pc.get("size"))
                except Exception:
                    self.health.rejected_updates += 1
                    continue
                if side in ("buy", "sell"):
                    book.apply(side=side, price=price, size=size)
                    await self._publish(token_id, book)
            return

        if et == "tick_size_change":
            token_id = str(msg.get("asset_id"))
            book = self.books.setdefault(token_id, _L2Book())
            book.tick_size = str(msg.get("tick_size") or book.tick_size)

    def _apply_level(self, book: _L2Book, side: str, level) -> None:
        try:
            price = float(level["price"] if isinstance(level, dict) else level[0])
            size = float(level["size"] if isinstance(level, dict) else level[1])
            book.apply(side=side, price=price, size=size)
        except Exception:
            self.health.rejected_updates += 1

    async def _publish(self, token_id: str, book: _L2Book) -> None:
        snap = book.snapshot(token_id)
        if snap is None:
            self.health.rejected_updates += 1
            self.parent.reject(token_id)
            return
        if self.parent.validate(token_id, snap):
            self.health.valid_updates += 1
            self.health.last_valid_ts = snap.timestamp
            self.parent.accept(self.connection_id, snap)
        else:
            self.health.rejected_updates += 1
            self.parent.reject(token_id)


class RedundantPolymarketWS:
    def __init__(
        self,
        *,
        connection_count: int = 5,
        stale_seconds: float = 1.0,
        jump_reject: float = 0.15,
        drop_first_book: bool = True,
        stagger_seconds: float = 0.2,
        reconnect_stale_seconds: float = 5.0,
        max_reconnects_per_minute: int = 10,
    ):
        self.connection_count = max(1, connection_count)
        self.stale_seconds = stale_seconds
        self.jump_reject = jump_reject
        self.drop_first_book = drop_first_book
        self.stagger_seconds = stagger_seconds
        self.reconnect_stale_seconds = reconnect_stale_seconds
        self.max_reconnects_per_minute = max_reconnects_per_minute
        self.connections = [_Connection(self, i) for i in range(self.connection_count)]
        self.books: dict[str, BookSnapshot] = {}
        self.token_health: dict[str, TokenHealth] = {}
        self.subscribed: set[str] = set()
        self._started = False
        self._closing = False
        self._health_task: Optional[asyncio.Task] = None
        self._reconnect_times: deque[float] = deque()
        self._last_stale_log_ts: dict[str, float] = {}

    async def ensure_started(self) -> None:
        if self._started:
            return
        self._closing = False
        for conn in self.connections:
            await conn.start()
            if self._closing:
                return
            if self.stagger_seconds > 0:
                await asyncio.sleep(self.stagger_seconds)
        self._health_task = asyncio.create_task(self._health_loop())
        self._started = True
        log.info("redundant_poly_ws_started", connections=self.connection_count)

    async def subscribe(self, token_ids: list[str]) -> None:
        ids = [str(t) for t in token_ids]
        new_ids = [t for t in ids if t not in self.subscribed]
        self.subscribed.update(ids)
        for token_id in ids:
            self.token_health.setdefault(token_id, TokenHealth(token_id=token_id, stale_after_seconds=self.stale_seconds))
        if self._started and new_ids:
            await asyncio.gather(*(conn.subscribe(new_ids, initial_dump=True) for conn in self.connections))

    def get(self, token_id: str) -> Optional[BookSnapshot]:
        token_id = str(token_id)
        health = self.token_health.get(token_id)
        if health and health.is_stale:
            age = health.age_seconds
            age_ms = None if age == float("inf") else round(age * 1000)
            now = time.time()
            last_log_ts = self._last_stale_log_ts.get(token_id, 0.0)
            if now - last_log_ts >= max(1.0, self.stale_seconds):
                self._last_stale_log_ts[token_id] = now
                log.warning("poly_ws_stale", token_id=token_id, age_ms=age_ms, source=health.source_connection_id)
            return None
        return self.books.get(token_id)

    def connection_health(self) -> list[ConnectionHealth]:
        return [conn.health for conn in self.connections]

    def validate(self, token_id: str, snap: BookSnapshot) -> bool:
        if snap.best_bid <= 0 or snap.best_ask <= 0 or snap.best_bid >= snap.best_ask:
            return False
        if not (0.01 <= snap.best_bid <= 0.99 and 0.01 <= snap.best_ask <= 0.99):
            return False
        current = self.books.get(token_id)
        if current:
            if abs(snap.best_bid - current.best_bid) > self.jump_reject:
                return False
            if abs(snap.best_ask - current.best_ask) > self.jump_reject:
                return False
        return True

    def accept(self, connection_id: int, snap: BookSnapshot) -> None:
        self.books[snap.token_id] = snap
        health = self.token_health.setdefault(snap.token_id, TokenHealth(token_id=snap.token_id, stale_after_seconds=self.stale_seconds))
        health.last_valid_ts = snap.timestamp
        health.source_connection_id = connection_id

    def reject(self, token_id: str) -> None:
        health = self.token_health.setdefault(str(token_id), TokenHealth(token_id=str(token_id), stale_after_seconds=self.stale_seconds))
        health.rejected_updates += 1

    async def close(self) -> None:
        self._closing = True
        if self._health_task:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
            self._health_task = None
        await asyncio.gather(*(conn.close() for conn in self.connections), return_exceptions=True)
        self._started = False

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self._closing:
                return
            await self._reconnect_unhealthy()
            if self._closing:
                return
            for h in self.connection_health():
                log.info(
                    "poly_ws_health",
                    conn=h.connection_id,
                    connected=h.connected,
                    valid=h.valid_updates,
                    rejected=h.rejected_updates,
                    reconnects=h.reconnect_count,
                    since_valid=round(h.seconds_since_valid, 3),
                    jitter=h.jitter_ema,
                )

    async def _reconnect_unhealthy(self) -> None:
        if self._closing:
            return
        now = time.time()
        while self._reconnect_times and now - self._reconnect_times[0] > 60:
            self._reconnect_times.popleft()
        if len(self._reconnect_times) >= self.max_reconnects_per_minute:
            return
        for conn in self.connections:
            if self._closing:
                return
            h = conn.health
            if (not h.connected) or h.seconds_since_valid > self.reconnect_stale_seconds:
                try:
                    await conn.reconnect()
                    if self._closing:
                        return
                    self._reconnect_times.append(time.time())
                    log.warning("poly_ws_reconnected", conn=conn.connection_id)
                except Exception as exc:
                    h.connected = False
                    h.rejected_updates += 1
                    log.error("poly_ws_reconnect_failed", conn=conn.connection_id, error=str(exc))
                if len(self._reconnect_times) >= self.max_reconnects_per_minute:
                    return
