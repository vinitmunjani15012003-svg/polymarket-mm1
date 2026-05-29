"""Minimal MT5 spot bridge for Exness prices.

Run on the Windows host where MetaTrader 5 is already logged in.
Env:
  MT5_BRIDGE_HOST=0.0.0.0
  MT5_BRIDGE_PORT=8765
  MT5_BRIDGE_API_KEY=<optional>
  MT5_DEFAULT_SYMBOL=BTCUSD
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import MetaTrader5 as mt5

HOST = os.environ.get("MT5_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("MT5_BRIDGE_PORT", "8765"))
API_KEY = os.environ.get("MT5_BRIDGE_API_KEY", "")
DEFAULT_SYMBOL = os.environ.get("MT5_DEFAULT_SYMBOL", "BTCUSD")


def ensure_mt5() -> bool:
    if mt5.terminal_info() is not None:
        return True
    return bool(mt5.initialize())


def tick_payload(symbol: str) -> dict:
    if not ensure_mt5():
        raise RuntimeError(f"mt5_initialize_failed: {mt5.last_error()}")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select_failed: {symbol}: {mt5.last_error()}")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or (not tick.bid and not tick.ask and not tick.last):
        raise RuntimeError(f"no_tick: {symbol}: {mt5.last_error()}")
    bid = float(tick.bid or tick.last or tick.ask)
    ask = float(tick.ask or tick.last or tick.bid)
    mid = (bid + ask) / 2.0
    ts = (float(tick.time_msc) / 1000.0) if getattr(tick, "time_msc", 0) else time.time()
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "price": mid,
        "ts": ts,
        "age": max(0.0, time.time() - ts),
        "source": "exness_mt5",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MT5Bridge/1.0"

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if API_KEY and self.headers.get("X-API-Key") != API_KEY:
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        path = urlparse(self.path).path.strip("/")
        try:
            if path in ("health", ""):
                self._send(200, {"ok": ensure_mt5(), "source": "exness_mt5"})
                return
            if path.startswith("price/"):
                symbol = unquote(path.split("/", 1)[1] or DEFAULT_SYMBOL).upper()
                self._send(200, {"ok": True, **tick_payload(symbol)})
                return
            self._send(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            self._send(503, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ensure_mt5()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MT5 bridge listening on {HOST}:{PORT}, default symbol={DEFAULT_SYMBOL}", flush=True)
    httpd.serve_forever()
