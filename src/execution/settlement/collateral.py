"""Collateral inference helpers for Polymarket CTF settlement."""

from __future__ import annotations

from src.monitoring.logger import get_logger
from src.execution.settlement.contracts import (
    BINARY_PARTITION,
    CTF_COLLATERAL_ADAPTER,
    DEFAULT_COLLATERAL_TOKEN,
    PARENT_COLLECTION_ID,
    USDC_E_COLLATERAL_TOKEN,
)

log = get_logger("ctf_ops")

def infer_collateral_token_for_market(w3, ctf, condition_id: str,
                                      yes_token_id: str = "",
                                      no_token_id: str = "",
                                      default_collateral: str = DEFAULT_COLLATERAL_TOKEN) -> str:
    """
    Infer the collateral token used to create a market's CTF position IDs.

    Important: the ERC1155 token id encodes the collateral address. If we call
    mergePositions with pUSD while the market's token ids were derived from
    USDC.e, the relayer batch is valid but the CTF call reverts because the
    wallet owns zero pUSD-derived positions. This is exactly the annoying kind
    of bug that looks like a relayer problem while being a collateral mismatch.
    """
    expected = {str(yes_token_id or ""), str(no_token_id or "")}
    expected.discard("")
    if len(expected) < 2 or not w3 or not ctf or not condition_id:
        return default_collateral or DEFAULT_COLLATERAL_TOKEN

    try:
        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        collection_ids = [
            ctf.functions.getCollectionId(PARENT_COLLECTION_ID, condition_bytes, idx).call()
            for idx in BINARY_PARTITION
        ]
        candidates = [
            default_collateral or DEFAULT_COLLATERAL_TOKEN,
            USDC_E_COLLATERAL_TOKEN,
            CTF_COLLATERAL_ADAPTER,
            DEFAULT_COLLATERAL_TOKEN,
        ]
        seen = set()
        for collateral in candidates:
            if not collateral or collateral.lower() in seen:
                continue
            seen.add(collateral.lower())
            derived = {
                str(ctf.functions.getPositionId(
                    w3.to_checksum_address(collateral), collection_id
                ).call())
                for collection_id in collection_ids
            }
            if derived == expected:
                return w3.to_checksum_address(collateral)
    except Exception as e:
        log.warning("collateral_inference_failed",
                    condition=condition_id[:12], error=str(e))

    return default_collateral or DEFAULT_COLLATERAL_TOKEN
