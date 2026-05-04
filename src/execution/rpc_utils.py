"""Shared RPC utilities.

We keep RPC selection logic here because public endpoints change reliability
frequently. Both balance monitoring and on-chain CTF ops need a working RPC.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple


def pick_working_polygon_rpc(Web3, candidates: Iterable[str]) -> Tuple[Optional["Web3"], Optional[str], Optional[Exception]]:
    """Return (w3, rpc, last_error) for the first RPC that responds."""
    last_err: Optional[Exception] = None
    for rpc in candidates:
        if not rpc:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            _ = w3.eth.block_number  # connectivity test
            return w3, rpc, None
        except Exception as e:  # pragma: no cover
            last_err = e
            continue
    return None, None, last_err

