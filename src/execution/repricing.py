"""Order repricing policy."""

from __future__ import annotations

from typing import Optional


def is_crossed_buy(price: Optional[float], book_snapshot=None) -> bool:
    return bool(book_snapshot is not None and price is not None and price >= book_snapshot.best_ask)


class RepricePolicy:
    def __init__(self, reprice_threshold: float = 0.005, repair_reprice_threshold: float | None = None):
        self.reprice_threshold = float(reprice_threshold or 0.0)
        self.repair_reprice_threshold = max(0.05, float(repair_reprice_threshold if repair_reprice_threshold is not None else self.reprice_threshold))

    def needs_reprice(self, existing_price: Optional[float],
                      new_price: Optional[float],
                      existing_size: int,
                      new_size: int) -> bool:
        if existing_price is None:
            return new_price is not None and new_size > 0
        if new_price is None or new_size <= 0:
            return True
        if abs(new_price - existing_price) >= self.reprice_threshold:
            return True
        if existing_size > 0:
            ratio = new_size / existing_size
            if ratio < 0.5 or ratio > 1.5:
                return True
        return False

    def decision(self, existing_price: Optional[float],
                 new_price: Optional[float],
                 existing_size: int,
                 new_size: int,
                 book_snapshot=None,
                 sticky_repair: bool = False) -> tuple[bool, bool]:
        if existing_price is None:
            return (new_price is not None and new_size > 0), False
        if new_price is None or new_size <= 0:
            return True, True
        if is_crossed_buy(existing_price, book_snapshot):
            return True, True

        price_delta = new_price - existing_price
        if sticky_repair:
            if abs(price_delta) > self.repair_reprice_threshold:
                return True, price_delta < 0
            if existing_size > 0 and new_size > existing_size * 2.0:
                return True, False
            return False, False

        if abs(price_delta) > self.reprice_threshold:
            return True, price_delta < 0
        if existing_size > 0 and new_size > existing_size * 1.5:
            return True, False
        return False, False
