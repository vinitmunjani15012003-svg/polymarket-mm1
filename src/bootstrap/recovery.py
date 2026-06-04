"""Restart/recovery workflow boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StartupRecoverySummary:
    open_orders: Any = None
    positions: Any = None
    cancelled_stale: bool = False
    state_loaded: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_orders": self.open_orders,
            "positions": self.positions,
            "cancelled_stale": self.cancelled_stale,
            "state_loaded": self.state_loaded,
            **self.metadata,
        }


async def reconcile_on_startup(executor=None, state_manager=None) -> dict:
    """Fetch exchange state and return a structured recovery summary.

    Existing startup code still owns the concrete sequence; this helper is the
    new architecture seam for moving recovery out of main.py incrementally.
    """
    summary = StartupRecoverySummary()
    if executor and hasattr(executor, "open_orders"):
        summary.open_orders = getattr(executor, "open_orders")
    if executor and hasattr(executor, "positions"):
        summary.positions = getattr(executor, "positions")
    if state_manager:
        summary.state_loaded = True
    return summary.as_dict()
