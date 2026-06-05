"""Balance monitors for settlement/auto-merge flows."""

from __future__ import annotations

import asyncio
import time

from src.monitoring.logger import get_logger
from src.execution.rpc_utils import pick_working_polygon_rpc
from src.execution.settlement.collateral import infer_collateral_token_for_market
from src.execution.settlement.contracts import CTF_ABI, CTF_CONTRACT, DEFAULT_COLLATERAL_TOKEN

log = get_logger("ctf_ops")

class BalanceMonitor:
    """
    Monitors USDC wallet balance and auto-triggers merge of matched
    pairs when the balance drops below a configurable threshold.
    
    This prevents the bot from running out of capital to place new
    orders in live trading. When balance is low:
      1. Identifies all markets with mergeable matched pairs
      2. Merges via gasless relayer (preferred) or on-chain tx (fallback)
      3. Recovered USDC is immediately available for new orders
    
    Thresholds:
      - warn_balance:  Log a warning (e.g., $20)
      - merge_balance: Trigger auto-merge (e.g., $10)
      - min_merge_pairs: Don't merge fewer than N pairs (avoid dust)
    """

    def __init__(self,
                 private_key: str,
                 rpc_url: str = "https://polygon-bor.publicnode.com",
                 collateral_token: str = DEFAULT_COLLATERAL_TOKEN,
                 warn_balance: float = 20.0,
                 merge_balance: float = 10.0,
                 min_merge_pairs: int = 5,
                 check_interval: float = 30.0,
                 balance_address: str = ""):
        """
        Args:
            private_key: Wallet private key.
            rpc_url: Polygon RPC for balance checks.
            warn_balance: USDC balance to trigger warning.
            merge_balance: USDC balance to trigger auto-merge.
            min_merge_pairs: Minimum matched pairs to trigger merge.
            check_interval: Seconds between balance checks.
            balance_address: Optional wallet address whose collateral balance
                should be checked. Required for proxy/deposit-wallet modes where
                the signer EOA differs from the funded trading wallet.
        """
        self._private_key = private_key
        self._balance_address = balance_address
        self._rpc_url = rpc_url
        self._collateral_token = collateral_token or DEFAULT_COLLATERAL_TOKEN
        self.warn_balance = warn_balance
        self.merge_balance = merge_balance
        self.min_merge_pairs = min_merge_pairs
        self.check_interval = check_interval

        self._w3 = None
        self._usdc = None
        self._ctf = None
        self._address = None
        self._initialized = False
        self._last_check_ts = 0.0
        self._last_balance = 0.0
        self._merge_in_progress = False
        self._total_merged_usdc = 0.0
        self._total_merges = 0

    async def initialize(self) -> bool:
        """Initialize web3 connection for balance monitoring."""
        try:
            from web3 import Web3

            rpc_candidates = [
                self._rpc_url,
                "https://polygon-bor.publicnode.com",
                "https://polygon.rpc.blxrbdn.com",
            ]
            self._w3, rpc, err = pick_working_polygon_rpc(Web3, rpc_candidates)
            if not self._w3:
                log.error("balance_monitor_rpc_down", rpc=self._rpc_url, error=str(err) if err else "unknown")
                return False
            self._rpc_url = rpc or self._rpc_url

            signer_address = self._w3.eth.account.from_key(
                self._private_key
            ).address
            self._address = (
                self._w3.to_checksum_address(self._balance_address)
                if self._balance_address
                else signer_address
            )

            # USDC.e balance check ABI
            usdc_abi = [
                {
                    "name": "balanceOf",
                    "type": "function",
                    "inputs": [
                        {"name": "account", "type": "address"},
                    ],
                    "outputs": [{"name": "", "type": "uint256"}],
                }
            ]
            self._usdc = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self._collateral_token),
                abi=usdc_abi,
            )
            self._ctf = self._w3.eth.contract(
                address=self._w3.to_checksum_address(CTF_CONTRACT),
                abi=CTF_ABI,
            )
            self._initialized = True
            log.info("balance_monitor_initialized",
                     address=self._address,
                     signer_address=signer_address,
                     address_source="configured" if self._balance_address else "signer",
                     warn_at=f"${self.warn_balance:.2f}",
                     merge_at=f"${self.merge_balance:.2f}")
            return True

        except ImportError:
            log.warning("balance_monitor_no_web3",
                        msg="web3 not installed, balance monitoring disabled")
            return False
        except Exception as e:
            log.error("balance_monitor_init_error", error=str(e))
            return False

    async def get_usdc_balance(self) -> float:
        """Get current USDC.e balance in human-readable units."""
        if not self._initialized:
            return -1.0
        try:
            raw = self._usdc.functions.balanceOf(self._address).call()
            balance = raw / 1e6  # USDC has 6 decimals
            self._last_balance = balance
            return balance
        except Exception as e:
            log.error("balance_check_error", error=str(e))
            return self._last_balance

    async def check_and_merge(self, inventory_mgr,
                               gasless_merger=None,
                               ctf_ops=None,
                               pnl_tracker=None,
                               force: bool = False,
                               balance_sync=None) -> dict:
        """
        Check balance and auto-merge if running low.
        
        Called from the main quote loop. Returns a status dict.
        
        Args:
            inventory_mgr: InventoryManager with current positions.
            gasless_merger: GaslessMerger instance (preferred).
            ctf_ops: CTFOperations instance (fallback, uses gas).
            pnl_tracker: PnLTracker to record merge profit.
        """
        result = {
            "checked": False,
            "balance": self._last_balance,
            "merged": False,
            "pairs_merged": 0,
            "usdc_recovered": 0.0,
        }

        # Throttle checks
        if not force:
            now = time.time()
            if now - self._last_check_ts < self.check_interval:
                return result
            self._last_check_ts = now

        if not self._initialized or self._merge_in_progress:
            return result

        balance = await self.get_usdc_balance()
        result["checked"] = True
        result["balance"] = balance

        if balance < 0:
            return result  # Error getting balance

        # Warn level
        if balance < self.warn_balance:
            log.warning("low_balance_warning",
                        balance=f"${balance:.2f}",
                        threshold=f"${self.warn_balance:.2f}")

        # Merge trigger level
        if not force and balance >= self.merge_balance:
            return result  # Balance is fine

        # --- Auto-merge needed ---
        log.info("auto_merge_triggered",
                 balance=f"${balance:.2f}",
                 threshold=f"${self.merge_balance:.2f}")

        self._merge_in_progress = True
        try:
            total_pairs = 0
            total_usdc = 0.0
            merged_collaterals: set[str] = set()

            eligible_markets = 0
            for market_id, pos in inventory_mgr.positions.items():
                # Start with local inventory, then reconcile upward/downward
                # against ERC1155 balances below. Local fill state can lag CLOB
                # trade events; on-chain balances are the merge authority.
                pairs = int(pos.matched_pairs())

                condition_id = getattr(pos, "condition_id", None) or market_id

                # Live-safe preflight: local inventory can drift when fills are
                # delayed/skipped or state is restored. The CTF merge will
                # revert if requested amount exceeds either actual ERC1155
                # token balance, so cap merge size to on-chain YES/NO balances
                # for the deposit/funder wallet before submitting to relayer.
                yes_token_id = getattr(pos, "yes_token_id", "") or getattr(pos, "token_id_yes", "")
                no_token_id = getattr(pos, "no_token_id", "") or getattr(pos, "token_id_no", "")
                collateral_token = self._collateral_token
                deposit_wallet_merge = bool(
                    gasless_merger
                    and getattr(gasless_merger, "_signature_type", 0) == 3
                    and getattr(gasless_merger, "_funder", "")
                )
                if deposit_wallet_merge and (not yes_token_id or not no_token_id):
                    log.warning(
                        "auto_merge_skipped_missing_token_ids",
                        market=market_id[:12],
                        local_pairs=pairs,
                        condition=condition_id[:12] if condition_id else "",
                    )
                    continue

                if self._ctf is not None and yes_token_id and no_token_id:
                    try:
                        collateral_token = infer_collateral_token_for_market(
                            self._w3,
                            self._ctf,
                            condition_id,
                            yes_token_id,
                            no_token_id,
                            self._collateral_token,
                        )
                        if collateral_token.lower() != (self._collateral_token or "").lower():
                            log.warning(
                                "auto_merge_collateral_override",
                                market=market_id[:12],
                                condition=condition_id[:12],
                                configured=self._collateral_token,
                                inferred=collateral_token,
                            )
                        yes_raw = int(self._ctf.functions.balanceOf(self._address, int(yes_token_id)).call())
                        no_raw = int(self._ctf.functions.balanceOf(self._address, int(no_token_id)).call())
                        onchain_pairs = min(yes_raw, no_raw) // 1_000_000
                        log.info(
                            "auto_merge_onchain_preflight",
                            market=market_id[:12],
                            local_pairs=pairs,
                            onchain_pairs=int(onchain_pairs),
                            yes_balance=f"{yes_raw / 1e6:.2f}",
                            no_balance=f"{no_raw / 1e6:.2f}",
                        )
                        if onchain_pairs < pairs:
                            log.warning(
                                "auto_merge_capped_to_onchain_balance",
                                market=market_id[:12],
                                local_pairs=pairs,
                                onchain_pairs=int(onchain_pairs),
                                yes_balance=f"{yes_raw / 1e6:.2f}",
                                no_balance=f"{no_raw / 1e6:.2f}",
                            )
                            pairs = int(onchain_pairs)
                        elif onchain_pairs > pairs:
                            log.warning(
                                "auto_merge_expanded_to_onchain_balance",
                                market=market_id[:12],
                                local_pairs=pairs,
                                onchain_pairs=int(onchain_pairs),
                                yes_balance=f"{yes_raw / 1e6:.2f}",
                                no_balance=f"{no_raw / 1e6:.2f}",
                                msg="Local inventory is stale; merging all chain-confirmed matched pairs",
                            )
                            pairs = int(onchain_pairs)
                        if pairs <= 0:
                            log.warning(
                                "auto_merge_skipped_no_onchain_pairs",
                                market=market_id[:12],
                                local_pairs=int(pos.matched_pairs()),
                            )
                            continue
                    except Exception as e:
                        log.warning(
                            "auto_merge_onchain_preflight_failed",
                            market=market_id[:12],
                            error=str(e),
                        )
                        if deposit_wallet_merge:
                            continue

                if not force and pairs < self.min_merge_pairs:
                    continue
                if pairs <= 0:
                    continue
                eligible_markets += 1

                usdc_recovery = pairs * 1.0  # 1 pair = $1 USDC
                pair_profit = pos.matched_pair_profit()
                amount = int(pairs * 1e6)

                log.info("auto_merge_market",
                         market=market_id[:12],
                         collateral=collateral_token,
                         pairs=pairs,
                         expected_usdc=f"${usdc_recovery:.2f}",
                         pair_profit=f"${pair_profit:.4f}")

                # Try gasless first, then on-chain fallback
                tx = None
                if gasless_merger and gasless_merger.is_available:
                    tx = await gasless_merger.merge_positions(
                        condition_id, amount, collateral_token=collateral_token
                    )
                    if tx:
                        log.info("auto_merge_gasless_ok",
                                 market=market_id[:12],
                                 tx=str(tx)[:16])

                if not tx and ctf_ops:
                    tx = await ctf_ops.merge_positions(
                        condition_id, amount, collateral_token=collateral_token
                    )
                    if tx:
                        log.info("auto_merge_onchain_ok",
                                 market=market_id[:12],
                                 tx=str(tx)[:16])

                if tx:
                    total_pairs += pairs
                    total_usdc += usdc_recovery

                    # Record profit in P&L tracker
                    if pnl_tracker:
                        if hasattr(pnl_tracker, "record_pair_merge"):
                            pnl_tracker.record_pair_merge(pair_profit, market_id)
                        else:
                            pnl_tracker.record_settlement(pair_profit, market_id)
                        pnl_tracker.record_capital_recovery(usdc_recovery)
                    pos.acknowledge_settlement()

                    # Deduct merged pairs from inventory
                    avg_yes = pos.yes_avg_entry
                    avg_no = pos.no_avg_entry
                    pos.yes_shares -= pairs
                    pos.no_shares -= pairs
                    pos.yes_total_cost -= pairs * avg_yes
                    pos.no_total_cost -= pairs * avg_no
                    # Clamp to zero to avoid negative dust
                    pos.yes_shares = max(0, pos.yes_shares)
                    pos.no_shares = max(0, pos.no_shares)
                    pos.yes_total_cost = max(0, pos.yes_total_cost)
                    pos.no_total_cost = max(0, pos.no_total_cost)

                    merged_collaterals.add(collateral_token)
                    self._total_merged_usdc += usdc_recovery
                    self._total_merges += 1

            if eligible_markets == 0:
                log.warning(
                    "auto_merge_no_eligible_pairs",
                    force=force,
                    min_merge_pairs=self.min_merge_pairs,
                    markets=len(getattr(inventory_mgr, "positions", {}) or {}),
                )

            if total_pairs > 0:
                inventory_mgr._save_state()

            result["merged"] = total_pairs > 0
            result["pairs_merged"] = total_pairs
            result["usdc_recovered"] = total_usdc

            if total_pairs > 0 and hasattr(inventory_mgr, "save_state"):
                inventory_mgr.save_state()

            if total_pairs > 0:
                self._last_balance = balance + total_usdc
                if gasless_merger and getattr(gasless_merger, "_signature_type", 0) == 3:
                    for merged_collateral in sorted(merged_collaterals or {self._collateral_token}):
                        await gasless_merger.ensure_deposit_wallet_trading_approvals(
                            collateral_token=merged_collateral,
                        )

                if callable(balance_sync):
                    # Wait for the CLOB indexer to catch up with the on-chain
                    # state after merge. Without this delay, update_balance_allowance
                    # often returns before the indexer has seen the new USDC.e balance,
                    # leaving a "redeem merge balance" popup instead of auto-crediting.
                    await asyncio.sleep(3)
                    sync_ok = False
                    for attempt in range(1, 6):
                        try:
                            sync_ok = bool(await balance_sync())
                        except Exception as e:
                            log.warning("post_merge_balance_sync_error",
                                        attempt=attempt, error=str(e))
                            sync_ok = False
                        if sync_ok:
                            break
                        await asyncio.sleep(min(2 * attempt, 8))
                    if sync_ok:
                        log.info("post_merge_balance_allowance_synced",
                                 attempts=attempt)
                    else:
                        log.warning("post_merge_balance_allowance_sync_failed",
                                    attempts=attempt)

                # Refresh the actual wallet balance after relayer/indexer sync.
                # The optimistic estimate is useful immediately, but dashboard
                # and sizing should converge to on-chain USDC as soon as RPC can
                # see it.
                refreshed_balance = self._last_balance
                for attempt in range(1, 5):
                    refreshed_balance = await self.get_usdc_balance()
                    if refreshed_balance >= max(0.0, balance + total_usdc - 0.01):
                        break
                    await asyncio.sleep(min(2 * attempt, 6))

                log.info("auto_merge_complete",
                         total_pairs=total_pairs,
                         usdc_recovered=f"${total_usdc:.2f}",
                         new_balance_est=f"${balance + total_usdc:.2f}",
                         wallet_balance=f"${refreshed_balance:.2f}",
                         lifetime_merged=f"${self._total_merged_usdc:.2f}",
                         lifetime_count=self._total_merges)

        except Exception as e:
            log.error("auto_merge_error", error=str(e))
        finally:
            self._merge_in_progress = False

        return result

    @property
    def stats(self) -> dict:
        return {
            "last_balance": self._last_balance,
            "total_merged_usdc": self._total_merged_usdc,
            "total_merges": self._total_merges,
            "initialized": self._initialized,
        }


