"""
Market discovery for Polymarket 15-minute crypto binary markets.

Slug format: {asset}-updown-15m-{window_start_timestamp}
where window_start = floor(now / 900) * 900

Outcomes are "Up" / "Down" (not YES/NO).
Token order: [Up_token, Down_token]
"""

import json
import time
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from src.monitoring.logger import get_logger

log = get_logger("market_discovery")

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Asset slug prefixes used by Polymarket
ASSET_SLUG_PREFIX = {
    "BTC": "btc",
    "ETH": "eth",
    "SOL": "sol",
    "XRP": "xrp",
}


@dataclass
class MarketInfo:
    """Metadata for a single 15-minute binary crypto market."""
    market_id: str           # Condition ID (hex)
    condition_id: str        # Same as market_id (for CLOB)
    token_id_up: str         # "Up" token ID (first clobTokenId)
    token_id_down: str       # "Down" token ID (second clobTokenId)
    question: str            # Human-readable question
    asset: str               # BTC, ETH, SOL, XRP
    slug: str                # e.g., "btc-updown-15m-1777006800"
    window_start_ts: float   # Unix timestamp of 15-min window start
    resolve_ts: float        # Unix timestamp of resolution (endDate)
    event_start_ts: float    # Unix timestamp of eventStartTime
    outcomes: list           # ["Up", "Down"]
    current_prices: list     # [up_price, down_price]
    order_min_size: int      # Minimum order size (typically 5)
    active: bool
    best_bid: float = 0.0    # Best bid for Up token (from CLOB)
    best_ask: float = 0.0    # Best ask for Up token (from CLOB)

    @property
    def time_remaining(self) -> float:
        """Seconds until resolution."""
        return max(0, self.resolve_ts - time.time())

    @property
    def total_duration(self) -> float:
        """Total market duration in seconds (900 for 15-min)."""
        return max(1, self.resolve_ts - self.event_start_ts)

    @property
    def up_price(self) -> float:
        """Current Up outcome price.

        Gamma's `outcomes` ordering is not guaranteed; do not assume
        current_prices[0] is always Up.
        """
        return self._price_for("up")

    @property
    def down_price(self) -> float:
        """Current Down outcome price.

        Gamma's `outcomes` ordering is not guaranteed; do not assume
        current_prices[1] is always Down.
        """
        return self._price_for("down")

    def _price_for(self, outcome: str) -> float:
        """Return price for the requested outcome label (case-insensitive).

        Falls back to the positional assumption when outcomes/prices are
        missing or inconsistent.
        """
        try:
            if not self.current_prices:
                return 0.5
            if not self.outcomes or len(self.outcomes) != len(self.current_prices):
                # Legacy/fallback assumption: [Up, Down]
                if outcome.lower() == "up":
                    return self.current_prices[0]
                if outcome.lower() == "down":
                    return self.current_prices[1] if len(self.current_prices) > 1 else 0.5
                return 0.5

            want = outcome.lower()
            for i, name in enumerate(self.outcomes):
                if str(name).strip().lower() == want:
                    return float(self.current_prices[i])

            # If Gamma uses YES/NO, try mapping: YES=Up, NO=Down (our bot convention)
            if want == "up":
                for i, name in enumerate(self.outcomes):
                    if str(name).strip().lower() == "yes":
                        return float(self.current_prices[i])
            if want == "down":
                for i, name in enumerate(self.outcomes):
                    if str(name).strip().lower() == "no":
                        return float(self.current_prices[i])

        except Exception:
            pass

        # Last-resort fallback
        if outcome.lower() == "up":
            return self.current_prices[0] if self.current_prices else 0.5
        if outcome.lower() == "down":
            return self.current_prices[1] if self.current_prices and len(self.current_prices) > 1 else 0.5
        return 0.5

    @property
    def market_mid_up(self) -> float:
        """Mid-price for Up token from CLOB order book.
        More accurate than outcomePrices (which is last trade)."""
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        return self.up_price


