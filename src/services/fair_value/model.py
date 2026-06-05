"""Raw mathematical fair-value models."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from scipy.stats import norm

from src.monitoring.logger import get_logger

log = get_logger("fair_value_model")


class UpDownFairValue:
    """Fair value for 15-minute Up/Down markets.

    P(Up) = P(price at end >= price at start), using observed drift versus the
    event start price and remaining volatility.
    """

    def __init__(self, event_start_ts: float, resolve_ts: float,
                 start_price: float = None):
        self.event_start_ts = event_start_ts
        self.resolve_ts = resolve_ts
        self.start_price = start_price
        self._last_fair_value = 0.50
        self._last_update_ts = 0.0

    def fair_value(self, current_price: float, sigma_annualized: float,
                   now_ts: float = None, update_state: bool = True) -> float:
        now_ts = now_ts or time.time()
        t_remaining = max(1, self.resolve_ts - now_ts)
        t_years = t_remaining / (365.25 * 86400)

        if self.start_price and self.start_price > 0 and current_price > 0:
            log_return_so_far = math.log(current_price / self.start_price)
            vol_sqrt_t = sigma_annualized * math.sqrt(t_years)
            if vol_sqrt_t < 1e-10:
                prob = 0.99 if log_return_so_far >= 0 else 0.01
            else:
                prob = norm.cdf(log_return_so_far / vol_sqrt_t)
        else:
            prob = 0.50

        prob = max(0.01, min(0.99, prob))
        if update_state:
            self._last_fair_value = prob
            self._last_update_ts = now_ts
        return prob

    def set_start_price(self, price: float):
        if self.start_price is None and price > 0:
            self.start_price = price
            log.info("start_price_set", price=price)

    def time_remaining_seconds(self, now_ts: float = None) -> float:
        now_ts = now_ts or time.time()
        return max(0, self.resolve_ts - now_ts)

    def normalized_time(self, now_ts: float = None) -> float:
        now_ts = now_ts or time.time()
        total = self.resolve_ts - self.event_start_ts
        if total <= 0:
            return 0.0
        remaining = self.resolve_ts - now_ts
        return max(0.0, min(1.0, remaining / total))

    @property
    def last_fair_value(self) -> float:
        return self._last_fair_value

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._last_update_ts) > 5.0


@dataclass(slots=True)
class FairValueResult:
    raw_fv: float
    market_fv: float | None
    blended_fv: float
    tradable_fv: float
    confidence: float
    basis_gap: float | None
    source: str = "model"
    reason: str = "OK"
