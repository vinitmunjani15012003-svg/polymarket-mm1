"""Polymarket settlement contract addresses and ABI fragments."""

from __future__ import annotations

# Polygon contract addresses
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E_COLLATERAL_TOKEN = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
# Official Polymarket trading contracts that may require collateral allowance.
# Older py-clob-client builds still use the first pair; current docs list the
# V2 pair. Approving both avoids the UI's "Activate Funds" prompt after merged
# USDC.e returns to a deposit wallet.
CLOB_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CLOB_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
# Neg Risk Adapter: the CLOB balance API checks allowance for this contract.
# Without approval, the "Activate Funds" popup persists even after merge.
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
MAX_UINT256 = 2**256 - 1

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
    {
        "name": "getCollectionId",
        "type": "function",
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "getPositionId",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
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

ERC1155_APPROVAL_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

# Standard binary partition: [1, 2] = [Up, Down]
BINARY_PARTITION = [1, 2]
# Zero parent collection for top-level positions
PARENT_COLLECTION_ID = b'\x00' * 32