class MarketDiscovery:
    """
    Discovers active 15-minute binary crypto markets on Polymarket.

    Uses deterministic slug computation:
      slug = {asset}-updown-15m-{floor(now/900)*900}

    Then fetches metadata from Gamma API (public, no auth needed).
    """

    def __init__(self, assets: List[str] = None):
        self.assets = assets or ["BTC", "ETH", "SOL", "XRP"]
        self._client = httpx.AsyncClient(timeout=10.0)
        self._known_markets: Dict[str, MarketInfo] = {}

    @staticmethod
    def compute_window_start(ts: float = None) -> int:
        """Compute the 15-minute window start timestamp."""
        ts = ts or time.time()
        return (int(ts) // 900) * 900

    @staticmethod
    def compute_slug(asset: str, window_start: int = None) -> str:
        """Compute the Gamma API slug for a market."""
        if window_start is None:
            window_start = MarketDiscovery.compute_window_start()
        prefix = ASSET_SLUG_PREFIX.get(asset.upper(), asset.lower())
        return f"{prefix}-updown-15m-{window_start}"

    async def discover_markets(self,
                                min_remaining_seconds: int = 120
                                ) -> List[MarketInfo]:
        """
        Discover active 15-minute markets for all configured assets.

        Computes the current window slug for each asset and fetches
        market data from the Gamma API.
        """
        markets = []
        now = time.time()
        window_start = self.compute_window_start(now)

        for asset in self.assets:
            market = await self._fetch_market(asset, window_start)
            if market and market.time_remaining >= min_remaining_seconds:
                markets.append(market)
                self._known_markets[market.market_id] = market
            elif market and market.time_remaining < min_remaining_seconds:
                # Current window is almost done, try next window
                next_window = window_start + 900
                next_market = await self._fetch_market(asset, next_window)
                if next_market and next_market.time_remaining >= min_remaining_seconds:
                    markets.append(next_market)
                    self._known_markets[next_market.market_id] = next_market

        if markets:
            log.info("markets_discovered",
                     count=len(markets),
                     assets=[m.asset for m in markets])

        return markets

    async def discover_single(self, asset: str,
                               min_remaining: int = 120) -> Optional[MarketInfo]:
        """Discover the active market for a single asset."""
        now = time.time()
        window_start = self.compute_window_start(now)

        market = await self._fetch_market(asset, window_start)
        if market and market.time_remaining >= min_remaining:
            self._known_markets[market.market_id] = market
            return market

        # Try next window
        next_window = window_start + 900
        market = await self._fetch_market(asset, next_window)
        if market and market.time_remaining >= min_remaining:
            self._known_markets[market.market_id] = market
            return market

        return None

    async def _fetch_market(self, asset: str,
                             window_start: int) -> Optional[MarketInfo]:
        """Fetch a single market by computed slug."""
        slug = self.compute_slug(asset, window_start)

        try:
            params = {
                "slug": slug,
                "_t": int(time.time())  # Cache-buster
            }
            resp = await self._client.get(
                f"{GAMMA_API_URL}/markets",
                params=params
            )
            resp.raise_for_status()
            data = resp.json()

            if not data:
                return None

            raw = data[0] if isinstance(data, list) else data
            return self._parse_market(raw, asset, slug, window_start)

        except httpx.HTTPError as e:
            log.error("gamma_fetch_error", slug=slug, error=str(e))
            return None
        except Exception as e:
            log.error("market_parse_error", slug=slug, error=str(e))
            return None

    def _parse_market(self, raw: dict, asset: str,
                      slug: str, window_start: int) -> Optional[MarketInfo]:
        """Parse raw Gamma API response into MarketInfo."""
        try:
            condition_id = raw.get("conditionId", "")
            if not condition_id:
                return None

            # Parse token IDs (stored as JSON string)
            clob_ids_raw = raw.get("clobTokenIds", "[]")
            if isinstance(clob_ids_raw, str):
                clob_ids = json.loads(clob_ids_raw)
            else:
                clob_ids = clob_ids_raw

            if len(clob_ids) < 2:
                log.warning("insufficient_tokens", slug=slug)
                return None

            # Parse timestamps
            end_date = raw.get("endDate", "")
            event_start = raw.get("eventStartTime", "")

            resolve_ts = self._parse_iso(end_date)
            event_start_ts = self._parse_iso(event_start)

            if not resolve_ts:
                return None
            if not event_start_ts:
                # Fallback: window_start as event start
                event_start_ts = float(window_start)

            # Parse outcome prices
            prices_raw = raw.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                prices = [float(p) for p in json.loads(prices_raw)]
            else:
                prices = [float(p) for p in prices_raw]

            # Parse outcomes
            outcomes_raw = raw.get("outcomes", '["Up", "Down"]')
            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw

            return MarketInfo(
                market_id=condition_id,
                condition_id=condition_id,
                token_id_up=clob_ids[0],     # First token = Up
                token_id_down=clob_ids[1],   # Second token = Down
                question=raw.get("question", ""),
                asset=asset.upper(),
                slug=slug,
                window_start_ts=float(window_start),
                resolve_ts=resolve_ts,
                event_start_ts=event_start_ts,
                outcomes=outcomes,
                current_prices=prices,
                order_min_size=raw.get("orderMinSize", 5),
                active=raw.get("active", True),
                best_bid=float(raw.get("bestBid", 0) or 0),
                best_ask=float(raw.get("bestAsk", 0) or 0),
            )
        except Exception as e:
            log.error("parse_error", slug=slug, error=str(e))
            return None

    def _parse_iso(self, iso_str) -> Optional[float]:
        """Parse ISO datetime string to Unix timestamp."""
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    def get_markets_for_asset(self, asset: str) -> List[MarketInfo]:
        """Get cached markets for a specific asset."""
        now = time.time()
        return [
            m for m in self._known_markets.values()
            if m.asset == asset.upper() and m.resolve_ts > now + 30
        ]

    async def close(self):
        await self._client.aclose()
