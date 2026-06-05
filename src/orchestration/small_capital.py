"""Small-capital one-cycle lifecycle policy for market cyclers.

This module keeps the small-capital state machine isolated from the broad
MarketCycler quote loop.  MarketCycler retains compatibility wrappers for older
callers/tests while delegating lifecycle decisions here.
"""

from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING

from src.monitoring.logger import get_logger
from src.services.inventory import emergency_hedge_cap_from_state, saved_repair_cap_from_state

if TYPE_CHECKING:  # pragma: no cover
    from src.data.market_discovery import MarketInfo

log = get_logger("market_cycler")


class SmallCapitalLifecycle:
    """One-cycle-per-window helper bound to a MarketCycler-like owner."""

    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def _small_capital_enabled(self) -> bool:
        cfg = getattr(self, "small_capital_config", None)
        return bool(cfg and getattr(cfg, "enabled", False) and getattr(cfg, "one_cycle_per_window", False))

    def _wallet_truth_snapshot(self, wallet_truth) -> dict | None:
        if wallet_truth is None:
            return None
        yes = float(wallet_truth[0] or 0)
        no = float(wallet_truth[1] or 0)
        return {
            "yes_shares": yes,
            "no_shares": no,
            "matched_pairs": min(yes, no),
            "share_imbalance": yes - no,
            "source": "wallet",
            "updated_ts": _time.time(),
        }

    def _apply_wallet_truth_to_small_capital_state(self, market_id: str, wallet_snapshot: dict | None) -> None:
        if not self._small_capital_enabled() or not wallet_snapshot:
            return
        yes = float(wallet_snapshot.get("yes_shares", 0) or 0)
        no = float(wallet_snapshot.get("no_shares", 0) or 0)
        state = self._small_capital_state(market_id)
        changed = False
        if yes > 0 or no > 0:
            if not state.get("quote_cycle_started"):
                state["quote_cycle_started"] = True
                state["quote_cycles_started"] = int(state.get("quote_cycles_started", 0) or 0) + 1
                state["opening_attempt_spent"] = True
                changed = True
            if not state.get("initial_filled"):
                state["initial_filled"] = True
                state["initial_side"] = "yes" if yes > 0 and yes >= no else "no"
                state.setdefault("initial_order_id", "wallet_truth")
                state.setdefault("initial_fill_ts", _time.time())
                changed = True
        if yes > 0 and no > 0 and not state.get("balancing_filled"):
            state["balancing_filled"] = True
            state["balancing_side"] = "yes" if yes < no else "no"
            state.setdefault("balancing_order_id", "wallet_truth")
            changed = True
        if changed:
            state["wallet_truth_reconciled"] = True
            self._save_small_capital_state(market_id, state)
            log.warning(
                "small_capital_state_reconciled_from_wallet",
                asset=self.asset,
                market=market_id[:8],
                yes=round(yes, 4),
                no=round(no, 4),
            )

    def _small_capital_balancing_side(self, market_id: str, pos, wallet_imbalance: float | None = None) -> str:
        """Return the only side small-capital mode may quote after first fill.

        Normal sizing can briefly see stale/flat inventory while the lifecycle
        state already knows the opening side. In one-cycle mode, once the first
        side is filled, every follow-up quote must be the opposite side; placing
        the same opening side again duplicates exposure.
        """
        if not self._small_capital_enabled():
            return ""
        state = self._small_capital_state(market_id)
        imbalance = float(wallet_imbalance if wallet_imbalance is not None else (pos.share_imbalance() or 0))
        if imbalance > 0:
            return "no"
        if imbalance < 0:
            return "yes"
        if state.get("initial_filled") and not state.get("balancing_filled"):
            initial_side = str(state.get("initial_side") or "").lower()
            if initial_side in ("no", "down"):
                return "yes"
            if initial_side in ("yes", "up"):
                return "no"
        return ""

    def _apply_small_capital_balancing_override(self, market_id: str, pos, quotes, repair_mode: str, min_order_size: int, wallet_imbalance: float | None = None) -> str:
        balancing_side = self._small_capital_balancing_side(market_id, pos, wallet_imbalance)
        effective_imbalance = float(wallet_imbalance if wallet_imbalance is not None else (pos.share_imbalance() or 0))
        if balancing_side == "yes":
            quotes.no_buy_size = 0
            quotes.yes_buy_size = max(int(quotes.yes_buy_size or 0), int(min_order_size or 0))
            log.warning(
                "small_capital_balancing_quote_forced",
                asset=self.asset,
                market=market_id[:8],
                side="yes",
                imbalance=round(effective_imbalance, 4),
                source="wallet" if wallet_imbalance is not None else "local",
                msg="forcing opposite side after initial small-capital fill",
            )
            return "repair_up"
        if balancing_side == "no":
            quotes.yes_buy_size = 0
            quotes.no_buy_size = max(int(quotes.no_buy_size or 0), int(min_order_size or 0))
            log.warning(
                "small_capital_balancing_quote_forced",
                asset=self.asset,
                market=market_id[:8],
                side="no",
                imbalance=round(effective_imbalance, 4),
                source="wallet" if wallet_imbalance is not None else "local",
                msg="forcing opposite side after initial small-capital fill",
            )
            return "repair_down"
        return repair_mode

    def _apply_small_capital_opening_one_side(self, market_id: str, quotes, repair_mode: str, fair_value: float) -> str | None:
        """In small-capital mode, the opening attempt may place only one side.

        The normal strategy can be atomic/two-sided while flat. That is correct
        for normal capital, but it violates one-cycle small-capital semantics by
        spending two opening orders before the state can mark the cycle started.
        Pick the better model-edge side and let the existing close-only repair
        path quote the complement after a fill.
        """
        if not self._small_capital_enabled() or repair_mode != "normal":
            return None
        state = self._small_capital_state(market_id)
        if self._small_capital_opening_spent(state):
            return None
        yes_size = int(getattr(quotes, "yes_buy_size", 0) or 0)
        no_size = int(getattr(quotes, "no_buy_size", 0) or 0)
        if yes_size <= 0 or no_size <= 0:
            return None

        fv = max(0.0, min(1.0, float(fair_value or 0.5)))
        yes_price = float(getattr(quotes, "yes_buy_price", 0) or 0)
        no_price = float(getattr(quotes, "no_buy_price", 0) or 0)
        yes_edge = fv - yes_price
        no_edge = (1.0 - fv) - no_price

        # Deterministic tie-breaker: favor the side indicated by FV. At exactly
        # neutral, YES is arbitrary but stable; only one order is the invariant.
        side = "yes" if yes_edge >= no_edge else "no"
        if side == "yes":
            quotes.no_buy_size = 0
        else:
            quotes.yes_buy_size = 0
        log.warning(
            "small_capital_opening_forced_one_side",
            asset=self.asset,
            market=market_id[:8],
            side=side,
            fair_value=round(fv, 4),
            yes_edge=round(yes_edge, 4),
            no_edge=round(no_edge, 4),
            msg="small-capital opening cannot rest both YES and NO",
        )
        return side

    def _small_capital_saved_repair_cap(self, state: dict, repair_side: str, min_edge: float) -> float | None:
        """Return max balancing bid from saved opening limit price, if known.

        Live wallet truth can observe shares before local CLOB fill history has
        the FIFO price. In that gap, the universal pair-cost guard sees no
        opposite fill and would allow an unsafe repair bid. The opening buy
        limit is a conservative cost-basis upper bound: a buy fill cannot be
        worse than our own limit.
        """
        return saved_repair_cap_from_state(state, repair_side, min_edge)

    def _small_capital_emergency_hedge_cap(self, state: dict, repair_side: str) -> tuple[float | None, bool, float]:
        """Return bounded-loss hedge cap after the emergency timer expires.

        Small-cap buy-only mode cannot sell wrong-side inventory. After waiting
        for the profitable balancing leg, this allows completing the pair at a
        bounded loss instead of risking the entire naked leg going to zero.
        """
        return emergency_hedge_cap_from_state(
            state,
            repair_side,
            config=getattr(self, "small_capital_config", None),
            now=_time.time(),
        )

    async def _wallet_position_truth(self, market: MarketInfo) -> tuple[float, float] | None:
        """Return authoritative YES/NO wallet balances for the current market.

        Local fills are still used for cost basis/P&L, but quote side selection
        should use wallet/ERC1155 balances when available because CLOB trade
        history can lag immediately after a fill.
        """
        yes_token_id = str(getattr(market, "token_id_up", "") or "")
        no_token_id = str(getattr(market, "token_id_down", "") or "")
        if not yes_token_id or not no_token_id:
            return None

        ctf_contract = None
        address = ""
        bm = getattr(self, "balance_monitor", None)
        if bm and getattr(bm, "_ctf", None) is not None and getattr(bm, "_address", ""):
            ctf_contract = bm._ctf
            address = bm._address
        elif self.ctf and getattr(self.ctf, "_ctf", None) is not None and getattr(self.ctf, "_account", None):
            ctf_contract = self.ctf._ctf
            address = self.ctf._account.address
        if ctf_contract is None or not address:
            return None

        try:
            yes_raw = int(ctf_contract.functions.balanceOf(address, int(yes_token_id)).call())
            no_raw = int(ctf_contract.functions.balanceOf(address, int(no_token_id)).call())
            return yes_raw / 1e6, no_raw / 1e6
        except Exception as e:
            log.warning("wallet_position_truth_error", asset=self.asset, error=str(e))
            return None

    async def _refresh_wallet_truth_for_market(self, market: MarketInfo) -> tuple[tuple[float, float] | None, dict | None]:
        wallet_truth = await self._wallet_position_truth(market)
        wallet_snapshot = self._wallet_truth_snapshot(wallet_truth)
        if wallet_snapshot is not None:
            self._wallet_truth_by_market[market.market_id] = wallet_snapshot
            self._apply_wallet_truth_to_small_capital_state(market.market_id, wallet_snapshot)
        return wallet_truth, wallet_snapshot

    def _small_capital_state(self, market_id: str) -> dict:
        sm = getattr(self.inventory, "state_manager", None)
        if sm and hasattr(sm, "get_small_capital_window"):
            return sm.get_small_capital_window(market_id)
        return {}

    def _save_small_capital_state(self, market_id: str, state: dict):
        sm = getattr(self.inventory, "state_manager", None)
        if sm and hasattr(sm, "update_small_capital_window"):
            sm.update_small_capital_window(market_id, state)

    def _repair_small_capital_unfilled_opening_state(self, market_id: str, state: dict, has_resting_opening_quote: bool, matched_pairs: int) -> bool:
        if (
            state.get("quote_cycle_started")
            and not has_resting_opening_quote
            and not state.get("initial_filled")
            and int(matched_pairs or 0) == 0
        ):
            retry_unfilled = bool(getattr(getattr(self, "small_capital_config", None), "retry_unfilled_opening", True))
            state["stale_quote_cycle_repaired"] = True
            state["initial_order_id"] = ""
            state["initial_side"] = ""
            if retry_unfilled:
                state["quote_cycle_started"] = False
                state["opening_attempt_spent"] = False
                state["initial_price"] = 0.0
                state["initial_yes_price"] = 0.0
                state["initial_no_price"] = 0.0
            self._save_small_capital_state(market_id, state)
            return True
        return False

    def _small_capital_should_hold_opening_quote(self, state: dict, has_resting_opening_quote: bool, matched_pairs: int) -> bool:
        return bool(
            self._small_capital_opening_spent(state)
            and has_resting_opening_quote
            and not state.get("initial_filled")
            and int(matched_pairs or 0) == 0
        )

    @staticmethod
    def _small_capital_opening_side(state: dict, active) -> str:
        side = str(state.get("initial_side") or "").lower()
        if side in ("yes", "up"):
            return "yes"
        if side in ("no", "down"):
            return "no"
        if bool(getattr(active, "yes_order_id", "")) and not bool(getattr(active, "no_order_id", "")):
            return "yes"
        if bool(getattr(active, "no_order_id", "")) and not bool(getattr(active, "yes_order_id", "")):
            return "no"
        return ""

    def _apply_small_capital_opening_reprice_guard(self, market_id: str, quotes, state: dict, min_order_size: int) -> str:
        """Constrain an unfilled opening reprice to the original side only.

        One-cycle small-cap mode should not freeze a stale opening bid forever.
        If the quote is still unfilled/resting, let OrderManager cancel/replace
        it to the latest target price, but only on the same opening side. This
        preserves the one-opening invariant while avoiding dead stale orders.
        """
        active = self.order_mgr.get_active(market_id)
        side = self._small_capital_opening_side(state, active)
        if side == "yes":
            quotes.no_buy_size = 0
            if quotes.yes_buy_price:
                quotes.yes_buy_size = max(int(quotes.yes_buy_size or 0), int(min_order_size or 0))
        elif side == "no":
            quotes.yes_buy_size = 0
            if quotes.no_buy_price:
                quotes.no_buy_size = max(int(quotes.no_buy_size or 0), int(min_order_size or 0))
        else:
            return ""
        log.info(
            "small_capital_opening_reprice_allowed",
            asset=self.asset,
            market=market_id[:8],
            side=side,
            current_yes_price=getattr(active, "yes_price", None),
            current_no_price=getattr(active, "no_price", None),
            target_yes_price=getattr(quotes, "yes_buy_price", None),
            target_no_price=getattr(quotes, "no_buy_price", None),
            msg="reprice same-side unfilled opening quote without opening extra exposure",
        )
        return side

    def _small_capital_opening_spent(self, state: dict) -> bool:
        return bool(state.get("opening_attempt_spent") or state.get("quote_cycle_started"))

    async def _small_capital_fail_closed_before_quotes(self, market: MarketInfo, pos, wallet_snapshot: dict | None, fv: float, sigma: float, remaining: float) -> bool:
        """Block all opening quote generation after small-cap opening is spent.

        This runs before directional/FV/reverse-move quote branches. Those
        branches can intentionally switch out of normal mode, so a late guard is
        not sufficient for one-cycle-per-window semantics.
        """
        if not self._small_capital_enabled():
            return False
        state = self._small_capital_state(market.market_id)
        if state.get("stopped_for_window"):
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0),
                                   fv or self.last_fair_value or 0, sigma or self.last_sigma or 0,
                                   "SMALL_CAP_DONE", remaining)
            return True
        if not self._small_capital_opening_spent(state):
            return False

        active = self.order_mgr.get_active(market.market_id)
        has_resting = bool(active.yes_order_id or active.no_order_id)
        wallet_imbalance = None
        wallet_pairs = 0
        if wallet_snapshot:
            wallet_imbalance = float(wallet_snapshot.get("share_imbalance", 0) or 0)
            wallet_pairs = int(float(wallet_snapshot.get("matched_pairs", 0) or 0))
        effective_imbalance = wallet_imbalance if wallet_imbalance is not None else float(pos.share_imbalance() or 0)
        effective_pairs = max(int(pos.matched_pairs() or 0), wallet_pairs)

        # If one side exists, allow the later balancing/repair logic. If both
        # sides are balanced, completion handler handles it first.
        if abs(effective_imbalance) > 0.0001:
            return False
        if effective_pairs > 0:
            return await self._small_capital_maybe_stop_completed(
                market, pos, "pre_quote_wallet_balanced", wallet_snapshot=wallet_snapshot
            )
        if has_resting and not state.get("initial_filled"):
            log.info(
                "small_capital_opening_quote_reprice_path_pre_generation",
                asset=self.asset,
                market=market.market_id[:8],
                msg="opening quote already spent; allowing same-side reprice path",
            )
            return False

        if not state.get("initial_filled") and bool(getattr(getattr(self, "small_capital_config", None), "retry_unfilled_opening", True)):
            # Compatibility/workflow restore: an opening order that was canceled
            # before any fill did not create market risk. Do not let stale
            # opening_attempt_spent state freeze the whole window before the
            # later quote-generation path can repair it.
            state["quote_cycle_started"] = False
            state["opening_attempt_spent"] = False
            state["initial_order_id"] = ""
            state["initial_side"] = ""
            state["initial_price"] = 0.0
            state["initial_yes_price"] = 0.0
            state["initial_no_price"] = 0.0
            state["stale_quote_cycle_repaired"] = True
            self._save_small_capital_state(market.market_id, state)
            log.warning(
                "small_capital_pre_generation_unfilled_retry_enabled",
                asset=self.asset,
                market=market.market_id[:8],
                msg="cleared stale opening-spent state with no fill/inventory; allowing opening quote retry",
            )
            self._set_dashboard_event(
                "info",
                "SMALL_CAP_RETRY_UNFILLED_OPENING",
                "cleared stale unfilled opening state; retrying",
            )
            return False

        log.warning(
            "small_capital_opening_spent_pre_generation",
            asset=self.asset,
            market=market.market_id[:8],
            msg="opening attempt already spent; blocking all new opening quotes this window",
        )
        self._set_dashboard_event("skip", "SMALL_CAP_OPENING_SPENT", "opening attempt already spent this window")
        await self.order_mgr.cancel_market_quotes(market.market_id)
        self._update_dashboard(market, getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0),
                               fv or self.last_fair_value or 0, sigma or self.last_sigma or 0,
                               "SMALL_CAP_WAIT_NEXT", remaining)
        return True

    def _mark_small_capital_quote_started(self, market: MarketInfo, quotes, repair_mode: str):
        """Persist that this window has spent its one opening quote cycle."""
        if not self._small_capital_enabled() or repair_mode != "normal":
            return
        if not ((quotes.yes_buy_size or 0) > 0 or (quotes.no_buy_size or 0) > 0):
            return
        state = self._small_capital_state(market.market_id)
        if state.get("quote_cycle_started"):
            return
        active = self.order_mgr.get_active(market.market_id)
        initial_order_id = active.yes_order_id or active.no_order_id or ""
        if not initial_order_id:
            log.warning(
                "small_capital_quote_cycle_not_marked",
                asset=self.asset,
                market=market.market_id[:8],
                msg="opening quote was generated but no live order id exists",
            )
            return
        state.update({
            "quote_cycle_started": True,
            "opening_attempt_spent": True,
            "quote_cycles_started": int(state.get("quote_cycles_started", 0) or 0) + 1,
            "initial_order_id": initial_order_id,
            "initial_side": "yes" if (quotes.yes_buy_size or 0) > 0 else "no",
            "slug": market.slug,
            "asset": self.asset,
        })
        if state["initial_side"] == "yes":
            state["initial_yes_price"] = float(getattr(quotes, "yes_buy_price", 0) or 0)
            state["initial_price"] = state["initial_yes_price"]
        else:
            state["initial_no_price"] = float(getattr(quotes, "no_buy_price", 0) or 0)
            state["initial_price"] = state["initial_no_price"]
        self._save_small_capital_state(market.market_id, state)
        log.info(
            "small_capital_quote_cycle_started",
            asset=self.asset,
            market=market.market_id[:8],
            quote_cycles_started=state["quote_cycles_started"],
            yes_size=quotes.yes_buy_size,
            no_size=quotes.no_buy_size,
        )

    def _small_capital_record_fills(self, market: MarketInfo, fills: list[dict]):
        if not self._small_capital_enabled() or not fills:
            return
        state = self._small_capital_state(market.market_id)
        if not state.get("quote_cycle_started"):
            return
        for fill in fills:
            side = str(fill.get("side") or "").lower()
            order_id = str(fill.get("order_id") or "")
            if side in ("up", "yes"):
                side = "yes"
            elif side in ("down", "no"):
                side = "no"
            if not state.get("initial_filled"):
                state["initial_filled"] = True
                state["initial_side"] = side
                state["initial_order_id"] = order_id
                state["initial_fill_ts"] = _time.time()
                fill_price = float(fill.get("price") or 0)
                if fill_price > 0:
                    if side == "yes":
                        state["initial_yes_price"] = fill_price
                    else:
                        state["initial_no_price"] = fill_price
                    state["initial_price"] = fill_price
            elif side and side != state.get("initial_side"):
                state["balancing_filled"] = True
                state["balancing_side"] = side
                state["balancing_order_id"] = order_id
        self._save_small_capital_state(market.market_id, state)

    async def _small_capital_maybe_stop_completed(self, market: MarketInfo, pos, reason: str = "", wallet_snapshot: dict | None = None) -> bool:
        """Stop quoting this window after the first balanced pair cycle completes."""
        if not self._small_capital_enabled():
            return False
        state = self._small_capital_state(market.market_id)
        if state.get("stopped_for_window"):
            await self.order_mgr.cancel_market_quotes(market.market_id)
            self._update_dashboard(market, getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0),
                                   self.last_fair_value or 0, self.last_sigma or 0,
                                   "SMALL_CAP_DONE", market.time_remaining)
            return True
        matched_pairs = int(pos.matched_pairs() or 0)
        imbalance = float(pos.share_imbalance() or 0)
        if wallet_snapshot:
            matched_pairs = int(float(wallet_snapshot.get("matched_pairs", matched_pairs) or 0))
            imbalance = float(wallet_snapshot.get("share_imbalance", imbalance) or 0)
        state_completed = bool(
            state.get("quote_cycle_started")
            and state.get("initial_filled")
            and state.get("balancing_filled")
        )
        inventory_completed = bool(matched_pairs > 0 and abs(imbalance) < 0.0001)
        if (state.get("quote_cycle_started")
                and getattr(self.small_capital_config, "stop_after_balanced_fill", True)
                and (state_completed or inventory_completed)):
            state["balancing_filled"] = True
            state["stopped_for_window"] = True
            state["stop_reason"] = reason or ("state_balanced_fill_complete" if state_completed else "balanced_fill_complete")
            self._save_small_capital_state(market.market_id, state)
            if getattr(self.small_capital_config, "cancel_remaining_orders_on_stop", True):
                await self.order_mgr.cancel_market_quotes(market.market_id)
            log.warning(
                "small_capital_window_complete",
                asset=self.asset,
                market=market.market_id[:8],
                slug=market.slug,
                matched_pairs=matched_pairs,
                reason=state["stop_reason"],
                inventory_source="wallet" if wallet_snapshot else "local",
            )
            self._update_dashboard(market, getattr(self.price_feed, 'prices', {}).get(self.ac.symbol, 0),
                                   self.last_fair_value or 0, self.last_sigma or 0,
                                   "SMALL_CAP_DONE", market.time_remaining)
            return True
        return False
