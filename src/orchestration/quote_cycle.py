"""Quote-cycle lifecycle seams for MarketCycler.

The quote loop is still intentionally hosted in ``MarketCycler``; this module
provides small typed boundaries for the mutable inputs/results used by one
iteration so future lifecycle extraction can happen without changing trading
semantics.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from src.services.fair_value import basis_guard_triggered, polymarket_implied_up_mid

if TYPE_CHECKING:  # pragma: no cover
    from src.data.market_discovery import MarketInfo


@dataclass(frozen=True)
class QuoteCycleContext:
    """Immutable inputs captured at the start of one quote-cycle iteration."""

    market: "MarketInfo"
    now: float
    remaining: float

    @classmethod
    def from_market(cls, market: "MarketInfo", now: float) -> "QuoteCycleContext":
        return cls(market=market, now=now, remaining=market.time_remaining)


@dataclass(frozen=True)
class StaleSpotDecision:
    """Decision boundary for spot-feed fail-closed handling."""

    should_stop: bool
    dashboard_reason: str | None = None
    event_reason: str | None = None
    event_message: str | None = None
    log_event: str | None = None

    @property
    def is_ok(self) -> bool:
        return not self.should_stop


@dataclass(frozen=True)
class BookSnapshot:
    """Best bid/ask and implied-UP probability captured from one book read."""

    book_up: Any | None
    book_down: Any | None
    best_ask_yes: float | None
    best_bid_yes: float | None
    best_ask_no: float | None
    best_bid_no: float | None
    polymarket_mid_up: float | None


@dataclass(frozen=True)
class FairValuePackage:
    """Normalized FV engine outputs consumed by quote-cycle orchestration."""

    model_fv: float
    model_confidence: float
    uncapped_fv: float
    tradable_fv: float
    basis_delta: float | None


@dataclass(frozen=True)
class BasisRiskDecision:
    """Basis guard action after sizing/risk state has been assembled."""

    triggered: bool
    action: str | None = None  # close_only | stop_quoting | None



def decide_stale_spot(raw_spot: float | None, price_age: float, max_spot_age: float) -> StaleSpotDecision:
    """Return the quote-cycle action for missing/stale spot data.

    This is intentionally a pure packaging seam; side effects (logging,
    cancels, dashboard updates) remain in ``MarketCycler`` for compatibility.
    """

    if not raw_spot:
        return StaleSpotDecision(
            should_stop=True,
            dashboard_reason="NO_SPOT",
            event_reason="NO_SPOT_PRICE",
            event_message="spot unavailable",
            log_event="no_spot_price",
        )
    if price_age > max_spot_age:
        return StaleSpotDecision(
            should_stop=True,
            dashboard_reason="STALE_SPOT",
            event_reason="STALE_SPOT",
            event_message=f"age {price_age:.2f}s > max {max_spot_age:.2f}s",
            log_event="spot_price_stale_stop_quoting",
        )
    return StaleSpotDecision(should_stop=False)



def package_book_snapshot(books: Mapping[Any, Any], market: "MarketInfo") -> BookSnapshot:
    """Package YES/NO book data without changing lookup semantics."""

    book_up = books.get(market.token_id_up)
    book_down = books.get(market.token_id_down)
    return BookSnapshot(
        book_up=book_up,
        book_down=book_down,
        best_ask_yes=book_up.best_ask if book_up else None,
        best_bid_yes=book_up.best_bid if book_up else None,
        best_ask_no=book_down.best_ask if book_down else None,
        best_bid_no=book_down.best_bid if book_down else None,
        polymarket_mid_up=polymarket_implied_up_mid(book_up, book_down),
    )



def package_fair_value_result(fv_result: Any, polymarket_mid_up: float | None) -> FairValuePackage:
    """Package FairValueEngine output and basis delta for orchestration."""

    model_fv = fv_result.raw_fv
    basis_delta = abs(model_fv - polymarket_mid_up) if polymarket_mid_up is not None else None
    return FairValuePackage(
        model_fv=model_fv,
        model_confidence=fv_result.confidence,
        uncapped_fv=fv_result.blended_fv,
        tradable_fv=fv_result.tradable_fv,
        basis_delta=basis_delta,
    )



def decide_basis_risk(
    *,
    repair_mode: str,
    balance_only: bool,
    is_halted: bool,
    model_fv: float,
    polymarket_mid_up: float | None,
    abs_imbalance: float,
    min_order_size: int,
) -> BasisRiskDecision:
    """Package the basis-guard branch selection used by ``MarketCycler``."""

    if repair_mode != "normal" or balance_only or is_halted:
        return BasisRiskDecision(triggered=False)
    if not basis_guard_triggered(model_fv, polymarket_mid_up):
        return BasisRiskDecision(triggered=False)
    action = "close_only" if abs_imbalance >= min_order_size else "stop_quoting"
    return BasisRiskDecision(triggered=True, action=action)
