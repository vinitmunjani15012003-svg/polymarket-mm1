"""Balanced repair planning for negative matched-pair debt.

This module implements the "add new profitable balanced pairs to dilute old
bad pairs" idea as a pure planner.  It does not assume both legs will fill;
callers must still submit/cancel using the normal order-management safety
path.
"""

from __future__ import annotations

import math
from typing import Any

from src.core.models.inventory import RepairPlan


def _cfg(config: Any, name: str, default: Any) -> Any:
    if config is None:
        return default
    return getattr(config, name, default)


def negative_pair_debt(pos) -> tuple[float, float, float]:
    """Return (debt, matched_pairs, pair_pnl) for a position-like object."""
    try:
        pair_pnl = float(pos.matched_pair_profit() or 0.0)
    except Exception:
        pair_pnl = 0.0
    try:
        matched_pairs = float(pos.matched_pairs() or 0.0)
    except Exception:
        matched_pairs = 0.0
    debt = max(0.0, -pair_pnl)
    return debt, matched_pairs, pair_pnl


def balanced_repair_debt_eligible(pos, config=None) -> tuple[bool, str, dict[str, Any]]:
    """Whether negative matched-pair PnL should be handled by repair mode.

    This intentionally checks only debt eligibility, not current quote prices.
    If eligible, orchestration may keep the position open and wait for a thick
    enough future balanced pair instead of immediately merging/clearing it.
    """
    enabled = bool(_cfg(config, "enabled", False))
    debt, matched_pairs, pair_pnl = negative_pair_debt(pos)
    min_debt = max(0.0, float(_cfg(config, "min_repair_debt", 0.01) or 0.0))
    max_debt = max(0.0, float(_cfg(config, "max_repair_debt", 5.0) or 0.0))
    metadata = {
        "enabled": enabled,
        "debt": round(debt, 6),
        "matched_pairs": round(matched_pairs, 6),
        "pair_pnl": round(pair_pnl, 6),
        "min_repair_debt": min_debt,
        "max_repair_debt": max_debt,
    }

    if not enabled:
        return False, "DISABLED", metadata
    if matched_pairs <= 0:
        return False, "NO_MATCHED_PAIRS", metadata
    if debt < min_debt:
        return False, "DEBT_BELOW_MIN", metadata
    if max_debt > 0 and debt > max_debt:
        return False, "DEBT_ABOVE_MAX", metadata
    return True, "ELIGIBLE", metadata


def plan_balanced_negative_edge_repair(
    pos,
    *,
    yes_price: float | None,
    no_price: float | None,
    min_order_size: int,
    max_order_size: int,
    config=None,
    remaining_seconds: float = 900.0,
    abs_imbalance: float = 0.0,
    is_halted: bool = False,
    close_only_phase: bool = False,
    small_capital_enabled: bool = False,
) -> RepairPlan:
    """Plan equal YES/NO orders that offset locked negative pair debt.

    A plan is returned only when all of these are true:
    - balanced repair is config-enabled;
    - the position has negative FIFO matched-pair PnL within the configured cap;
    - share inventory is already balanced enough (no close-only tail to repair);
    - the new pair cost is <= 1 - min_pair_edge;
    - enough time remains and we are not in a halt/close-only mode.

    Sizing targets enough new positive-edge pairs to offset current repair debt,
    capped by the current order-size limit.  The caller may receive repeated
    plans across quote cycles until the debt is offset by actual fills.
    """
    eligible, reason, metadata = balanced_repair_debt_eligible(pos, config)
    min_order_size = max(1, int(min_order_size or 1))
    max_order_size = max(min_order_size, int(max_order_size or min_order_size))

    if not eligible:
        return RepairPlan(mode="normal", reason=reason, metadata=metadata)

    if small_capital_enabled:
        return RepairPlan(mode="normal", reason="SMALL_CAPITAL_DISABLED", metadata=metadata)
    if is_halted:
        return RepairPlan(mode="normal", reason="HALTED", metadata=metadata)
    if close_only_phase:
        return RepairPlan(mode="normal", reason="CLOSE_ONLY_PHASE", metadata=metadata)
    max_abs_imbalance = max(0.0, float(_cfg(config, "max_abs_imbalance", 0.5) or 0.0))
    if abs(float(abs_imbalance or 0.0)) > max_abs_imbalance:
        metadata["abs_imbalance"] = float(abs_imbalance or 0.0)
        metadata["max_abs_imbalance"] = max_abs_imbalance
        return RepairPlan(mode="normal", reason="IMBALANCE_REPAIR_FIRST", metadata=metadata)

    min_remaining = max(0.0, float(_cfg(config, "min_seconds_remaining", 90.0) or 0.0))
    if float(remaining_seconds or 0.0) < min_remaining:
        metadata["remaining_seconds"] = float(remaining_seconds or 0.0)
        metadata["min_seconds_remaining"] = min_remaining
        return RepairPlan(mode="normal", reason="TOO_LATE", metadata=metadata)

    try:
        yp = float(yes_price or 0.0)
        np = float(no_price or 0.0)
    except Exception:
        yp, np = 0.0, 0.0
    if yp <= 0 or np <= 0:
        metadata.update({"yes_price": yes_price, "no_price": no_price})
        return RepairPlan(mode="normal", reason="MISSING_PRICE", metadata=metadata)

    min_pair_edge = max(0.0, float(_cfg(config, "min_pair_edge", 0.02) or 0.0))
    configured_max_pair_cost = _cfg(config, "max_pair_cost", None)
    if configured_max_pair_cost is None:
        max_pair_cost = max(0.0, 1.0 - min_pair_edge)
    else:
        max_pair_cost = max(0.0, min(1.0, float(configured_max_pair_cost or 0.0)))
        min_pair_edge = max(min_pair_edge, 1.0 - max_pair_cost)

    pair_cost = round(yp + np, 4)
    pair_edge = round(1.0 - pair_cost, 4)
    metadata.update({
        "yes_price": round(yp, 4),
        "no_price": round(np, 4),
        "pair_cost": pair_cost,
        "pair_edge": pair_edge,
        "max_pair_cost": round(max_pair_cost, 4),
        "min_pair_edge": round(min_pair_edge, 4),
    })

    if pair_cost > max_pair_cost or pair_edge <= 0:
        return RepairPlan(mode="normal", reason="PAIR_EDGE_TOO_THIN", metadata=metadata)

    target_net_profit = max(0.0, float(_cfg(config, "target_net_profit", 0.0) or 0.0))
    debt = float(metadata["debt"])
    needed_pairs = int(math.ceil((debt + target_net_profit) / max(pair_edge, 1e-9)))

    config_size_cap = int(_cfg(config, "max_order_size", 0) or 0)
    cycle_cap = min(max_order_size, config_size_cap) if config_size_cap > 0 else max_order_size
    repair_size = min(cycle_cap, max(min_order_size, needed_pairs))
    if repair_size < min_order_size:
        return RepairPlan(mode="normal", reason="SIZE_BELOW_MIN", metadata=metadata)

    metadata.update({
        "needed_pairs": needed_pairs,
        "target_net_profit": target_net_profit,
        "cycle_size_cap": cycle_cap,
    })
    return RepairPlan(
        yes_size=int(repair_size),
        no_size=int(repair_size),
        mode="balanced_repair",
        reason="NEGATIVE_PAIR_DEBT_REPAIR",
        metadata=metadata,
    )
