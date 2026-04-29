"""
Adverse selection and toxicity monitoring.
Tracks per-fill edge and delayed price drift after fills.
"""

import time
from collections import deque
from src.monitoring.logger import get_logger

log = get_logger("toxicity")


class FillEdgeTracker:
    """
    Real-time per-fill edge metric.
    edge = (current_mid - fill_price) * direction
    Positive = good fill, Negative = adverse selection.
    """
    def __init__(self, window=30):
        self.edges = deque(maxlen=window)
        self.fills = deque(maxlen=window)

    def record_fill(self, side: str, fill_price: float, current_mid: float):
        direction = 1 if side == "yes" else -1  # bought YES: want price up
        edge = (current_mid - fill_price) * direction
        self.edges.append(edge)
        self.fills.append({"side": side, "edge": edge, "time": time.time()})

    def adverse_selection_rate(self) -> float:
        if len(self.edges) < 5:
            return 0.0
        return sum(1 for e in self.edges if e < 0) / len(self.edges)

    def mean_edge(self) -> float:
        if not self.edges:
            return 0.0
        return sum(self.edges) / len(self.edges)
        
    def recent_one_sided_fills(self) -> int:
        if not self.fills:
            return 0
        last_side = self.fills[-1]["side"]
        count = 0
        for f in reversed(self.fills):
            if f["side"] == last_side:
                count += 1
            else:
                break
        return count

    def should_react(self, quote_engine) -> bool:
        """Auto-widen spreads if adverse selection is high. Returns True if we should HALT."""
        rate = self.adverse_selection_rate()
        avg = self.mean_edge()

        if rate > 0.7 and avg < -0.005:
            quote_engine.spread_multiplier = min(3.0, quote_engine.spread_multiplier * 1.5)
            quote_engine.max_order_size = max(5, int(quote_engine.max_order_size * 0.5))
            log.warning("high_adverse_selection", rate=f"{rate:.0%}", avg_edge=f"{avg:.4f}")
            
        elif rate > 0.5:
            quote_engine.spread_multiplier = min(2.0, quote_engine.spread_multiplier * 1.1)
        elif rate < 0.3 and avg > 0:
            quote_engine.spread_multiplier = max(1.0, quote_engine.spread_multiplier * 0.95)
            quote_engine.max_order_size = min(quote_engine.base_order_size,
                                               quote_engine.max_order_size + 2)
        return False


class ToxicityMonitor:
    """Delayed toxicity measurement — checks price drift 30s after each fill."""
    def __init__(self, window_seconds=300, threshold=0.002, halt_cooldown=60):
        self.window = window_seconds
        self.threshold = threshold
        self.fill_history = []
        self.halt_until = 0.0
        self.halt_cooldown = halt_cooldown

    def record_fill(self, side: str, price: float, size: float, mid_at_fill: float):
        self.fill_history.append({
            "time": time.time(), "side": side, "price": price,
            "size": size, "mid_at_fill": mid_at_fill, "mid_after": None,
        })

    def update_delayed_mids(self, current_mid: float):
        """Call periodically to fill in the 'mid_after' for fills > 30s old."""
        now = time.time()
        for f in self.fill_history:
            if f["mid_after"] is None and now - f["time"] >= 30:
                f["mid_after"] = current_mid

    def compute_toxicity(self) -> float:
        """Volume-weighted adverse drift per share."""
        cutoff = time.time() - self.window
        recent = [f for f in self.fill_history
                  if f["time"] > cutoff and f["mid_after"] is not None]
        if not recent:
            return 0.0
        total_weighted = 0.0
        total_vol = 0.0
        for f in recent:
            direction = 1 if f["side"] in ("yes",) else -1
            drift = direction * (f["mid_after"] - f["mid_at_fill"])
            total_weighted += drift * f["size"]
            total_vol += f["size"]
        return total_weighted / total_vol if total_vol > 0 else 0.0

    def adjust_spread(self, quote_engine):
        tox = self.compute_toxicity()
        if tox < -self.threshold:
            quote_engine.spread_multiplier = min(3.0, quote_engine.spread_multiplier * 1.3)
        elif tox > -self.threshold * 0.3:
            quote_engine.spread_multiplier = max(1.0, quote_engine.spread_multiplier * 0.97)

    def check_kill_switch(self, edge_tracker: FillEdgeTracker) -> bool:
        """
        Check if we need to completely halt quoting.
        Returns True if halted.
        """
        now = time.time()
        if now < self.halt_until:
            return True
            
        # 1. Repeated adverse fills: rate > 80% and mean edge < -1c
        if edge_tracker.adverse_selection_rate() > 0.8 and edge_tracker.mean_edge() < -0.01:
            log.error("toxicity_halt", reason="repeated_adverse_fills")
            self.halt_until = now + self.halt_cooldown
            return True
            
        # 2. One-sided fill regime: 6 or more consecutive fills on the same side
        if edge_tracker.recent_one_sided_fills() >= 6:
            log.error("toxicity_halt", reason="one_sided_fill_regime")
            self.halt_until = now + self.halt_cooldown
            return True
            
        # 3. Immediate post-fill move against us: tox < extreme threshold
        tox = self.compute_toxicity()
        if tox < -0.01:  # drifted 1c against us on average recently
            log.error("toxicity_halt", reason="immediate_post_fill_drift", tox=tox)
            self.halt_until = now + self.halt_cooldown
            return True
            
        return False
