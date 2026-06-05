import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bootstrap.dependency_builder import (
    active_symbols,
    balance_monitor_address,
    build_container,
    mt5_bridge_log_fields,
    select_active_assets,
    should_disable_onchain_ctf_fallback,
    symbol_to_asset,
)
from src.bootstrap.recovery import run_live_recovery_sequence, summarize_recovery_state
from src.bootstrap.startup_checks import (
    missing_live_credentials,
    run_startup_checks,
    validate_credentials,
    validate_live_credentials,
)
from src.config import load_config


def asset(symbol, enabled=True):
    return SimpleNamespace(symbol=symbol, enabled=enabled)


def test_dependency_builder_selects_assets_and_builds_container_without_side_effects():
    cfg = SimpleNamespace(assets={"BTC": asset("btcusdt"), "ETH": asset("ethusdt", enabled=False)})

    selected = select_active_assets(cfg)
    container = build_container(active_assets=selected)

    assert list(selected) == ["BTC"]
    assert active_symbols(selected) == ["btcusdt"]
    assert symbol_to_asset(selected) == {"BTCUSDT": "BTC"}
    assert container.require("active_assets") is selected
    assert tuple(container.names()) == ("active_assets",)


def test_dependency_builder_preserves_wallet_mode_address_and_mt5_log_shape():
    credentials = SimpleNamespace(
        signature_type=3,
        funder="0xfunder",
        mt5_bridge_url="http://bridge.local:8765/path",
        mt5_bridge_api_key="secret-value",
        mt5_bridge_stale_seconds=5.0,
    )
    cfg = SimpleNamespace(credentials=credentials)

    fields = mt5_bridge_log_fields(cfg, {"MT5_BRIDGE_URL": "x", "MT5_BRIDGE_API_KEY": "y"}, [".env"])

    assert should_disable_onchain_ctf_fallback(credentials) is True
    assert balance_monitor_address(credentials) == "0xfunder"
    assert fields == {
        "configured": True,
        "url_host": "bridge.local:8765",
        "has_api_key": True,
        "stale_seconds": 5.0,
        "loaded_env_files": [".env"],
        "mt5_env_url_present": True,
        "mt5_env_key_present": True,
    }


def test_startup_checks_validate_live_credentials_read_only():
    cfg = SimpleNamespace(
        mode="live",
        credentials=SimpleNamespace(private_key="pk", api_key="", api_secret="sec", api_passphrase=""),
    )

    result = validate_live_credentials(cfg)

    assert result.ok is False
    assert result.missing == ("api_key", "api_passphrase")
    assert missing_live_credentials(cfg) == ("api_key", "api_passphrase")
    with pytest.raises(ValueError, match="api_key, api_passphrase"):
        validate_credentials(cfg)


def test_load_config_rejects_empty_base_or_override_files(tmp_path):
    empty_base = tmp_path / "empty.yaml"
    empty_base.write_text("")

    with pytest.raises(ValueError, match="config file is empty"):
        load_config(str(empty_base))

    base = tmp_path / "base.yaml"
    override = tmp_path / "override.yaml"
    base.write_text("mode: dry-run\ncredentials: {}\nassets: {}\n")
    override.write_text("")

    with pytest.raises(ValueError, match="override config file is empty"):
        load_config(str(base), str(override))


def test_run_startup_checks_returns_structured_failures():
    results = run_startup_checks([
        ("ok", lambda: True),
        ("false", lambda: False),
        ("boom", lambda: (_ for _ in ()).throw(RuntimeError("bad"))),
    ])

    assert [(r.name, r.ok, r.error) for r in results] == [
        ("ok", True, ""),
        ("false", False, "returned false"),
        ("boom", False, "bad"),
    ]


@pytest.mark.asyncio
async def test_recovery_sequence_reconciles_before_cancel_and_summarizes_state():
    class Executor:
        def __init__(self):
            self.calls = []
            self.open_orders = {"old": {}}
            self.positions = {"M1": {}}

        async def reconcile_on_startup(self):
            self.calls.append("reconcile")
            self.open_orders["reconciled"] = {}

        async def cancel_all(self):
            self.calls.append("cancel_all")
            self.open_orders = {}

    executor = Executor()
    summary = await run_live_recovery_sequence(executor, state_manager=object())

    assert executor.calls == ["reconcile", "cancel_all"]
    assert summary.as_dict()["steps"] == ["reconcile_on_startup", "cancel_all"]
    assert summary.as_dict()["cancelled_stale"] is True
    assert summary.as_dict()["reconciled"] is True
    assert summary.as_dict()["state_loaded"] is True
    assert summary.as_dict()["open_orders"] == {}


def test_recovery_summary_is_side_effect_free():
    executor = SimpleNamespace(open_orders={"OID": {}}, positions={"M1": {}})

    summary = summarize_recovery_state(executor, state_manager=None, mode="live").as_dict()

    assert summary["open_orders"] == {"OID": {}}
    assert summary["positions"] == {"M1": {}}
    assert summary["state_loaded"] is False
    assert summary["mode"] == "live"


def test_bootstrap_modules_do_not_import_main_or_concrete_services():
    bootstrap_files = Path("src/bootstrap")
    forbidden_prefixes = ("src.main", "src.data", "src.execution", "src.monitoring", "src.orchestration", "src.risk", "src.strategy")

    for path in bootstrap_files.glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not [name for name in imports if name.startswith(forbidden_prefixes)]

    modules = [
        "src.bootstrap.dependency_builder",
        "src.bootstrap.startup_checks",
        "src.bootstrap.recovery",
        "src.main",
    ]
    assert [importlib.import_module(name).__name__ for name in modules] == modules
