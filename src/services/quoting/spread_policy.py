"""Spread/edge policy helpers."""

from __future__ import annotations


def combined_cost(yes_price: float | None, no_price: float | None) -> float:
    return round(float(yes_price or 0.0) + float(no_price or 0.0), 4)


def edge_per_pair(yes_price: float | None, no_price: float | None) -> float:
    return round(1.0 - combined_cost(yes_price, no_price), 4)


def has_pair_edge(yes_price: float | None, no_price: float | None, min_edge: float) -> bool:
    return edge_per_pair(yes_price, no_price) >= float(min_edge or 0.0)
