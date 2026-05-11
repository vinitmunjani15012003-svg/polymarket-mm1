"""
Polymarket CTF (Conditional Token Framework) operations.

On-chain operations for managing outcome tokens:
  - MERGE:  1 Up + 1 Down → $1 USDC (lock in pair profit mid-market)
  - REDEEM: After resolution, winning tokens → $1 USDC each
  - SPLIT:  $1 USDC → 1 Up + 1 Down (mint new tokens)

These interact directly with the CTF smart contract on Polygon,
NOT through the CLOB API (which only handles order placement).

Contract: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045 (Polygon)
Collateral: pUSD by default 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
"""

import asyncio
from typing import Optional
from src.monitoring.logger import get_logger
from src.execution.rpc_utils import pick_working_polygon_rpc

log = get_logger("ctf_ops")

# Polygon contract addresses
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Minimal ABIs for the CTF operations we need
CTF_ABI = [
    {
        "name": "mergePositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "splitPosition",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ERC20 approve ABI
ERC20_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# Standard binary partition: [1, 2] = [Up, Down]
BINARY_PARTITION = [1, 2]
# Zero parent collection for top-level positions
PARENT_COLLECTION_ID = b'\x00' * 32


class GaslessMerger:
    """
    Gasless merge/split via Polymarket's Builder Relayer Client.
    
    Uses the Polymarket relayer infrastructure to execute CTF operations
    (merge, split, redeem) without paying gas. Requires Builder Program
    credentials obtained from polymarket.com/settings?tab=builder.
    
    This is the PREFERRED method for live trading because:
      - Zero gas cost (relayer pays)
      - Faster execution (relayer has priority)
      - Same security (signed by your key)
    """

    # ABI fragments for encoding merge calls
    CTF_MERGE_ABI = [
        {
            "name": "mergePositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "partition", "type": "uint256[]"},
                {"name": "amount", "type": "uint256"},
            ],
            "outputs": [],
        }
    ]
    CTF_REDEEM_ABI = [
        {
            "name": "redeemPositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"},
            ],
            "outputs": [],
        }
    ]
    NEG_RISK_MERGE_ABI = [
        {
            "name": "mergePositions",
            "type": "function",
            "inputs": [
                {"name": "_conditionId", "type": "bytes32"},
                {"name": "_amount", "type": "uint256"},
            ],
            "outputs": [],
        }
    ]
    # Neg Risk Adapter address on Polygon
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

    def __init__(self, private_key: str,
                 builder_api_key: str = "",
                 builder_secret: str = "",
                 builder_passphrase: str = "",
                 relayer_url: str = "https://relayer-v2.polymarket.com",
                 chain_id: int = 137,
                 collateral_token: str = DEFAULT_COLLATERAL_TOKEN,
                 relayer_api_key: str = "",
                 relayer_api_key_address: str = ""):
        self._private_key = private_key
        self._builder_api_key = builder_api_key
        self._builder_secret = builder_secret
        self._builder_passphrase = builder_passphrase
        self._relayer_api_key = relayer_api_key
        self._relayer_api_key_address = relayer_api_key_address
        self._relayer_url = relayer_url
        self._chain_id = chain_id
        self._collateral_token = collateral_token or DEFAULT_COLLATERAL_TOKEN
        self._client = None
        self._w3 = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the gasless relayer client."""
        has_current_relayer_creds = all([
            self._relayer_api_key,
            self._relayer_api_key_address,
        ])
        has_legacy_builder_creds = all([
            self._builder_api_key,
            self._builder_secret,
            self._builder_passphrase,
        ])
        if not has_current_relayer_creds and not has_legacy_builder_creds:
            log.warning("gasless_no_builder_creds",
                        msg="Relayer credentials not configured. "
                            "Gasless merge unavailable.")
            return False

        try:
            from web3 import Web3
            self._w3 = Web3()  # Only for ABI encoding, no RPC needed

            from py_builder_relayer_client.client import RelayClient
            import inspect

            relay_sig = inspect.signature(RelayClient)
            if "relayer_api_key" in relay_sig.parameters:
                # Current docs flow: RELAYER_API_KEY + RELAYER_API_KEY_ADDRESS.
                self._client = RelayClient(
                    host=self._relayer_url,
                    chain=self._chain_id,
                    signer=self._private_key,
                    relayer_api_key=self._relayer_api_key,
                    relayer_api_key_address=self._relayer_api_key_address,
                )
                auth_mode = "relayer_api_key"
            else:
                # Backward-compatible path for older py-builder-relayer-client.
                from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

                builder_config = BuilderConfig(
                    local_builder_creds=BuilderApiKeyCreds(
                        key=self._builder_api_key,
                        secret=self._builder_secret,
                        passphrase=self._builder_passphrase,
                    )
                )
                self._client = RelayClient(
                    self._relayer_url,
                    self._chain_id,
                    self._private_key,
                    builder_config,
                )
                auth_mode = "legacy_builder_creds"
            self._initialized = True
            log.info("gasless_merger_initialized",
                     relayer=self._relayer_url,
                     auth_mode=auth_mode)
            return True

        except ImportError as e:
            log.warning("gasless_deps_missing",
                        msg="Install: pip install py-builder-relayer-client "
                            "py-builder-signing-sdk",
                        error=str(e))
            return False
        except Exception as e:
            log.error("gasless_init_error", error=str(e))
            return False

    async def merge_positions(self, condition_id: str, amount: int,
                               is_neg_risk: bool = False) -> Optional[str]:
        """
        Merge matched pairs via gasless relayer.
        
        1 Up + 1 Down → $1 USDC (zero gas cost).
        
        Args:
            condition_id: Market condition ID (hex string).
            amount: Number of pairs in token units (1 share = 10^6).
            is_neg_risk: Whether this is a neg-risk market.
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("gasless_not_initialized")
            return None

        try:
            condition_bytes = bytes.fromhex(
                condition_id.replace("0x", "")
            )

            if is_neg_risk:
                # Neg-risk markets use the NegRiskAdapter
                contract = self._w3.eth.contract(
                    address=self._w3.to_checksum_address(
                        self.NEG_RISK_ADAPTER
                    ),
                    abi=self.NEG_RISK_MERGE_ABI,
                )
                data = contract.encode_abi(
                    "mergePositions",
                    args=[condition_bytes, amount],
                )
                target = self.NEG_RISK_ADAPTER
            else:
                # Standard binary markets use ConditionalTokens
                contract = self._w3.eth.contract(
                    address=self._w3.to_checksum_address(CTF_CONTRACT),
                    abi=self.CTF_MERGE_ABI,
                )
                parent = bytes(32)  # Zero bytes32
                data = contract.encode_abi(
                    "mergePositions",
                    args=[
                        self._w3.to_checksum_address(self._collateral_token),
                        parent,
                        condition_bytes,
                        [1, 2],  # Binary partition
                        amount,
                    ],
                )
                target = CTF_CONTRACT

            # Execute via relayer (gasless)
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.execute(
                    [{"to": target, "data": data, "value": "0"}],
                    "Merge Positions",
                ),
            )

            tx_hash = (response if isinstance(response, str)
                       else str(response))
            log.info("gasless_merge_success",
                     condition=condition_id[:12],
                     pairs=amount // 10**6,
                     usdc_back=f"${amount / 1e6:.2f}",
                     tx=tx_hash[:16] if tx_hash else "submitted")
            return tx_hash

        except Exception as e:
            log.error("gasless_merge_error",
                      condition=condition_id[:12],
                      error=str(e))
            return None

    async def redeem_positions(self, condition_id: str) -> Optional[str]:
        """Redeem winning tokens via gasless Builder relayer."""
        if not self._initialized:
            log.error("gasless_not_initialized")
            return None

        try:
            from py_builder_relayer_client.models import OperationType, SafeTransaction

            condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            contract = self._w3.eth.contract(
                address=self._w3.to_checksum_address(CTF_CONTRACT),
                abi=self.CTF_REDEEM_ABI,
            )
            data = contract.encode_abi(
                "redeemPositions",
                args=[
                    self._w3.to_checksum_address(self._collateral_token),
                    bytes(32),
                    condition_bytes,
                    [1, 2],
                ],
            )

            tx = SafeTransaction(
                to=CTF_CONTRACT,
                operation=OperationType.Call,
                data=data,
                value="0",
            )

            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.execute([tx], "Redeem Positions"),
            )

            tx_hash = response if isinstance(response, str) else str(response)
            log.info("gasless_redeem_success",
                     condition=condition_id[:12],
                     tx=tx_hash[:16] if tx_hash else "submitted")
            return tx_hash

        except Exception as e:
            log.error("gasless_redeem_error",
                      condition=condition_id[:12],
                      error=str(e))
            return None

    @property
    def is_available(self) -> bool:
        return self._initialized


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
                               force: bool = False) -> dict:
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

            for market_id, pos in inventory_mgr.positions.items():
                pairs = int(pos.matched_pairs())
                if not force and pairs < self.min_merge_pairs:
                    continue

                condition_id = market_id
                usdc_recovery = pairs * 1.0  # 1 pair = $1 USDC
                pair_profit = pos.matched_pair_profit()
                amount = int(pairs * 1e6)

                log.info("auto_merge_market",
                         market=market_id[:12],
                         pairs=pairs,
                         expected_usdc=f"${usdc_recovery:.2f}",
                         pair_profit=f"${pair_profit:.4f}")

                # Try gasless first, then on-chain fallback
                tx = None
                if gasless_merger and gasless_merger.is_available:
                    tx = await gasless_merger.merge_positions(
                        condition_id, amount
                    )
                    if tx:
                        log.info("auto_merge_gasless_ok",
                                 market=market_id[:12],
                                 tx=str(tx)[:16])

                if not tx and ctf_ops:
                    tx = await ctf_ops.merge_positions(
                        condition_id, amount
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
                        pnl_tracker.record_settlement(
                            pair_profit, market_id
                        )
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

                    self._total_merged_usdc += usdc_recovery
                    self._total_merges += 1

            if total_pairs > 0:
                inventory_mgr._save_state()

            result["merged"] = total_pairs > 0
            result["pairs_merged"] = total_pairs
            result["usdc_recovered"] = total_usdc

            if total_pairs > 0 and hasattr(inventory_mgr, "save_state"):
                inventory_mgr.save_state()

            if total_pairs > 0:
                log.info("auto_merge_complete",
                         total_pairs=total_pairs,
                         usdc_recovered=f"${total_usdc:.2f}",
                         new_balance_est=f"${balance + total_usdc:.2f}",
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


class CTFOperations:
    """
    On-chain CTF operations for Polymarket.
    
    Requires:
      - web3.py installed
      - Private key with POL for gas on Polygon
      - USDC.e balance for split operations
      - Token balances for merge/redeem operations
    """

    def __init__(self, private_key: str,
                 rpc_url: str = "https://polygon-bor.publicnode.com",
                 collateral_token: str = DEFAULT_COLLATERAL_TOKEN,
                 dry_run: bool = True):
        """
        Args:
            private_key: Ethereum private key (0x-prefixed).
            rpc_url: Polygon RPC endpoint.
            dry_run: If True, simulate without sending transactions.
        """
        self._private_key = private_key
        self._rpc_url = rpc_url
        self._collateral_token = collateral_token or DEFAULT_COLLATERAL_TOKEN
        self._dry_run = dry_run
        self._w3 = None
        self._ctf = None
        self._usdc = None
        self._account = None
        self._initialized = False

    async def initialize(self):
        """Initialize web3 connection and contract instances."""
        try:
            from web3 import Web3

            rpc_candidates = [
                self._rpc_url,
                "https://polygon-bor.publicnode.com",
                "https://polygon.rpc.blxrbdn.com",
            ]
            self._w3, rpc, err = pick_working_polygon_rpc(Web3, rpc_candidates)
            if not self._w3:
                log.error("web3_not_connected", rpc=self._rpc_url, error=str(err) if err else "unknown")
                return False
            self._rpc_url = rpc or self._rpc_url

            self._account = self._w3.eth.account.from_key(self._private_key)
            self._ctf = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT),
                abi=CTF_ABI,
            )
            self._usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(self._collateral_token),
                abi=ERC20_APPROVE_ABI,
            )
            self._initialized = True

            log.info("ctf_initialized",
                     address=self._account.address,
                     dry_run=self._dry_run)
            return True

        except ImportError:
            log.error("web3_not_installed",
                      msg="Install with: pip install web3")
            return False
        except Exception as e:
            log.error("ctf_init_error", error=str(e))
            return False

    async def merge_positions(self, condition_id: str,
                               amount: int) -> Optional[str]:
        """
        Merge matched pairs: 1 Up + 1 Down → $1 USDC.
        
        This is the KEY profit-taking operation for a pair-matching MM.
        Call this when you have matched pairs to lock in guaranteed profit.
        
        Args:
            condition_id: The market's condition ID (bytes32 hex).
            amount: Number of pairs to merge (in token units, typically 10^6).
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("ctf_not_initialized")
            return None

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        if self._dry_run:
            log.info("dry_merge", condition=condition_id[:10],
                     pairs=amount, usdc_out=f"${amount / 1e6:.2f}")
            return f"DRY-MERGE-{condition_id[:8]}"

        try:
            tx = self._ctf.functions.mergePositions(
                self._w3.to_checksum_address(self._collateral_token),
                PARENT_COLLECTION_ID,
                condition_bytes,
                BINARY_PARTITION,
                amount,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 200_000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                log.info("merge_success",
                         tx_hash=tx_hash.hex()[:16],
                         pairs=amount,
                         usdc_out=f"${amount / 1e6:.2f}")
                return tx_hash.hex()
            else:
                log.error("merge_reverted", tx_hash=tx_hash.hex()[:16])
                return None

        except Exception as e:
            log.error("merge_error", error=str(e))
            return None

    async def redeem_positions(self, condition_id: str) -> Optional[str]:
        """
        Redeem winning tokens after market resolution.
        
        Call this after a market has resolved. Winning tokens are
        burned and USDC is returned.
        
        Args:
            condition_id: The resolved market's condition ID.
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("ctf_not_initialized")
            return None

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # Check if market is actually resolved
        try:
            payout_denom = self._ctf.functions.payoutDenominator(
                condition_bytes
            ).call()
            if payout_denom == 0:
                log.warning("market_not_resolved", condition=condition_id[:10])
                return None
        except Exception:
            pass

        if self._dry_run:
            log.info("dry_redeem", condition=condition_id[:10])
            return f"DRY-REDEEM-{condition_id[:8]}"

        try:
            tx = self._ctf.functions.redeemPositions(
                self._w3.to_checksum_address(self._collateral_token),
                PARENT_COLLECTION_ID,
                condition_bytes,
                BINARY_PARTITION,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 200_000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                log.info("redeem_success", tx_hash=tx_hash.hex()[:16],
                         condition=condition_id[:10])
                return tx_hash.hex()
            else:
                log.error("redeem_reverted", tx_hash=tx_hash.hex()[:16])
                return None

        except Exception as e:
            log.error("redeem_error", error=str(e))
            return None

    async def split_position(self, condition_id: str,
                              amount: int) -> Optional[str]:
        """
        Split USDC into Up + Down tokens.
        
        $1 USDC → 1 Up token + 1 Down token.
        Useful for providing initial liquidity or minting tokens to sell.
        
        Note: Requires prior USDC approval to CTF contract.
        
        Args:
            condition_id: The market's condition ID.
            amount: USDC amount in base units (10^6 = $1).
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("ctf_not_initialized")
            return None

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        if self._dry_run:
            log.info("dry_split", condition=condition_id[:10],
                     usdc_in=f"${amount / 1e6:.2f}")
            return f"DRY-SPLIT-{condition_id[:8]}"

        try:
            # Check and set USDC approval if needed
            await self._ensure_usdc_approval(amount)

            tx = self._ctf.functions.splitPosition(
                self._w3.to_checksum_address(self._collateral_token),
                PARENT_COLLECTION_ID,
                condition_bytes,
                BINARY_PARTITION,
                amount,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 250_000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                log.info("split_success", tx_hash=tx_hash.hex()[:16],
                         usdc_in=f"${amount / 1e6:.2f}")
                return tx_hash.hex()
            else:
                log.error("split_reverted", tx_hash=tx_hash.hex()[:16])
                return None

        except Exception as e:
            log.error("split_error", error=str(e))
            return None

    async def get_token_balance(self, token_id: int) -> int:
        """Get balance of a specific outcome token."""
        if not self._initialized:
            return 0
        try:
            balance = self._ctf.functions.balanceOf(
                self._account.address, token_id
            ).call()
            return balance
        except Exception as e:
            log.error("balance_error", error=str(e))
            return 0

    async def is_market_resolved(self, condition_id: str) -> bool:
        """Check if a market has been resolved."""
        if not self._initialized:
            return False
        try:
            condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            payout_denom = self._ctf.functions.payoutDenominator(
                condition_bytes
            ).call()
            return payout_denom > 0
        except Exception:
            return False

    async def _ensure_usdc_approval(self, amount: int):
        """Ensure CTF contract has USDC approval."""
        try:
            allowance = self._usdc.functions.allowance(
                self._account.address,
                self._w3.to_checksum_address(CTF_CONTRACT),
            ).call()

            if allowance < amount:
                # Approve max uint256
                max_approval = 2**256 - 1
                tx = self._usdc.functions.approve(
                    self._w3.to_checksum_address(CTF_CONTRACT),
                    max_approval,
                ).build_transaction({
                    "from": self._account.address,
                    "nonce": self._w3.eth.get_transaction_count(
                        self._account.address
                    ),
                    "gas": 60_000,
                    "gasPrice": self._w3.eth.gas_price,
                })
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(
                    signed.raw_transaction
                )
                self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                log.info("usdc_approved", tx_hash=tx_hash.hex()[:16])

        except Exception as e:
            log.error("approval_error", error=str(e))
            raise


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
        
    async def check_and_merge(self, inventory_mgr, gasless_merger=None, ctf_ops=None, pnl_tracker=None, force: bool = False) -> dict:
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
            
            if pnl_tracker and pair_profit > 0:
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