class SimulatedBalanceMonitor:
    """Simulated Balance Monitor for Dry-Run Mode. Uses current_capital from PnLTracker."""
    def __init__(self, warn_balance: float = 20.0, merge_balance: float = 10.0,
                 min_merge_pairs: int = 5, check_interval: float = 30.0):
        self.warn_balance = warn_balance
        self.merge_balance = merge_balance
        self.min_merge_pairs = min_merge_pairs
        self.check_interval = check_interval
        self._last_check_ts = 0.0
        self._total_merged_usdc = 0.0
        self._total_merges = 0
        self._last_balance = 0.0
        self._merge_message = ""
        
    async def initialize(self) -> bool:
        return True
        
    async def check_and_merge(self, inventory_mgr, gasless_merger=None, ctf_ops=None, pnl_tracker=None, force: bool = False, balance_sync=None) -> dict:
        result = { "checked": False, "balance": 0.0, "merged": False, "pairs_merged": 0, "usdc_recovered": 0.0 }
        if not pnl_tracker: return result
        
        import time
        if not force:
            now = time.time()
            if now - self._last_check_ts < self.check_interval: return result
            self._last_check_ts = now
        
        balance = pnl_tracker.current_capital
        self._last_balance = balance
        result["checked"] = True
        result["balance"] = balance
        
        if not force and balance >= self.merge_balance: return result
        
        import asyncio
        import random
        
        # Simulate network/relayer latency for the merge (1.2 - 2.8 seconds)
        latency_sec = 0.0
        if not force:
            latency_sec = round(random.uniform(1.2, 2.8), 2)
            await asyncio.sleep(latency_sec)
        
        total_pairs = 0
        total_usdc = 0.0
        
        for market_id, pos in inventory_mgr.positions.items():
            pairs = int(pos.matched_pairs())
            if not force and pairs < self.min_merge_pairs: continue
            
            usdc_recovery = pairs * 1.0
            pair_profit = pos.matched_pair_profit()
            
            total_pairs += pairs
            total_usdc += usdc_recovery
            
            if pnl_tracker:
                if hasattr(pnl_tracker, "record_pair_merge"):
                    pnl_tracker.record_pair_merge(pair_profit, market_id)
                else:
                    pnl_tracker.record_settlement(pair_profit, market_id)
            if pnl_tracker:
                pnl_tracker.record_capital_recovery(usdc_recovery)
            pos.acknowledge_settlement()
                
            avg_yes = pos.yes_avg_entry
            avg_no = pos.no_avg_entry
            pos.yes_shares -= pairs
            pos.no_shares -= pairs
            pos.yes_total_cost -= pairs * avg_yes
            pos.no_total_cost -= pairs * avg_no
            pos.yes_shares = max(0, pos.yes_shares)
            pos.no_shares = max(0, pos.no_shares)
            pos.yes_total_cost = max(0, pos.yes_total_cost)
            pos.no_total_cost = max(0, pos.no_total_cost)
            
            self._total_merged_usdc += usdc_recovery
            self._total_merges += 1
            
        result["merged"] = total_pairs > 0
        result["pairs_merged"] = total_pairs
        result["usdc_recovered"] = total_usdc
        
        if total_pairs > 0:
            self._last_balance += total_usdc
            self._merge_message = f"Merged {total_pairs} pairs | +${total_usdc:.2f} [{latency_sec}s lat]"
            log.info("simulated_auto_merge", pairs=total_pairs, usdc=total_usdc, latency=latency_sec)
            
        return result

    @property
    def stats(self) -> dict:
        return {
            "last_balance": self._last_balance,
            "total_merged_usdc": self._total_merged_usdc,
            "total_merges": self._total_merges,
            "initialized": True,
            "merge_message": getattr(self, '_merge_message', "")
        }
