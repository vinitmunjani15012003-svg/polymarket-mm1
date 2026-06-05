"""Decision/result models shared by strategy services.

These models are intentionally small and serializable so every service can
explain why it allowed, blocked, or transformed a trading action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


DecisionAction = Literal[
    "QUOTE",
    "CANCEL",
    "CANCEL_SIDE",
    "REPAIR",
    "REDUCE_SIZE",
    "HOLD",
    "STOP",
    "SETTLE",
    "HALT",
]


@dataclass(slots=True)
class DecisionResult:
    allowed: bool
    action: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, action: str = "QUOTE", reason: str = "OK", **metadata: Any) -> "DecisionResult":
        return cls(True, action, reason, metadata)

    @classmethod
    def block(cls, action: str = "HOLD", reason: str = "BLOCKED", **metadata: Any) -> "DecisionResult":
        return cls(False, action, reason, metadata)


@dataclass(slots=True)
class RiskDecision:
    action: str
    reason: str
    severity: str = "info"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QuotePlan:
    bid_price: float | None = None
    ask_price: float | None = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    action: str = "HOLD"
    metadata: dict[str, Any] = field(default_factory=dict)
