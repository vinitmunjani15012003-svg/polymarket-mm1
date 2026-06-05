"""Order intent and order state models.

OrderIntent is the idempotency boundary: a quote version should map to stable
intent ids so retries/timeouts do not create duplicate live orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import hashlib


OrderSide = Literal["yes", "no"]
OrderIntentAction = Literal["PLACE", "CANCEL", "REPLACE", "HOLD"]


@dataclass(slots=True, frozen=True)
class OrderIntent:
    market_id: str
    quote_version: int
    side: OrderSide
    action: OrderIntentAction = "PLACE"
    price: float | None = None
    size: float = 0.0
    token_id: str = ""
    metadata: tuple[tuple[str, Any], ...] = ()

    @property
    def intent_id(self) -> str:
        raw = f"{self.market_id}:{self.quote_version}:{self.side}:{self.action}:{self.price}:{self.size}:{self.token_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(slots=True)
class OrderState:
    order_id: str = ""
    intent_id: str = ""
    market_id: str = ""
    side: str = ""
    price: float | None = None
    size: float = 0.0
    status: str = "unknown"
    updated_ts: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
