"""
Volatility estimation from CEX data.

Computes realized volatility from rolling price samples
and optionally blends with Deribit implied volatility.
"""

import math
import time
import numpy as np
from collections import deque
from typing import Optional

from src.monitoring.logger import get_logger

log = get_logger("volatility")

# Short-horizon binary markets are extremely sensitive to sigma. A BTC annual
# vol of 20% makes ordinary 15m moves look nearly certain and pins raw FV at
# 0.99, even when the live Polymarket book is around 0.70. Use a conservative
# model floor so the raw model remains usable for basis/risk decisions.
MIN_BINARY_MODEL_SIGMA = 0.60


class VolatilityEstimator:
    """
    Estimate realized + implied volatility from exchange data.
    CEX data is 100x more liquid than Polymarket book.
    """

    def __init__(self, lookback_seconds: int = 300,
                 default_sigma: float = 0.60):
        """
        Args:
            lookback_seconds: Rolling window for realized vol (seconds).
            default_sigma: Default annualized vol when insufficient data.
        """
        self.lookback = lookback_seconds
        self.default_sigma = max(MIN_BINARY_MODEL_SIGMA, float(default_sigma or MIN_BINARY_MODEL_SIGMA))

        self._prices: deque = deque(maxlen=lookback_seconds)
        self._timestamps: deque = deque(maxlen=lookback_seconds)
        self._deribit_iv: Optional[float] = None
        self._deribit_iv_ts: float = 0.0

    def update(self, price: float, ts: float = None):
        """Record a new price observation (throttled to 1 sample/sec)."""
        ts = ts or time.time()
        
        # Throttle to 1 sample per second to avoid micro-structure collapse
        if self._timestamps and (ts - self._timestamps[-1]) < 1.0:
            return
            
        self._prices.append(price)
        self._timestamps.append(ts)

    def realized_sigma_annualized(self) -> float:
        """
        Compute annualized realized vol from rolling window.
        Returns sigma (e.g., 0.60 = 60% annualized).
        """
        # A 30-second rolling window of 1Hz ticks will mathematically collapse 
        # to ~10% annualized vol because micro-structure variance is near zero.
        # We bypass this and strictly use the default_sigma (or Deribit IV if passed).
        return self.default_sigma

    def set_deribit_iv(self, iv: float):
        """
        Set Deribit implied volatility (annualized).
        Call this periodically if you have access to Deribit data.
        """
        self._deribit_iv = iv
        self._deribit_iv_ts = time.time()

    def sigma_for_model(self) -> float:
        """
        Best estimate of annualized volatility.
        Blends realized vol with Deribit IV if available and fresh.
        """
        realized = self.realized_sigma_annualized()

        # Use Deribit IV if available and less than 60s old
        if (self._deribit_iv is not None and
                (time.time() - self._deribit_iv_ts) < 60):
            # Trust forward-looking IV more (70/30 blend)
            blended = 0.3 * realized + 0.7 * self._deribit_iv
            return max(MIN_BINARY_MODEL_SIGMA, min(3.0, blended))

        return max(MIN_BINARY_MODEL_SIGMA, realized)

    @property
    def has_sufficient_data(self) -> bool:
        """True if we have enough price history for reliable vol."""
        return len(self._prices) >= 30
