"""
Risk engine — pre-trade checks, P&L stops, and quoting phase control.

Designed for 15-minute markets with automatic session resets
between market cycles.
"""

import time
from src.monitoring.logger import get_logger

log = get_logger("risk_engine")


class RiskEngine:
    def __init__(self, total_capital: float, max_daily_loss_pct: float = 0.05,
                 max_drawdown_pct: float = 0.10):
        """
        Args:
            total_capital: Total capital allocated.
            max_daily_loss_pct: Max loss as % of capital before daily halt.
                                0.10 = 10% = $20 on $200 capital.
            max_drawdown_pct: Max drawdown from peak before pause.
                              0.10 = 10% = $20 drawdown from peak.
        """
        self.total_capital = total_capital
        self.max_daily_loss = -total_capital * max_daily_loss_pct
        self.max_drawdown = -total_capital * max_drawdown_pct
        self.session_start_pnl = 0.0
        self.peak_pnl = 0.0
        self.halted = False
        self.halt_reason = ""
        self._halt_until = 0.0  # Temporary halt with auto-resume

    def check_stops(self, current_pnl: float) -> bool:
        """
        Returns False if trading should stop.
        
        Supports temporary halts (auto-resume after cooldown)
        and permanent daily halts.
        """
        # Check temporary halt cooldown
        if self._halt_until > 0 and time.time() < self._halt_until:
            return False
        elif self._halt_until > 0 and time.time() >= self._halt_until:
            # Cooldown expired, resume
            self._halt_until = 0.0
            self.halted = False
            log.info("risk_cooldown_expired", msg="Resuming trading after cooldown")

        if self.halted and self.halt_reason == "DAILY_LOSS_LIMIT":
            return False  # Permanent halt for the day

        daily_pnl = current_pnl - self.session_start_pnl
        self.peak_pnl = max(self.peak_pnl, current_pnl)
        drawdown = current_pnl - self.peak_pnl

        if daily_pnl < self.max_daily_loss:
            self._halt_permanent("DAILY_LOSS_LIMIT", daily_pnl)
            return False

        if drawdown < self.max_drawdown:
            # Temporary halt — pause for 60 seconds then resume
            self._halt_temporary("INTRADAY_DRAWDOWN", drawdown, cooldown=60)
            return False

        return True

    def _halt_permanent(self, reason: str, value: float):
        self.halted = True
        self.halt_reason = reason
        log.critical("trading_halted_permanent", reason=reason,
                     value=round(value, 4))

    def _halt_temporary(self, reason: str, value: float, cooldown: int = 60):
        self.halted = True
        self.halt_reason = reason
        self._halt_until = time.time() + cooldown
        log.warning("trading_paused", reason=reason,
                    value=round(value, 4), cooldown_s=cooldown)

    def reset_for_new_market(self, current_pnl: float = 0.0):
        """Reset drawdown tracking for a new market cycle.
        Does NOT reset daily loss tracking."""
        self.peak_pnl = current_pnl
        if self.halt_reason != "DAILY_LOSS_LIMIT":
            self.halted = False
            self.halt_reason = ""
            self._halt_until = 0.0

    def reset_session(self, starting_pnl: float = 0.0):
        """Full reset for a new trading session/day."""
        self.session_start_pnl = starting_pnl
        self.peak_pnl = starting_pnl
        self.halted = False
        self.halt_reason = ""
        self._halt_until = 0.0


def determine_phase(time_remaining: float, stop_seconds: int = 120,
                    reduce_seconds: int = 300) -> str:
    """
    Determine quoting phase based on time remaining.
    Returns: "FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE", "WIND_DOWN", or "ACTIVE"
    """
    if time_remaining < 15:
        return "FINAL_SECONDS"
    elif time_remaining < 60:
        return "DEFENSIVE"
    elif time_remaining < stop_seconds:
        return "DEAD_ZONE"
    elif time_remaining < reduce_seconds:
        return "WIND_DOWN"
    return "ACTIVE"


def apply_phase_params(phase: str, quote_engine, asset_config):
    """Adjust quote engine parameters based on current phase."""
    if phase == "FINAL_SECONDS":
        # Full stop unless inventory repair dictates it (Close only)
        quote_engine.max_order_size = asset_config.max_order_size
        quote_engine.gamma = asset_config.gamma_near_expiry * 2.0
        quote_engine.min_spread = max(0.10, asset_config.max_spread)
    elif phase == "DEFENSIVE":
        # Stop two-sided quoting earlier, defensive mode
        quote_engine.max_order_size = max(1, int(asset_config.max_order_size * 0.05))
        quote_engine.gamma = asset_config.gamma_near_expiry * 1.5
        quote_engine.min_spread = max(0.08, asset_config.max_spread)
    elif phase == "DEAD_ZONE":
        # Allow close-only sizing, but widen spread aggressively to prevent adverse selection
        quote_engine.max_order_size = asset_config.max_order_size
        quote_engine.gamma = asset_config.gamma_near_expiry
        quote_engine.min_spread = max(0.05, asset_config.max_spread)
    elif phase == "WIND_DOWN":
        # Reduce size earlier
        quote_engine.max_order_size = max(2, int(asset_config.max_order_size * 0.10))
        quote_engine.gamma = asset_config.gamma_near_expiry
        quote_engine.min_spread = 0.03  # 3 ticks near expiry
    else:  # ACTIVE
        quote_engine.max_order_size = asset_config.max_order_size
        quote_engine.gamma = asset_config.gamma
        quote_engine.min_spread = asset_config.min_spread


def pre_trade_checks(fair_value: float, quotes, inventory_state,
                     fair_value_fresh: bool, phase: str) -> tuple[bool, list[str]]:
    """
    Run all pre-trade validations.
    Returns (passed: bool, failed_checks: list[str]).
    """
    failed = []

    if phase in ["FINAL_SECONDS", "DEFENSIVE", "DEAD_ZONE"]:
        # In these phases, we only allow CLOSE-ONLY orders (one side must be 0)
        # If both sides are quoted, it's illegal.
        if quotes.yes_buy_size > 0 and quotes.no_buy_size > 0:
            failed.append(f"{phase.lower()}_no_two_sided")

    if not fair_value_fresh:
        failed.append("stale_fair_value")

    if quotes.yes_buy_price is not None and quotes.no_buy_price is not None:
        combined = quotes.yes_buy_price + quotes.no_buy_price
        if combined >= 1.0 and quotes.yes_buy_size > 0 and quotes.no_buy_size > 0:
            failed.append("no_edge_combined_cost_gte_1")

    # Only fail on price_too_low if that side is actually active
    if quotes.yes_buy_size > 0 and (quotes.yes_buy_price or 0) <= 0.005:
        failed.append("price_too_low_yes")
    if quotes.no_buy_size > 0 and (quotes.no_buy_price or 0) <= 0.005:
        failed.append("price_too_low_no")

    if quotes.yes_buy_size <= 0 and quotes.no_buy_size <= 0:
        failed.append("zero_size_both_sides")

    # NOTE: EMERGENCY inventory is handled by compute_size_adjustment()
    # which stops the heavy side but keeps the light side for rebalancing.
    # We do NOT block all trading here.

    return len(failed) == 0, failed
