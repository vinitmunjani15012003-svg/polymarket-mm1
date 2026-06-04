"""Order intent helpers for execution services.

These helpers keep quote-version/idempotency concerns out of concrete live/dry
executors.  They deliberately avoid changing order payloads required by
ClobClientWrapper/DryRunExecutor.
"""

from __future__ import annotations

from src.core.models.orders import OrderIntent


def next_quote_version(current: int | None) -> int:
    """Return the next monotonically increasing quote version."""
    return int(current or 0) + 1


def build_place_intent(*, market_id: str, quote_version: int, side: str,
                       token_id: str, price: float | None,
                       size: float) -> OrderIntent:
    """Build the stable idempotency key for a quote placement."""
    return OrderIntent(
        market_id=market_id,
        quote_version=int(quote_version),
        side=side,  # type: ignore[arg-type]
        action="PLACE",
        price=price,
        size=size,
        token_id=str(token_id),
    )


def attach_place_intent(spec: dict, *, market_id: str, quote_version: int) -> dict:
    """Return a copy of an order spec with its OrderIntent attached."""
    enriched = dict(spec)
    enriched["quote_version"] = int(quote_version)
    enriched["intent"] = build_place_intent(
        market_id=market_id,
        quote_version=quote_version,
        side=str(spec["side"]),
        token_id=str(spec["token_id"]),
        price=spec.get("price"),
        size=float(spec.get("size") or 0),
    )
    return enriched


def strip_execution_metadata(spec: dict) -> dict:
    """Remove orchestration-only metadata before handing specs to executors."""
    return {
        key: value
        for key, value in spec.items()
        if key not in {"intent", "quote_version", "intent_id"}
    }
