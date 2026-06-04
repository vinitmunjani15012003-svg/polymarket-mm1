"""Pure relayer constants and request-building helpers."""

from __future__ import annotations

import json
from typing import Any

# Relayer proxy/deposit-wallet contract config on Polygon. These match the
# official relayer/deposit-wallet docs and current py-builder-relayer-client
# chain config.
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
RELAY_HUB = "0xD216153c06E857cD7f72665E0aF1d7D82172F494"
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
DEFAULT_PROXY_GAS_LIMIT = 500_000

DEPOSIT_WALLET_BATCH_TYPES = {
    "Call": [
        {"name": "target", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
    ],
    "Batch": [
        {"name": "wallet", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "calls", "type": "Call[]"},
    ],
}


def normalize_relayer_call(call: dict, checksum_address) -> dict:
    """Normalize a relayer call dictionary without changing transaction intent."""
    return {
        "target": checksum_address(call["target"]),
        "value": str(call.get("value", "0")),
        "data": call["data"],
    }


def deposit_wallet_domain(chain_id: int, wallet: str) -> dict:
    """Build DepositWallet EIP-712 domain data."""
    return {
        "name": "DepositWallet",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": wallet,
    }


def deposit_wallet_message(wallet: str, nonce: str | int, deadline: str | int, calls: list[dict]) -> dict:
    """Build DepositWallet EIP-712 message data."""
    return {
        "wallet": wallet,
        "nonce": int(nonce),
        "deadline": int(deadline),
        "calls": calls,
    }


def deposit_wallet_submit_payload(
    *,
    from_address: str,
    factory: str,
    wallet: str,
    nonce: str,
    deadline: str,
    calls: list[dict],
    signature: str,
    metadata: str = "",
) -> dict[str, Any]:
    """Build the raw `/submit` payload for deposit-wallet relayer calls."""
    payload = {
        "type": "WALLET",
        "from": from_address,
        "to": factory,
        "nonce": nonce,
        "signature": signature,
        "depositWalletParams": {
            "depositWallet": wallet,
            "deadline": deadline,
            "calls": calls,
        },
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def compact_json(payload: dict) -> str:
    """Serialize relayer payloads exactly as raw-submit auth expects."""
    return json.dumps(payload, separators=(",", ":"))
