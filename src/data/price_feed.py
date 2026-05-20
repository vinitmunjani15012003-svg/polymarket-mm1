"""
Binance WebSocket price feed for real-time crypto spot prices.

Connects to Binance's public WebSocket stream (no API key needed)
and maintains latest price + rolling data for each subscribed symbol.
"""

import asyncio
import json
import time
import math
import numpy as np
from collections import deque
from typing import Dict, Optional, Callable

import websockets

from src.monitoring.logger import get_logger

log = get_logger("price_feed")


class PriceFeed:
    """
    Real-time price feed from Binance WebSocket.
    Tracks latest price and computes rolling realized volatility.
    """

    def __init__(self, ws_url: str, symbols: list[str],
                 vol_lookback: int = 300):
        """
        Args:
            ws_url: Binance WebSocket base URL.
            symbols: List of symbols like ["BTCUSDT", "ETHUSDT"].
            vol_lookback: Number of 1s price samples for vol calculation.
        """
        self.ws_url = ws_url
        self.symbols = [s.lower() for s in symbols]
        self.vol_lookback = vol_lookback

        # Latest price per symbol (uppercase key)
        self.prices: Dict[str, float] = {}
        # Timestamp of latest price update
        self.timestamps: Dict[str, float] = {}
        # Rolling price history for vol calculation
        self._price_history: Dict[str, deque] = {
            s.upper(): deque(maxlen=vol_lookback) for s in self.symbols
        }
        # Callbacks for price updates
        self._callbacks: list[Callable] = []

        self._ws = None
        self._running = False
        self._reconnect_delay = 1
        self._last_message_ts: float = 0.0
        self._message_timeout: float = 15.0
        # Throttle: only record one price sample per second for vol calculation
        self._last_history_ts: Dict[str, float] = {}

    def on_price_update(self, callback: Callable):
        """Register a callback for price updates: callback(symbol, price, ts)."""
        self._callbacks.append(callback)

    def get_price(self, symbol: str) -> Optional[float]:
        """Get latest price for symbol (e.g., 'BTCUSDT')."""
        return self.prices.get(symbol.upper())

    def get_price_age(self, symbol: str) -> float:
        """Seconds since last price update for symbol."""
        ts = self.timestamps.get(symbol.upper())
        if ts is None:
            return float('inf')
        return time.time() - ts

    def realized_sigma_annualized(self, symbol: str) -> float:
        """
        Compute annualized realized volatility from rolling 1s price samples.
        Returns annualized sigma (e.g., 0.60 = 60%).
        """
        sym = symbol.upper()
        history = self._price_history.get(sym)
        if not history or len(history) < 30:
            return 0.60  # Default fallback

        prices = np.array(history)
        log_returns = np.diff(np.log(prices))

        if len(log_returns) < 10:
            return 0.60

        var_per_sec = np.var(log_returns)
        # Annualize: seconds in a year = 365.25 * 86400
        var_annual = var_per_sec * (365.25 * 86400)
        sigma = math.sqrt(max(1e-10, var_annual))

        # Clamp to reasonable range
        return max(0.10, min(3.0, sigma))

    async def start(self):
        """Start the WebSocket connection and begin streaming."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_stream()
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                log.warning("ws_disconnected", error=str(e),
                           reconnect_in=self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(30, self._reconnect_delay * 2)
            except Exception as e:
                log.error("ws_error", error=str(e))
                if not self._running:
                    break
                await asyncio.sleep(5)

    async def _connect_and_stream(self):
        """Connect to Binance combined stream and process messages."""
        # Build combined stream URL for all symbols using bookTicker for ultra-fast updates
        streams = "/".join(f"{s}@bookTicker" for s in self.symbols)
        
        base_url = self.ws_url
        if base_url.endswith("/ws"):
            base_url = base_url[:-3]
        elif base_url.endswith("/"):
            base_url = base_url[:-1]
            
        url = f"{base_url}/stream?streams={streams}"

        log.info("ws_connecting", url=url, symbols=self.symbols)

        async with websockets.connect(url, ping_interval=20,
                                       ping_timeout=10) as ws:
            self._ws = ws
            self._reconnect_delay = 1  # Reset on successful connect
            log.info("ws_connected", symbols=self.symbols)

            self._last_message_ts = time.time()

            while self._running:
                try:
                    # Application-level watchdog: bookTicker updates 10+ times a second.
                    # If we receive nothing for the configured timeout, the feed has silently stalled.
                    message = await asyncio.wait_for(
                        ws.recv(), timeout=self._message_timeout
                    )
                    data = json.loads(message)
                    self._process_trade(data)
                    self._last_message_ts = time.time()
                except asyncio.TimeoutError:
                    age = time.time() - self._last_message_ts
                    log.warning(
                        "ws_stall_detected",
                        timeout=self._message_timeout,
                        age=age,
                        msg="No messages received. Forcing reconnect.",
                    )
                    raise ConnectionError(f"No Binance messages for {age:.1f}s")
                except websockets.ConnectionClosed:
                    break
                except (json.JSONDecodeError, KeyError) as e:
                    log.debug("ws_parse_error", error=str(e))

    def _process_trade(self, data: dict):
        """Process a single trade event from Binance."""
        # Combined stream wraps data in {"stream": ..., "data": {...}}
        if "data" in data:
            data = data["data"]

        symbol = data.get("s", "").upper()  # Symbol
        
        # bookTicker uses 'b' (best bid) and 'a' (best ask)
        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))
        
        # Calculate mid price
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2.0
        else:
            # Fallback for trade stream if it happens
            price = float(data.get("p", 0))
        ts = time.time()

        if price <= 0 or not symbol:
            return

        self.prices[symbol] = price
        self.timestamps[symbol] = ts
        
        # Keep track of total ticks to prove it's alive
        self.ticks = getattr(self, 'ticks', 0) + 1

        # Throttle price history to ~1 sample/sec for vol calculation
        # bookTicker fires 50-100x/sec; recording every tick would
        # collapse realized vol to near-zero and break the BS model.
        last_hist = self._last_history_ts.get(symbol, 0)
        if ts - last_hist >= 1.0:
            self._price_history[symbol].append(price)
            self._last_history_ts[symbol] = ts

        # Fire callbacks
        for cb in self._callbacks:
            try:
                # pass ticks in callback if needed, or just let dashboard access it
                cb(symbol, price, ts)
            except Exception as e:
                log.error("price_callback_error", error=str(e))

    async def stop(self):
        """Stop the price feed."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        log.info("price_feed_stopped")

    async def fetch_price_rest(self, symbol: str,
                                rest_url: str) -> Optional[float]:
        """
        Fallback: fetch price via REST if WebSocket is down.
        Uses Binance public ticker endpoint (no auth needed).
        """
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{rest_url}/ticker/price",
                    params={"symbol": symbol.upper()},
                    timeout=5.0,
                )
                resp.raise_for_status()
                price = float(resp.json()["price"])
                self.prices[symbol.upper()] = price
                self.timestamps[symbol.upper()] = time.time()
                return price
        except Exception as e:
            log.error("rest_price_error", symbol=symbol, error=str(e))
            return None

    async def fetch_historical_price(self, symbol: str, timestamp: float,
                                      rest_url: str = "https://api.binance.com/api/v3"
                                      ) -> Optional[float]:
        """
        Fetch the price of an asset at a specific historical timestamp.
        
        Uses Binance klines (1-minute candles) to find the opening price
        of the candle that contains the given timestamp.
        
        This is critical for 15-minute binary markets: when the bot starts
        mid-window, we need the price at window open, not current spot.
        
        Args:
            symbol: Binance symbol (e.g., "BTCUSDT").
            timestamp: Unix timestamp to look up.
            rest_url: Binance REST API base URL.
            
        Returns:
            The opening price at that timestamp, or None on failure.
        """
        import httpx

        # Convert to milliseconds for Binance API
        # To get the previous 15-minute close, we fetch the 15m candle
        # that started exactly 15 minutes (900 seconds) before the target timestamp.
        prev_candle_start_ms = int((timestamp - 900) * 1000)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{rest_url}/klines",
                    params={
                        "symbol": symbol.upper(),
                        "interval": "15m",
                        "startTime": prev_candle_start_ms,
                        "limit": 1,
                    },
                    timeout=5.0,
                )
                resp.raise_for_status()
                data = resp.json()

                if data and len(data) > 0:
                    # Kline format: [open_time, open, high, low, close, ...]
                    # Index 4 is the close price
                    close_price = float(data[0][4])
                    log.info("historical_price_fetched",
                             symbol=symbol,
                             timestamp=round(timestamp, 0),
                             type="previous_15m_close",
                             price=close_price)
                    return close_price
                else:
                    log.warning("no_kline_data", symbol=symbol,
                                timestamp=round(timestamp, 0))
                    return None

        except Exception as e:
            log.error("historical_price_error", symbol=symbol, error=str(e))
            return None

    async def fetch_vatic_strike(self, symbol: str, timestamp: float) -> Optional[float]:
        """
        Fetch the exact "price to beat" (strike) from the Vatic Trading API.
        Polymarket uses Vatic to determine the start price for 15m crypto markets.
        
        Args:
            symbol: Asset symbol (e.g., "BTCUSDT").
            timestamp: Unix timestamp of the window start (eventStartTime).
            
        Returns:
            The exact strike price used by Polymarket, or None on failure.
        """
        import httpx
        
        # Extract base asset (e.g., BTCUSDT -> btc)
        asset = symbol.lower().replace("usdt", "").replace("usd", "")
        
        try:
            import asyncio
            async with httpx.AsyncClient() as client:
                for attempt in range(3):
                    try:
                        resp = await client.get(
                            "https://api.vatic.trading/api/v1/targets/timestamp",
                            params={
                                "asset": asset,
                                "type": "15min",
                                "timestamp": int(timestamp)
                            },
                            timeout=15.0,
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        
                        price = float(data.get("price", 0))
                        if price > 0:
                            log.info("vatic_strike_fetched",
                                     asset=asset,
                                     timestamp=int(timestamp),
                                     price=price)
                            return price
                    except httpx.HTTPError as e:
                        if attempt == 2:
                            raise e
                        await asyncio.sleep(1.0)
                return None
                
        except httpx.HTTPStatusError as e:
            # Usually means queried too early or invalid window
            log.warning("vatic_api_error", status=e.response.status_code, text=e.response.text)
            return None
        except Exception as e:
            log.error("vatic_strike_error", asset=asset, error=str(e))
            return None

    async def fetch_chainlink_price(self, symbol: str, target_ts: float,
                                     rpc_url: str = "https://polygon-mainnet.g.alchemy.com/v2/demo"
                                     ) -> Optional[float]:
        """
        Fetch the Chainlink price at a specific timestamp.
        
        This is THE resolution source for Polymarket 15-min markets.
        Reads the on-chain Chainlink aggregator on Polygon and searches
        backward through rounds to find the price at target_ts.
        
        Args:
            symbol: Asset symbol (BTCUSDT → BTC/USD feed).
            target_ts: Unix timestamp to look up.
            rpc_url: Polygon RPC endpoint.
            
        Returns:
            Price at that timestamp, or None on failure.
        """
        # Chainlink Price Feed addresses on Polygon
        FEEDS = {
            "BTCUSDT": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
            "ETHUSDT": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
            "SOLUSDT": "0x10C8264C0935b3B9870013e36ce8f4DEB2db4264",
            "XRPUSDT": "0x785ba89291f676b5386652eB12b30cF361020694",
        }
        
        feed_addr = FEEDS.get(symbol.upper())
        if not feed_addr:
            log.warning("no_chainlink_feed", symbol=symbol)
            return None

        ABI = [
            {
                "name": "latestRoundData",
                "type": "function",
                "inputs": [],
                "outputs": [
                    {"name": "roundId", "type": "uint80"},
                    {"name": "answer", "type": "int256"},
                    {"name": "startedAt", "type": "uint256"},
                    {"name": "updatedAt", "type": "uint256"},
                    {"name": "answeredInRound", "type": "uint80"},
                ],
            },
            {
                "name": "getRoundData",
                "type": "function",
                "inputs": [{"name": "_roundId", "type": "uint80"}],
                "outputs": [
                    {"name": "roundId", "type": "uint80"},
                    {"name": "answer", "type": "int256"},
                    {"name": "startedAt", "type": "uint256"},
                    {"name": "updatedAt", "type": "uint256"},
                    {"name": "answeredInRound", "type": "uint80"},
                ],
            },
            {
                "name": "decimals",
                "type": "function",
                "inputs": [],
                "outputs": [{"name": "", "type": "uint8"}],
            },
        ]

        try:
            from web3 import Web3
            
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not w3.is_connected():
                log.warning("chainlink_rpc_down", rpc=rpc_url)
                return None
            
            feed = w3.eth.contract(
                address=Web3.to_checksum_address(feed_addr),
                abi=ABI,
            )
            
            decimals = feed.functions.decimals().call()
            latest = feed.functions.latestRoundData().call()
            latest_round_id = latest[0]
            
            target_ts_int = int(target_ts)
            
            # Polymarket resolves using the Chainlink price AT or AFTER
            # the window start (eventStartTime). So we need the FIRST
            # round whose updatedAt >= target_ts.
            #
            # Strategy: search backward from latest to find the last
            # round BEFORE target, then the one AFTER it is our answer.
            
            # Step 1: Walk backward to find the boundary
            round_id = latest_round_id
            first_after = None     # First round at or after target_ts
            last_before = None     # Last round before target_ts
            
            for step in range(80):  # Max 80 rounds back
                try:
                    data = feed.functions.getRoundData(round_id).call()
                    r_answer = data[1]
                    r_updated = data[3]
                    
                    if r_updated >= target_ts_int:
                        # This round is at or after our target — candidate
                        first_after = (round_id, r_answer, r_updated)
                        round_id -= 1  # Keep going backward
                    else:
                        # This round is before target — we've found the boundary
                        last_before = (round_id, r_answer, r_updated)
                        break
                    
                except Exception:
                    round_id -= 1
                    continue
            
            # Use the first round at or after target_ts
            if first_after:
                _, answer, ts = first_after
                price = answer / (10 ** decimals)
                log.info("chainlink_price_fetched",
                         symbol=symbol,
                         target_ts=target_ts_int,
                         price_ts=ts,
                         price=price,
                         offset_seconds=ts - target_ts_int)
                return price
            else:
                log.warning("chainlink_no_round_found",
                            symbol=symbol, target_ts=target_ts_int)
                return None

        except ImportError:
            log.warning("web3_not_installed",
                        msg="Chainlink feed requires web3: pip install web3")
            return None
        except Exception as e:
            log.error("chainlink_error", symbol=symbol, error=str(e))
            return None

