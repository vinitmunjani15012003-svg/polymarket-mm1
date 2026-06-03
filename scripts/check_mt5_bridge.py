"""Check MT5 bridge reachability without printing secrets.

Usage:
  MT5_BRIDGE_URL=http://host:8765 MT5_BRIDGE_API_KEY=... python scripts/check_mt5_bridge.py
  python scripts/check_mt5_bridge.py --url http://host:8765 --symbol BTCUSD

Exit codes:
  0 reachable and price endpoint OK
  1 bridge reachable but returned non-OK/auth/MT5 error
  2 network/TCP/timeout unreachable
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from urllib.parse import urlparse

import httpx


def _safe_json(payload: dict) -> str:
    if "api_key" in payload:
        payload = {**payload, "api_key": "<redacted>"}
    return json.dumps(payload, separators=(",", ":"))


def _tcp_probe(url: str, timeout: float) -> tuple[bool, str]:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False, "missing_host"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "tcp_open"
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__


def main() -> int:
    parser = argparse.ArgumentParser(description="Check MT5 bridge health/price reachability")
    parser.add_argument("--url", default=os.environ.get("MT5_BRIDGE_URL", ""), help="MT5 bridge base URL")
    parser.add_argument("--api-key", default=os.environ.get("MT5_BRIDGE_API_KEY", ""), help=argparse.SUPPRESS)
    parser.add_argument("--symbol", default=os.environ.get("MT5_DEFAULT_SYMBOL", "BTCUSD"))
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    base_url = (args.url or "").rstrip("/")
    if not base_url:
        print(_safe_json({"ok": False, "stage": "config", "error": "MT5_BRIDGE_URL missing"}))
        return 1

    tcp_ok, tcp_detail = _tcp_probe(base_url, args.timeout)
    parsed = urlparse(base_url)
    host = parsed.netloc or parsed.path
    if not tcp_ok:
        print(_safe_json({"ok": False, "stage": "tcp", "host": host, "error": tcp_detail}))
        return 2

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    try:
        with httpx.Client(timeout=args.timeout) as client:
            health = client.get(f"{base_url}/health", headers=headers)
            price = client.get(f"{base_url}/price/{args.symbol.upper()}", headers=headers)
    except httpx.HTTPError as exc:
        print(_safe_json({"ok": False, "stage": "http", "host": host, "error_type": exc.__class__.__name__, "error": str(exc) or exc.__class__.__name__}))
        return 2

    def body(resp: httpx.Response):
        try:
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                data["error"] = str(data["error"])[:200]
            return data
        except ValueError:
            return {"text": resp.text[:200]}

    result = {
        "ok": health.is_success and price.is_success,
        "host": host,
        "health_status": health.status_code,
        "health": body(health),
        "price_status": price.status_code,
        "price": body(price),
    }
    print(_safe_json(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
