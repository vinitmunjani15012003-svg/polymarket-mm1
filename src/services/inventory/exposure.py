"""Inventory exposure calculations."""

from __future__ import annotations


def share_imbalance(yes_shares: float, no_shares: float) -> float:
    return float(yes_shares or 0.0) - float(no_shares or 0.0)


def gross_exposure(yes_shares: float, no_shares: float) -> float:
    return float(yes_shares or 0.0) + float(no_shares or 0.0)


def matched_pairs(yes_shares: float, no_shares: float) -> float:
    return min(float(yes_shares or 0.0), float(no_shares or 0.0))


def inventory_skew(yes_shares: float, no_shares: float) -> float:
    gross = gross_exposure(yes_shares, no_shares)
    if gross <= 0:
        return 0.0
    return share_imbalance(yes_shares, no_shares) / gross
