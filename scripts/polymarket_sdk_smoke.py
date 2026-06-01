#!/usr/bin/env python3
"""Smoke test Polymarket's unified Python SDK.

Safe by default:
- reads secrets from env or an ignored local YAML config
- never prints private/API secret values
- only performs public reads + authenticated readiness checks unless you pass
  explicit live-action flags

Example:
    POLYMARKET_PRIVATE_KEY=0x... \
    POLYMARKET_WALLET_ADDRESS=0x... \
    python scripts/polymarket_sdk_smoke.py

Using local config:
    python scripts/polymarket_sdk_smoke.py --config config/live_20_config.yaml

Live/idempotent setup actions:
    python scripts/polymarket_sdk_smoke.py --config config/live_20_config.yaml \
      --setup-gasless --setup-approvals --yes-i-understand-live-actions
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass
class SmokeConfig:
    private_key: str | None = None
    wallet: str | None = None
    relayer_api_key: str | None = None
    relayer_api_key_address: str | None = None
    builder_api_key: str | None = None
    builder_secret: str | None = None
    builder_passphrase: str | None = None


def _pick(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def load_smoke_config(path: str | None) -> SmokeConfig:
    file_cfg: dict[str, Any] = {}
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        creds = raw.get("credentials", {}) or {}
        pm = creds.get("polymarket", {}) or {}
        builder = creds.get("builder", {}) or {}
        file_cfg = {
            "private_key": pm.get("private_key"),
            # For existing bot configs this is the Polymarket deposit wallet.
            # Override with POLYMARKET_WALLET_ADDRESS if needed.
            "wallet": pm.get("funder"),
            "relayer_api_key": builder.get("relayer_api_key"),
            "relayer_api_key_address": builder.get("relayer_api_key_address"),
            "builder_api_key": builder.get("api_key"),
            "builder_secret": builder.get("secret"),
            "builder_passphrase": builder.get("passphrase"),
        }

    return SmokeConfig(
        private_key=_pick(os.getenv("POLYMARKET_PRIVATE_KEY"), file_cfg.get("private_key")),
        wallet=_pick(os.getenv("POLYMARKET_WALLET_ADDRESS"), file_cfg.get("wallet")),
        relayer_api_key=_pick(os.getenv("POLYMARKET_RELAYER_API_KEY"), file_cfg.get("relayer_api_key")),
        relayer_api_key_address=_pick(
            os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS"),
            file_cfg.get("relayer_api_key_address"),
        ),
        builder_api_key=_pick(os.getenv("POLYMARKET_BUILDER_API_KEY"), file_cfg.get("builder_api_key")),
        builder_secret=_pick(os.getenv("POLYMARKET_BUILDER_SECRET"), file_cfg.get("builder_secret")),
        builder_passphrase=_pick(
            os.getenv("POLYMARKET_BUILDER_PASSPHRASE"),
            file_cfg.get("builder_passphrase"),
        ),
    )


def build_api_key(cfg: SmokeConfig) -> Any | None:
    try:
        from polymarket import BuilderApiKey, RelayerApiKey
    except ImportError as exc:  # pragma: no cover - exercised before runtime
        raise SystemExit(
            "Missing SDK dependency. Install with: pip install polymarket-client"
        ) from exc

    if cfg.relayer_api_key and cfg.relayer_api_key_address:
        return RelayerApiKey(key=cfg.relayer_api_key, address=cfg.relayer_api_key_address)

    if cfg.builder_api_key and cfg.builder_secret and cfg.builder_passphrase:
        return BuilderApiKey(
            key=cfg.builder_api_key,
            secret=cfg.builder_secret,
            passphrase=cfg.builder_passphrase,
        )

    return None


async def run(args: argparse.Namespace) -> int:
    try:
        from polymarket import AsyncPublicClient, AsyncSecureClient, PolymarketError
    except ImportError:
        print("Missing SDK dependency. Install with: pip install polymarket-client", file=sys.stderr)
        return 2

    cfg = load_smoke_config(args.config)

    print("[1/3] Public SDK check: list one open market")
    async with AsyncPublicClient() as public_client:
        markets = public_client.list_markets(closed=False, page_size=1)
        first_page = await markets.first_page()
        print(f"      open_markets_returned={len(first_page.items)}")
        if first_page.items:
            market = first_page.items[0]
            print(f"      sample_market_id={getattr(market, 'id', None)}")
            print(f"      sample_question={getattr(market, 'question', None)!r}")

    if args.public_only:
        print("[done] public-only smoke passed")
        return 0

    if not cfg.private_key:
        print(
            "Missing private key. Set POLYMARKET_PRIVATE_KEY or pass --config pointing to an ignored local config.",
            file=sys.stderr,
        )
        return 2

    live_action_requested = args.setup_gasless or args.setup_approvals
    if live_action_requested and not args.yes_i_understand_live_actions:
        print(
            "Refusing live setup action without --yes-i-understand-live-actions. "
            "These calls may submit transactions/approvals.",
            file=sys.stderr,
        )
        return 2

    api_key = build_api_key(cfg)
    print("[2/3] Secure SDK check: create authenticated client")
    print(f"      wallet_address_configured={bool(cfg.wallet)}")
    print(f"      gasless_api_key_configured={bool(api_key)}")

    secure_client = await AsyncSecureClient.create(
        private_key=cfg.private_key,
        wallet=cfg.wallet,
        api_key=api_key,
    )
    try:
        print("[3/3] Gasless readiness check")
        ready = await secure_client.is_gasless_ready()
        print(f"      is_gasless_ready={ready}")

        if args.setup_gasless:
            print("[live] setup_gasless_wallet()")
            gasless_client = await secure_client.setup_gasless_wallet()
            await secure_client.close()
            secure_client = gasless_client
            print("      gasless wallet setup returned client")

        if args.setup_approvals:
            print("[live] setup_trading_approvals()")
            handle = await secure_client.setup_trading_approvals()
            outcome = await handle.wait()
            print(f"      approval_transaction_hash={getattr(outcome, 'transaction_hash', None)}")

    except PolymarketError as exc:
        print(f"Polymarket SDK error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await secure_client.close()

    print("[done] SDK smoke completed")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Ignored local YAML config to read secrets from")
    parser.add_argument("--public-only", action="store_true", help="Only test public market data")
    parser.add_argument("--setup-gasless", action="store_true", help="Run setup_gasless_wallet()")
    parser.add_argument("--setup-approvals", action="store_true", help="Run setup_trading_approvals() and wait")
    parser.add_argument(
        "--yes-i-understand-live-actions",
        action="store_true",
        help="Required with setup flags because they may submit live transactions/approvals",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
