"""Restart/recovery workflow boundary.

The functions here preserve the live startup order from ``main.py`` while making
that order explicit and testable: reconcile exchange state, then cancel stale
orders.  No concrete client classes are imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StartupRecoverySummary:
    open_orders: Any = None
    positions: Any = None
    cancelled_stale: bool = False
    state_loaded: bool = False
    reconciled: bool = False
    steps: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_orders": self.open_orders,
            "positions": self.positions,
            "cancelled_stale": self.cancelled_stale,
            "state_loaded": self.state_loaded,
            "reconciled": self.reconciled,
            "steps": list(self.steps),
            **self.metadata,
        }


def summarize_recovery_state(executor: Any = None, state_manager: Any = None, **metadata: Any) -> StartupRecoverySummary:
    """Create a side-effect-free recovery summary from already-known objects."""
    summary = StartupRecoverySummary(metadata=dict(metadata))
    if executor and hasattr(executor, "open_orders"):
        summary.open_orders = getattr(executor, "open_orders")
    if executor and hasattr(executor, "positions"):
        summary.positions = getattr(executor, "positions")
    if state_manager:
        summary.state_loaded = True
    return summary


async def reconcile_on_startup(executor: Any = None, state_manager: Any = None) -> dict[str, Any]:
    """Compatibility summary helper retained for existing imports/tests."""
    return summarize_recovery_state(executor, state_manager).as_dict()


async def run_live_recovery_sequence(executor: Any, state_manager: Any = None, logger: Any = None) -> StartupRecoverySummary:
    """Run live startup recovery in the same order main.py previously used.

    Sequence:
      1. ``executor.reconcile_on_startup()``
      2. ``executor.cancel_all()``

    Exceptions propagate exactly as they did inline in ``main.py``.
    """
    summary = summarize_recovery_state(executor, state_manager)

    await executor.reconcile_on_startup()
    summary.reconciled = True
    summary.steps.append("reconcile_on_startup")

    await executor.cancel_all()
    summary.cancelled_stale = True
    summary.steps.append("cancel_all")

    # Refresh state in case reconciliation/cancellation updated executor attrs.
    refreshed = summarize_recovery_state(executor, state_manager)
    summary.open_orders = refreshed.open_orders
    summary.positions = refreshed.positions
    summary.state_loaded = refreshed.state_loaded

    if logger is not None:
        logger.info("startup_cleanup", msg="Cancelled all stale orders from previous session")
    return summary
