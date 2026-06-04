"""QuotePolicy facade for quote validation and guardrail orchestration."""

from __future__ import annotations

from typing import Callable

from src.core.models.decision import DecisionResult
from .quote_builder import construct_orders
from .quote_sanity import validate_quote_pair, validate_quote_pair_for_active_sides
from .size_policy import normalize_quote_sizes


class QuotePolicy:
    def validate(self, quotes) -> DecisionResult:
        return validate_quote_pair(getattr(quotes, "yes_buy_price", None), getattr(quotes, "no_buy_price", None))

    def validate_final(self, quotes, max_combined_cost: float = 0.99) -> DecisionResult:
        return validate_quote_pair_for_active_sides(
            yes_price=getattr(quotes, "yes_buy_price", None),
            yes_size=getattr(quotes, "yes_buy_size", 0),
            no_price=getattr(quotes, "no_buy_price", None),
            no_size=getattr(quotes, "no_buy_size", 0),
            max_combined_cost=max_combined_cost,
        )

    def construct_orders(self, market_id: str, token_id_yes: str, token_id_no: str, quotes) -> list[dict]:
        return construct_orders(market_id, token_id_yes, token_id_no, quotes)

    def normalize_sizes(
        self,
        quotes,
        min_order_size: int,
        allow_round_up: bool = False,
        repair_mode: str = "normal",
    ) -> DecisionResult:
        before = {
            "yes_size": int(getattr(quotes, "yes_buy_size", 0) or 0),
            "no_size": int(getattr(quotes, "no_buy_size", 0) or 0),
        }
        quotes.yes_buy_size, quotes.no_buy_size = normalize_quote_sizes(
            before["yes_size"],
            before["no_size"],
            min_order_size,
            allow_round_up=allow_round_up,
        )
        if repair_mode.startswith("dust_") and (
            quotes.yes_buy_size < min_order_size or quotes.no_buy_size < min_order_size
        ):
            quotes.yes_buy_size = 0
            quotes.no_buy_size = 0
            return DecisionResult.block(
                "HOLD",
                "DUST_REPAIR_NOT_ATOMIC",
                before=before,
                yes_size=quotes.yes_buy_size,
                no_size=quotes.no_buy_size,
            )
        return DecisionResult.allow(
            "QUOTE",
            "SIZES_NORMALIZED",
            before=before,
            yes_size=quotes.yes_buy_size,
            no_size=quotes.no_buy_size,
        )

    def enforce_repair_side(self, quotes, repair_mode: str) -> DecisionResult:
        if repair_mode == "repair_up":
            quotes.no_buy_size = 0
            return DecisionResult.allow("QUOTE", "REPAIR_UP_YES_ONLY")
        if repair_mode == "repair_down":
            quotes.yes_buy_size = 0
            return DecisionResult.allow("QUOTE", "REPAIR_DOWN_NO_ONLY")
        return DecisionResult.allow("QUOTE", "NO_REPAIR_SIDE_RESTRICTION")

    def enforce_normal_atomicity(
        self,
        quotes,
        *,
        repair_mode: str,
        abs_imbalance: float,
        min_order_size: int,
        fv_entry_side: str | None = None,
        sct_entry_side: str | None = None,
        merge_blocked: bool = False,
        reason: str = "NORMAL_QUOTE_NOT_ATOMIC",
    ) -> DecisionResult:
        if repair_mode != "normal" or abs_imbalance >= min_order_size:
            return DecisionResult.allow("QUOTE", "ATOMICITY_NOT_REQUIRED")

        one_sided_normal = (getattr(quotes, "yes_buy_size", 0) > 0) != (getattr(quotes, "no_buy_size", 0) > 0)
        allowed_fv_entry = fv_entry_side in ("yes", "no") and one_sided_normal and not merge_blocked
        allowed_sct_entry = sct_entry_side in ("yes", "no") and one_sided_normal and not merge_blocked
        if (one_sided_normal and not (allowed_fv_entry or allowed_sct_entry)) or merge_blocked:
            before = {"yes_size": quotes.yes_buy_size, "no_size": quotes.no_buy_size}
            quotes.yes_buy_size = 0
            quotes.no_buy_size = 0
            return DecisionResult.block(
                "HOLD",
                reason,
                one_sided_normal=one_sided_normal,
                merge_blocked=merge_blocked,
                before=before,
                yes_size=0,
                no_size=0,
            )
        return DecisionResult.allow("QUOTE", "ATOMIC")

    def enforce_inventory_heavy_side(self, quotes, imbalance: float, min_order_size: int, repair_mode: str) -> DecisionResult:
        if abs(float(imbalance or 0)) < min_order_size:
            return DecisionResult.allow("QUOTE", "NO_HEAVY_SIDE_BACKSTOP", repair_mode=repair_mode)
        if float(imbalance) > 0:
            quotes.yes_buy_size = 0
            return DecisionResult.allow("QUOTE", "HEAVY_YES_BLOCKED", repair_mode="repair_down")
        quotes.no_buy_size = 0
        return DecisionResult.allow("QUOTE", "HEAVY_NO_BLOCKED", repair_mode="repair_up")

    def apply_post_capital_safety(
        self,
        quotes,
        *,
        min_order_size: int,
        allow_round_up: bool,
        repair_mode: str,
        abs_imbalance: float,
        fv_entry_side: str | None = None,
        sct_entry_side: str | None = None,
        merge_blocked: bool = False,
        atomic_reason: str = "NORMAL_QUOTE_NOT_ATOMIC",
    ) -> DecisionResult:
        """Apply pure quote-side invariants after capital/backoff transforms.

        MarketCycler owns capital availability, logging, and dashboard effects;
        QuotePolicy owns the resulting quote mutation/validation decisions.
        """
        decisions: list[DecisionResult] = []
        decisions.append(self.normalize_sizes(
            quotes,
            min_order_size,
            allow_round_up=allow_round_up,
            repair_mode=repair_mode,
        ))
        decisions.append(self.enforce_repair_side(quotes, repair_mode))
        atomic_decision = self.enforce_normal_atomicity(
            quotes,
            repair_mode=repair_mode,
            abs_imbalance=abs_imbalance,
            min_order_size=min_order_size,
            fv_entry_side=fv_entry_side,
            sct_entry_side=sct_entry_side,
            merge_blocked=merge_blocked,
            reason=atomic_reason,
        )
        decisions.append(atomic_decision)
        if not atomic_decision.allowed:
            return DecisionResult.block(
                atomic_decision.action,
                atomic_decision.reason,
                decisions=decisions,
                repair_mode=repair_mode,
                **atomic_decision.metadata,
            )
        blocked = next((decision for decision in decisions if not decision.allowed), None)
        if blocked:
            return DecisionResult.block(
                blocked.action,
                blocked.reason,
                decisions=decisions,
                repair_mode=repair_mode,
                **blocked.metadata,
            )
        return DecisionResult.allow("QUOTE", "POST_CAPITAL_SAFETY_OK", decisions=decisions, repair_mode=repair_mode)

    def apply_final_inventory_safety(
        self,
        quotes,
        *,
        imbalance: float,
        min_order_size: int,
        repair_mode: str,
        max_combined_cost: float = 0.99,
    ) -> DecisionResult:
        """Apply final inventory heavy-side backstop and active-side validation."""
        heavy_decision = self.enforce_inventory_heavy_side(quotes, imbalance, min_order_size, repair_mode)
        repair_mode = heavy_decision.metadata.get("repair_mode", repair_mode)
        validation_decision = self.validate_final(quotes, max_combined_cost=max_combined_cost)
        decisions = [heavy_decision, validation_decision]
        if not validation_decision.allowed:
            return DecisionResult.block(
                validation_decision.action,
                validation_decision.reason,
                decisions=decisions,
                repair_mode=repair_mode,
                **validation_decision.metadata,
            )
        return DecisionResult.allow(
            "QUOTE",
            "FINAL_INVENTORY_SAFETY_OK",
            decisions=decisions,
            repair_mode=repair_mode,
            validation_reason=validation_decision.reason,
        )

    @staticmethod
    def is_repair_side(side_label: str, repair_mode: str) -> bool:
        return (repair_mode == f"repair_{side_label}"
                or (side_label == "yes" and repair_mode == "repair_up")
                or (side_label == "no" and repair_mode == "repair_down"))

    def apply_pair_cost_side_guard(
        self,
        quotes,
        *,
        side_label: str,
        repair_mode: str,
        cap: float,
        pair_edge: float,
        best_ask: float | None,
        best_bid: float | None,
        aggressive_price_fn: Callable[..., float | None],
        guard_source: str = "fifo",
    ) -> DecisionResult:
        buy_price_attr = "yes_buy_price" if side_label == "yes" else "no_buy_price"
        buy_size_attr = "yes_buy_size" if side_label == "yes" else "no_buy_size"
        size_val = getattr(quotes, buy_size_attr, 0)
        price_val = getattr(quotes, buy_price_attr, None)
        if size_val <= 0 or not price_val:
            return DecisionResult.allow("QUOTE", "PAIR_COST_SIDE_INACTIVE", side=side_label)
        if cap >= 0.99:
            return DecisionResult.allow("QUOTE", "PAIR_COST_UNCONSTRAINED", side=side_label, cap=cap)

        is_repair = self.is_repair_side(side_label, repair_mode)

        if cap < 0.01:
            setattr(quotes, buy_size_attr, 0)
            return DecisionResult.block(
                "CANCEL_SIDE", "PAIR_COST_BLOCKED", side=side_label, quoted=price_val, cap=cap, mode=repair_mode
            )
        if is_repair:
            new_price = aggressive_price_fn(price_val, cap, best_ask=best_ask, best_bid=best_bid)
            if new_price is None:
                setattr(quotes, buy_size_attr, 0)
                return DecisionResult.block(
                    "CANCEL_SIDE", "PAIR_COST_REPAIR_NO_PRICE", side=side_label, quoted=price_val, cap=cap, mode=repair_mode
                )
            setattr(quotes, buy_price_attr, new_price)
            if price_val and price_val > cap:
                reason = "REPAIR_QUOTE_CAPPED_FOR_PAIR_EDGE"
            elif new_price > float(price_val or 0):
                reason = "REPAIR_QUOTE_AGGRESSED_TO_CAP"
            else:
                reason = "PAIR_COST_OK"
            return DecisionResult.allow(
                "REPAIR", reason, side=side_label, old_price=price_val, new_price=new_price,
                cap=cap, min_edge=pair_edge, best_ask=best_ask, source=guard_source
            )
        if float(price_val) > cap:
            setattr(quotes, buy_price_attr, round(cap, 2))
            return DecisionResult.allow(
                "REDUCE_SIZE", "PAIR_COST_CLAMPED", side=side_label, quoted=price_val, cap=cap, mode=repair_mode
            )
        return DecisionResult.allow("QUOTE", "PAIR_COST_OK", side=side_label, quoted=price_val, cap=cap, mode=repair_mode)
