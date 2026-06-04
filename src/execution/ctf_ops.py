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
import json
import time
from typing import Optional

from src.monitoring.logger import get_logger
from src.execution.rpc_utils import pick_working_polygon_rpc
from src.execution.settlement.collateral import infer_collateral_token_for_market
from src.execution.settlement.contracts import (
    BINARY_PARTITION,
    CLOB_EXCHANGE,
    CTF_ABI,
    CTF_COLLATERAL_ADAPTER,
    CTF_CONTRACT,
    CTF_EXCHANGE_V2,
    DEFAULT_COLLATERAL_TOKEN,
    ERC1155_APPROVAL_ABI,
    ERC20_APPROVE_ABI,
    MAX_UINT256,
    NEG_RISK_ADAPTER,
    NEG_RISK_CLOB_EXCHANGE,
    NEG_RISK_CTF_EXCHANGE_V2,
    PARENT_COLLECTION_ID,
    USDC_E_COLLATERAL_TOKEN,
)
from src.execution.settlement.balance_monitor import BalanceMonitor, SimulatedBalanceMonitor

log = get_logger("ctf_ops")


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
    # Relayer proxy/deposit-wallet contract config on Polygon. These match the
    # official relayer/deposit-wallet docs and current py-builder-relayer-client
    # chain config. Proxy wallets (signature_type=1) must be executed through
    # the proxy factory; sending those calls through the Safe path derives the
    # wrong wallet and auto-merge never reaches the tokens.
    PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
    RELAY_HUB = "0xD216153c06E857cD7f72665E0aF1d7D82172F494"
    DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
    DEFAULT_PROXY_GAS_LIMIT = 500_000

    def __init__(self, private_key: str,
                 builder_api_key: str = "",
                 builder_secret: str = "",
                 builder_passphrase: str = "",
                 relayer_url: str = "https://relayer-v2.polymarket.com",
                 chain_id: int = 137,
                 collateral_token: str = DEFAULT_COLLATERAL_TOKEN,
                 relayer_api_key: str = "",
                 relayer_api_key_address: str = "",
                 funder: str = "",
                 signature_type: int = 0,
                 owner_private_key: str = ""):
        self._private_key = private_key
        # For deposit wallets, the relayer requires the wallet OWNER's
        # signature for EIP-712 batches. If the trading key (used for
        # CLOB API auth) is different from the owner, set owner_private_key.
        self._owner_key = owner_private_key or private_key
        self._builder_api_key = builder_api_key
        self._builder_secret = builder_secret
        self._builder_passphrase = builder_passphrase
        self._relayer_api_key = relayer_api_key
        self._relayer_api_key_address = relayer_api_key_address
        self._relayer_url = relayer_url
        self._chain_id = chain_id
        self._funder = funder
        self._signature_type = int(signature_type or 0)
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
                     auth_mode=auth_mode,
                     wallet_mode=("deposit_wallet" if self._signature_type == 3 and self._funder else "safe"))
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
                               is_neg_risk: bool = False,
                               collateral_token: str = "") -> Optional[str]:
        """
        Merge matched pairs via gasless relayer.
        
        1 Up + 1 Down → $1 USDC (zero gas cost).
        
        Args:
            condition_id: Market condition ID (hex string).
            amount: Number of pairs in token units (1 share = 10^6).
            is_neg_risk: Whether this is a neg-risk market.
            collateral_token: Optional collateral override for this market.
            
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
                        self._w3.to_checksum_address(
                            collateral_token or self._collateral_token
                        ),
                        parent,
                        condition_bytes,
                        [1, 2],  # Binary partition
                        amount,
                    ],
                )
                target = CTF_CONTRACT

            # Proxy/deposit-wallet accounts hold positions in the configured
            # funder wallet, not in the relayer SDK's derived Safe. The older
            # installed Python relayer client only supports Safe execution, so
            # submit the official raw Relayer API request for those wallet
            # modes. This fixes `expected safe ... is not deployed` and, more
            # importantly, executes mergePositions from the wallet that actually
            # owns the outcome tokens.
            if self._signature_type == 1 and self._funder:
                response = await self._execute_proxy_wallet_call(
                    target=target,
                    data=data,
                    metadata="Merge Positions",
                )
            elif self._signature_type == 3 and self._funder:
                response = await self._execute_deposit_wallet_call(
                    target=target,
                    data=data,
                    metadata="Merge Positions",
                )
            else:
                # Legacy Safe/proxy path via the installed SDK.
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

    async def _execute_deposit_wallet_call(self, target: str, data: str, metadata: str = ""):
        """Submit one gasless call through Polymarket's deposit-wallet relayer flow."""
        import requests
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        signer = Account.from_key(self._owner_key)
        from_address = signer.address
        wallet = self._w3.to_checksum_address(self._funder)
        deadline = str(int(time.time()) + 300)

        def _get_json(path: str, params: dict | None = None):
            resp = requests.get(f"{self._relayer_url}{path}", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

        nonce_payload = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _get_json("/nonce", {"address": from_address, "type": "WALLET"}),
        )
        nonce = str(nonce_payload.get("nonce"))
        if not nonce or nonce == "None":
            raise RuntimeError(f"invalid deposit wallet nonce payload: {nonce_payload}")

        calls = [{
            "target": self._w3.to_checksum_address(target),
            "value": "0",
            "data": data,
        }]
        domain = {
            "name": "DepositWallet",
            "version": "1",
            "chainId": self._chain_id,
            "verifyingContract": wallet,
        }
        types = {
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        }
        message = {
            "wallet": wallet,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": calls,
        }
        signable = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
        signature = Account.sign_message(signable, self._owner_key).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        payload = {
            "type": "WALLET",
            "from": from_address,
            "to": self.DEPOSIT_WALLET_FACTORY,
            "nonce": nonce,
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": wallet,
                "deadline": deadline,
                "calls": calls,
            },
        }
        if metadata:
            payload["metadata"] = metadata

        body = json.dumps(payload, separators=(",", ":"))
        headers = self._relayer_auth_headers(body)

        def _post():
            resp = requests.post(
                f"{self._relayer_url}/submit",
                headers=headers,
                data=body,
                timeout=20,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"deposit wallet relayer submit failed {resp.status_code}: {resp.text[:300]}")
            return resp.json()

        response = await asyncio.get_event_loop().run_in_executor(None, _post)
        tx_id = response.get("transactionID") or response.get("transactionId")
        state = response.get("state")
        log.info(
            "deposit_wallet_relayer_submitted",
            wallet=wallet,
            tx_id=tx_id,
            state=state,
        )
        if tx_id:
            await self._wait_for_relayer_finality(tx_id)
        return tx_id or response

    async def ensure_deposit_wallet_trading_approvals(
        self,
        collateral_token: str = USDC_E_COLLATERAL_TOKEN,
        spenders: list[str] | None = None,
    ) -> bool:
        """Approve Polymarket trading contracts from the deposit wallet.

        `update_balance_allowance` only refreshes CLOB's indexed view. The UI's
        "Activate Funds" prompt is an actual ERC20 approval requirement. For
        deposit wallets, that approval must be submitted as a relayer WALLET
        batch from the deposit wallet, not signed by the owner EOA directly.

        Polymarket's activation flow covers both asset classes: collateral
        allowance for BUY orders and CTF ERC1155 operator approval for outcome
        tokens. Approving only ERC20 can still leave the account in an
        "activation required" state in the app/API, because CLOB balance checks
        track the full trading-approval tuple, not merely the returned USDC.e
        balance from a merge. Annoying, but at least deterministic.
        """
        if not self._initialized:
            log.warning("deposit_wallet_approval_skipped", reason="gasless_not_initialized")
            return False
        if self._signature_type != 3 or not self._funder:
            return True

        spenders = spenders or [
            CLOB_EXCHANGE,
            NEG_RISK_CLOB_EXCHANGE,
            CTF_EXCHANGE_V2,
            NEG_RISK_CTF_EXCHANGE_V2,
            NEG_RISK_ADAPTER,
        ]

        try:
            token = self._w3.eth.contract(
                address=self._w3.to_checksum_address(collateral_token),
                abi=ERC20_APPROVE_ABI,
            )
            ctf = self._w3.eth.contract(
                address=self._w3.to_checksum_address(CTF_CONTRACT),
                abi=ERC1155_APPROVAL_ABI,
            )
            calls = []
            for spender in dict.fromkeys([s for s in spenders if s]):
                spender_addr = self._w3.to_checksum_address(spender)
                erc20_data = token.encode_abi(
                    "approve",
                    args=[spender_addr, MAX_UINT256],
                )
                calls.append({
                    "target": self._w3.to_checksum_address(collateral_token),
                    "value": "0",
                    "data": erc20_data,
                })
                erc1155_data = ctf.encode_abi(
                    "setApprovalForAll",
                    args=[spender_addr, True],
                )
                calls.append({
                    "target": self._w3.to_checksum_address(CTF_CONTRACT),
                    "value": "0",
                    "data": erc1155_data,
                })
            if not calls:
                return True
            tx = await self._execute_deposit_wallet_batch(
                calls,
                metadata="Activate Trading Funds",
            )
            ok = bool(tx)
            log.info(
                "deposit_wallet_trading_approvals_submitted" if ok else "deposit_wallet_trading_approvals_failed",
                collateral=collateral_token,
                spenders=len(spenders),
                calls=len(calls),
                tx=str(tx)[:16] if tx else "",
            )
            return ok
        except Exception as e:
            log.error("deposit_wallet_trading_approval_error", error=str(e))
            return False

    async def _execute_deposit_wallet_batch(self, calls: list[dict], metadata: str = ""):
        """Submit an arbitrary deposit-wallet batch through the relayer."""
        import requests
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        signer = Account.from_key(self._owner_key)
        from_address = signer.address
        wallet = self._w3.to_checksum_address(self._funder)
        deadline = str(int(time.time()) + 300)

        def _get_json(path: str, params: dict | None = None):
            resp = requests.get(f"{self._relayer_url}{path}", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

        nonce_payload = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _get_json("/nonce", {"address": from_address, "type": "WALLET"}),
        )
        nonce = str(nonce_payload.get("nonce"))
        if not nonce or nonce == "None":
            raise RuntimeError(f"invalid deposit wallet nonce payload: {nonce_payload}")

        normalized_calls = [
            {
                "target": self._w3.to_checksum_address(call["target"]),
                "value": str(call.get("value", "0")),
                "data": call["data"],
            }
            for call in calls
        ]
        domain = {
            "name": "DepositWallet",
            "version": "1",
            "chainId": self._chain_id,
            "verifyingContract": wallet,
        }
        types = {
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        }
        message = {
            "wallet": wallet,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": normalized_calls,
        }
        signable = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
        signature = Account.sign_message(signable, self._owner_key).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        payload = {
            "type": "WALLET",
            "from": from_address,
            "to": self.DEPOSIT_WALLET_FACTORY,
            "nonce": nonce,
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": wallet,
                "deadline": deadline,
                "calls": normalized_calls,
            },
        }
        if metadata:
            payload["metadata"] = metadata

        body = json.dumps(payload, separators=(",", ":"))
        headers = self._relayer_auth_headers(body)

        def _post():
            resp = requests.post(
                f"{self._relayer_url}/submit",
                headers=headers,
                data=body,
                timeout=20,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"deposit wallet relayer batch failed {resp.status_code}: {resp.text[:300]}")
            return resp.json()

        response = await asyncio.get_event_loop().run_in_executor(None, _post)
        tx_id = response.get("transactionID") or response.get("transactionId")
        log.info(
            "deposit_wallet_batch_submitted",
            wallet=wallet,
            tx_id=tx_id,
            state=response.get("state"),
            calls=len(normalized_calls),
        )
        if tx_id:
            await self._wait_for_relayer_finality(tx_id)
        return tx_id or response

    async def _execute_proxy_wallet_call(self, target: str, data: str, metadata: str = ""):
        """Submit one gasless call through Polymarket's PROXY relayer flow."""
        import requests
        from eth_abi import encode
        from eth_account import Account
        from eth_account.messages import encode_defunct
        from eth_utils import keccak, to_bytes
        from hexbytes import HexBytes

        signer = Account.from_key(self._private_key)
        from_address = signer.address
        proxy_wallet = self._w3.to_checksum_address(self._funder)
        proxy_factory = self._w3.to_checksum_address(self.PROXY_FACTORY)
        relay_hub = self._w3.to_checksum_address(self.RELAY_HUB)

        def _get_json(path: str, params: dict | None = None):
            resp = requests.get(f"{self._relayer_url}{path}", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

        relay_payload = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _get_json("/relay-payload", {"address": from_address, "type": "PROXY"}),
        )
        nonce = str(relay_payload.get("nonce"))
        relay = relay_payload.get("address")
        if not nonce or nonce == "None" or not relay:
            raise RuntimeError(f"invalid proxy relay payload: {relay_payload}")
        relay = self._w3.to_checksum_address(relay)

        target = self._w3.to_checksum_address(target)
        call_data = to_bytes(hexstr=data if data.startswith("0x") else "0x" + data)
        selector = keccak(b"proxy((uint8,address,uint256,bytes)[])")[:4]
        encoded_args = encode(
            ["(uint8,address,uint256,bytes)[]"],
            [[(1, target, 0, call_data)]],  # CallType.Call = 1
        )
        proxy_data = "0x" + (selector + encoded_args).hex()

        gas_limit = str(self.DEFAULT_PROXY_GAS_LIMIT)
        gas_price = "0"
        relayer_fee = "0"

        # Current relayer client signs keccak256("rlx:" || fields...) as an
        # EIP-191 defunct message. Keep this wire-compatible with the official
        # Python builder-relayer implementation while avoiding a hard dependency
        # on a newer client version at runtime.
        message = (
            b"rlx:"
            + HexBytes(from_address)
            + HexBytes(proxy_factory)
            + to_bytes(hexstr=proxy_data)
            + int(relayer_fee).to_bytes(32, "big")
            + int(gas_price).to_bytes(32, "big")
            + int(gas_limit).to_bytes(32, "big")
            + int(nonce).to_bytes(32, "big")
            + HexBytes(relay_hub)
            + HexBytes(relay)
        )
        struct_hash = "0x" + keccak(message).hex()
        signature = Account.sign_message(
            encode_defunct(HexBytes(struct_hash)),
            self._private_key,
        ).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        payload = {
            "type": "PROXY",
            "from": from_address,
            "to": proxy_factory,
            "proxyWallet": proxy_wallet,
            "data": proxy_data,
            "nonce": nonce,
            "signature": signature,
            "signatureParams": {
                "gasPrice": gas_price,
                "gasLimit": gas_limit,
                "relayerFee": relayer_fee,
                "relayHub": relay_hub,
                "relay": relay,
            },
        }
        if metadata:
            payload["metadata"] = metadata

        body = json.dumps(payload, separators=(",", ":"))
        headers = self._relayer_auth_headers(body)

        def _post():
            resp = requests.post(
                f"{self._relayer_url}/submit",
                headers=headers,
                data=body,
                timeout=20,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"proxy relayer submit failed {resp.status_code}: {resp.text[:300]}")
            return resp.json()

        response = await asyncio.get_event_loop().run_in_executor(None, _post)
        tx_id = response.get("transactionID") or response.get("transactionId")
        log.info(
            "proxy_relayer_submitted",
            proxy_wallet=proxy_wallet,
            tx_id=tx_id,
            state=response.get("state"),
        )
        if tx_id:
            await self._wait_for_relayer_finality(tx_id)
        return tx_id or response

    async def _wait_for_relayer_finality(self, tx_id: str, max_polls: int = 30,
                                         poll_seconds: float = 2.0) -> bool:
        """Wait until the relayer tx is mined/confirmed before CLOB balance sync.

        `/submit` often returns STATE_EXECUTED before the CLOB balance service can
        see the merged collateral. Syncing immediately leaves the UI/API showing
        “activate fund” until a manual refresh. Waiting for finality makes the
        following update_balance_allowance call useful instead of decorative.
        """
        import requests

        terminal_ok = {"STATE_MINED", "STATE_CONFIRMED"}
        terminal_bad = {"STATE_FAILED", "STATE_INVALID"}

        def _get_tx():
            resp = requests.get(
                f"{self._relayer_url}/transaction",
                params={"id": tx_id},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()

        last_state = ""
        for attempt in range(1, max_polls + 1):
            try:
                payload = await asyncio.get_event_loop().run_in_executor(None, _get_tx)
                tx = payload[0] if isinstance(payload, list) and payload else payload
                state = str((tx or {}).get("state") or "")
                last_state = state or last_state
                if state in terminal_ok:
                    log.info("relayer_tx_finalized", tx_id=tx_id, state=state, attempts=attempt)
                    return True
                if state in terminal_bad:
                    log.error("relayer_tx_failed", tx_id=tx_id, state=state, attempts=attempt)
                    return False
            except Exception as e:
                log.warning("relayer_tx_poll_error", tx_id=tx_id, attempt=attempt, error=str(e))
            await asyncio.sleep(poll_seconds)

        log.warning("relayer_tx_finality_timeout", tx_id=tx_id, last_state=last_state)
        return False

    def _relayer_auth_headers(self, body: str) -> dict:
        """Build relayer auth headers for raw `/submit` requests."""
        headers = {"Content-Type": "application/json"}
        if self._relayer_api_key and self._relayer_api_key_address:
            headers.update({
                "RELAYER_API_KEY": self._relayer_api_key,
                "RELAYER_API_KEY_ADDRESS": self._relayer_api_key_address,
            })
        elif getattr(self._client, "builder_config", None) is not None:
            builder_headers = self._client.builder_config.generate_builder_headers(
                "POST", "/submit", body
            )
            if builder_headers is not None:
                headers.update(builder_headers.to_dict())
        return headers

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
                               amount: int,
                               collateral_token: str = "") -> Optional[str]:
        """
        Merge matched pairs: 1 Up + 1 Down → $1 USDC.
        
        This is the KEY profit-taking operation for a pair-matching MM.
        Call this when you have matched pairs to lock in guaranteed profit.
        
        Args:
            condition_id: The market's condition ID (bytes32 hex).
            amount: Number of pairs to merge (in token units, typically 10^6).
            collateral_token: Optional collateral override for this market.
            
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
                self._w3.to_checksum_address(collateral_token or self._collateral_token),
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

