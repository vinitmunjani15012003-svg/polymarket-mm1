import importlib
import subprocess
import sys

from src.execution.settlement.collateral import infer_collateral_token_for_market
from src.execution.settlement.contracts import DEFAULT_COLLATERAL_TOKEN, USDC_E_COLLATERAL_TOKEN
from src.execution.settlement.relayer import (
    DEPOSIT_WALLET_BATCH_TYPES,
    DEPOSIT_WALLET_FACTORY,
    compact_json,
    deposit_wallet_domain,
    deposit_wallet_message,
    deposit_wallet_submit_payload,
    normalize_relayer_call,
)
from src.execution.settlement.settlement_manager import SettlementManager


class _Call:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def call(self):
        if self.error:
            raise self.error
        return self.value


class _Functions:
    def __init__(self, mapping=None, error=None):
        self.mapping = mapping or {}
        self.error = error

    def getCollectionId(self, parent, condition, index):
        if self.error:
            return _Call(error=self.error)
        return _Call(f"collection-{index}".encode())

    def getPositionId(self, collateral, collection_id):
        return _Call(self.mapping[(collateral.lower(), collection_id)])


class _CTF:
    def __init__(self, mapping=None, error=None):
        self.functions = _Functions(mapping, error)


class _W3:
    def to_checksum_address(self, address):
        return address.lower()


def test_collateral_inference_falls_back_without_enough_token_ids():
    assert infer_collateral_token_for_market(_W3(), _CTF(), "0x" + "11" * 32, yes_token_id="1") == DEFAULT_COLLATERAL_TOKEN


def test_collateral_inference_falls_back_on_contract_error():
    assert infer_collateral_token_for_market(
        _W3(), _CTF(error=RuntimeError("rpc down")), "0x" + "11" * 32, "1", "2"
    ) == DEFAULT_COLLATERAL_TOKEN


def test_collateral_inference_uses_derived_market_token_ids():
    mapping = {
        (DEFAULT_COLLATERAL_TOKEN.lower(), b"collection-1"): "101",
        (DEFAULT_COLLATERAL_TOKEN.lower(), b"collection-2"): "102",
        (USDC_E_COLLATERAL_TOKEN.lower(), b"collection-1"): "201",
        (USDC_E_COLLATERAL_TOKEN.lower(), b"collection-2"): "202",
    }

    inferred = infer_collateral_token_for_market(
        _W3(), _CTF(mapping), "0x" + "11" * 32, yes_token_id="201", no_token_id="202"
    )

    assert inferred == USDC_E_COLLATERAL_TOKEN.lower()


def test_settlement_manager_prefers_available_gasless_merger():
    calls = []

    class Gasless:
        is_available = True

        async def merge_positions(self, condition_id, amount, **kwargs):
            calls.append(("gasless", condition_id, amount, kwargs))
            return "gasless-tx"

    class CTF:
        async def merge_positions(self, condition_id, amount, **kwargs):
            calls.append(("ctf", condition_id, amount, kwargs))
            return "ctf-tx"

    import asyncio

    result = asyncio.run(SettlementManager(ctf_ops=CTF(), gasless_merger=Gasless()).merge("cond", 7, collateral_token="tok"))

    assert result == "gasless-tx"
    assert calls == [("gasless", "cond", 7, {"collateral_token": "tok"})]


def test_settlement_manager_falls_back_to_ctf_ops():
    class Gasless:
        is_available = False

    class CTF:
        async def merge_positions(self, condition_id, amount, **kwargs):
            return f"ctf:{condition_id}:{amount}:{kwargs['collateral_token']}"

    import asyncio

    assert asyncio.run(SettlementManager(ctf_ops=CTF(), gasless_merger=Gasless()).merge("cond", 7, collateral_token="tok")) == "ctf:cond:7:tok"


def test_relayer_helpers_build_deposit_wallet_request_shapes():
    checksum = lambda addr: addr.upper()
    call = normalize_relayer_call({"target": "0xabc", "data": "0x123"}, checksum)

    assert call == {"target": "0XABC", "value": "0", "data": "0x123"}
    assert deposit_wallet_domain(137, "0xwallet") == {
        "name": "DepositWallet",
        "version": "1",
        "chainId": 137,
        "verifyingContract": "0xwallet",
    }
    assert deposit_wallet_message("0xwallet", "7", "9", [call]) == {
        "wallet": "0xwallet",
        "nonce": 7,
        "deadline": 9,
        "calls": [call],
    }
    assert "Batch" in DEPOSIT_WALLET_BATCH_TYPES

    payload = deposit_wallet_submit_payload(
        from_address="0xowner",
        factory=DEPOSIT_WALLET_FACTORY,
        wallet="0xwallet",
        nonce="7",
        deadline="9",
        calls=[call],
        signature="0xsig",
        metadata="Merge Positions",
    )
    assert payload["type"] == "WALLET"
    assert payload["metadata"] == "Merge Positions"
    assert compact_json({"b": 1, "a": 2}) == '{"b":1,"a":2}'


def test_balance_monitor_exports_preserve_existing_imports():
    ctf_ops = importlib.import_module("src.execution.ctf_ops")
    settlement = importlib.import_module("src.execution.settlement")
    balances = importlib.import_module("src.execution.settlement.balances")

    assert ctf_ops.BalanceMonitor is settlement.BalanceMonitor is balances.BalanceMonitor
    assert ctf_ops.SimulatedBalanceMonitor is settlement.SimulatedBalanceMonitor is balances.SimulatedBalanceMonitor


def test_settlement_package_import_does_not_import_ctf_ops_subprocess():
    code = """
import sys
import src.execution.settlement
print('src.execution.ctf_ops' in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "False"
