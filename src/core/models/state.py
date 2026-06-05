"""Runtime state models for deterministic lifecycle decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LifecycleState(StrEnum):
    BOOT = "BOOT"
    DISCOVERING = "DISCOVERING"
    INITIALIZING = "INITIALIZING"
    QUOTING = "QUOTING"
    REPAIRING = "REPAIRING"
    WINDDOWN = "WINDDOWN"
    SETTLING = "SETTLING"
    RESETTING = "RESETTING"
    HALTED = "HALTED"


@dataclass(slots=True)
class FeedState:
    source: str = "unknown"
    age_seconds: float = float("inf")
    healthy: bool = False
    price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InventoryState:
    yes_shares: float = 0.0
    no_shares: float = 0.0
    matched_pairs: float = 0.0
    share_imbalance: float = 0.0
    source: str = "local"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionState:
    open_orders: int = 0
    pending_intents: int = 0
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketCycleState:
    market_id: str = ""
    asset: str = ""
    lifecycle: LifecycleState = LifecycleState.DISCOVERING
    quote_version: int = 0
    started_ts: float = 0.0
    updated_ts: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BotRuntimeState:
    lifecycle: LifecycleState = LifecycleState.BOOT
    markets: dict[str, MarketCycleState] = field(default_factory=dict)
    execution: ExecutionState = field(default_factory=ExecutionState)
    feed: FeedState = field(default_factory=FeedState)
    metadata: dict[str, Any] = field(default_factory=dict)
