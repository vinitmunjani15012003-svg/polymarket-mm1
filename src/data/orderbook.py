"""
Order book reader for Polymarket CLOB.

Fetches order book snapshots and computes micro-price.
"""

import asyncio
import time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

import httpx

from src.monitoring.logger import get_logger

log = get_logger("orderbook")


@dataclass
class BookSnapshot:
    """Snapshot of an order book."""
    token_id: str
    timestamp: float
    bids: list  # [(price, size), ...] sorted desc by price
    asks: list  # [(price, size), ...] sorted asc by price
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    mid_price: float
    micro_price: float


class OrderBookReader:
    """
    Reads order book snapshots from Polymarket CLOB API.
    Public endpoint — no authentication needed.
    """

    def __init__(self, host: str = "https://clob.polymarket.com"):
        self.host = host
        self._client = httpx.AsyncClient(timeout=5.0)

    async def get_book(self, token_id: str) -> Optional[BookSnapshot]:
        """
        Fetch the current order book for a token.

        Args:
            token_id: The CLOB token ID (YES or NO token).

        Returns:
            BookSnapshot or None on error.
        """
        try:
            resp = await self._client.get(
                f"{self.host}/book",
                params={"token_id": token_id}
            )
            resp.raise_for_status()
            raw = resp.json()
            return self._parse_book(token_id, raw)
        except Exception as e:
            log.error("book_fetch_error", token_id=token_id, error=str(e))
            return None

    async def get_books(self, token_ids: list[str]) -> dict[str, Optional[BookSnapshot]]:
        """Fetch multiple order books in one CLOB /books request."""
        if not token_ids:
            return {}
        try:
            resp = await self._client.post(
                f"{self.host}/books",
                json=[{"token_id": token_id} for token_id in token_ids],
            )
            resp.raise_for_status()
            raw_books = resp.json()

            books: dict[str, Optional[BookSnapshot]] = {token_id: None for token_id in token_ids}
            for token_id, raw in zip(token_ids, raw_books):
                books[token_id] = self._parse_book(token_id, raw)
            return books
        except Exception as e:
            log.error("books_fetch_error", count=len(token_ids), error=str(e))
            # Safe fallback: parallel single-book requests.
            results = await asyncio.gather(
                *(self.get_book(token_id) for token_id in token_ids),
                return_exceptions=True,
            )
            return {
                token_id: (book if isinstance(book, BookSnapshot) else None)
                for token_id, book in zip(token_ids, results)
            }

    def _parse_book(self, token_id: str, raw: dict) -> BookSnapshot:
        """Parse raw API response into BookSnapshot."""
        bids = []
        asks = []

        for b in raw.get("bids", []):
            price = float(b.get("price", 0))
            size = float(b.get("size", 0))
            if price > 0 and size > 0:
                bids.append((price, size))

        for a in raw.get("asks", []):
            price = float(a.get("price", 0))
            size = float(a.get("size", 0))
            if price > 0 and size > 0:
                asks.append((price, size))

        # Sort: bids descending, asks ascending
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        best_bid = bids[0][0] if bids else 0.01
        best_ask = asks[0][0] if asks else 0.99
        best_bid_size = bids[0][1] if bids else 0
        best_ask_size = asks[0][1] if asks else 0

        mid = (best_bid + best_ask) / 2

        # Micro-price: size-weighted mid
        if best_bid_size + best_ask_size > 0:
            micro = (best_bid * best_ask_size + best_ask * best_bid_size) / \
                    (best_bid_size + best_ask_size)
        else:
            micro = mid

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
        )

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
