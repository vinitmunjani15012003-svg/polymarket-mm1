"""Restart/recovery workflow boundary."""

from __future__ import annotations


async def reconcile_on_startup(executor=None, state_manager=None) -> dict:
    """Fetch exchange state and return a structured recovery summary.

    Existing startup code still owns the concrete sequence; this helper is the
    new architecture seam for moving recovery out of main.py incrementally.
    """
    summary = {"open_orders": None, "positions": None, "cancelled_stale": False}
    if executor and hasattr(executor, "open_orders"):
        summary["open_orders"] = getattr(executor, "open_orders")
    if state_manager:
        summary["state_loaded"] = True
    return summary
