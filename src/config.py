"""
Configuration loader and validator.

Loads YAML config with environment variable substitution,
validates all required parameters, and provides typed access.
"""

import os
import re
import yaml
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class AssetConfig:
    """Per-asset trading parameters."""
    enabled: bool = True
    symbol: str = ""
    default_sigma: float = 0.60
    gamma: float = 0.30
    gamma_near_expiry: float = 1.0
    min_spread: float = 0.04
    max_spread: float = 0.25
    min_order_size: int = 5
    max_order_size: int = 30
    max_dollar_delta: float = 50.0
    soft_limit: float = 25.0
    hard_limit: float = 40.0
    emergency: float = 48.0
    auto_merge_dollar_threshold: float = 15.0  # Merge when locked capital exceeds this ($)


@dataclass
class GlobalConfig:
    """Global trading parameters."""
    refresh_interval: float = 1.0
    min_quote_interval: float = 0.25
    min_order_update_interval: float = 2.0
    stop_quoting_seconds: int = 120
    reduce_size_seconds: int = 300
    reprice_threshold: float = 0.005
    max_daily_loss_pct: float = 0.05
    max_drawdown_pct: float = 0.03
    max_capital_per_market: float = 200.0
    total_capital: float = 200.0
    starting_capital: float = 500.0
    market_discovery_interval: int = 30
    vol_lookback_seconds: int = 300
    kappa_window_seconds: int = 300


@dataclass
class CredentialsConfig:
    """API credentials."""
    # Polymarket
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    chain_id: int = 137
    host: str = "https://clob.polymarket.com"
    signature_type: int = 3
    funder: str = ""
    # Separate owner key for deposit wallet operations. If the trading API key
    # derives a different address than the deposit wallet owner, the relayer
    # requires the owner key for EIP-712 signed batches (approvals, merges).
    # Falls back to private_key if not set.
    owner_private_key: str = ""
    collateral_token: str = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD on Polygon
    # Polymarket Builder (for gasless merge/split/redeem)
    builder_api_key: str = ""
    builder_secret: str = ""
    builder_passphrase: str = ""
    builder_relayer_url: str = "https://relayer-v2.polymarket.com"
    relayer_api_key: str = ""
    relayer_api_key_address: str = ""
    # Polygon RPC (for balance monitoring and on-chain ops)
    # NOTE: polygon-rpc.com has become unreliable/paid for many tenants.
    polygon_rpc_url: str = "https://polygon-bor.publicnode.com"
    # Binance
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    binance_rest_url: str = "https://api.binance.com/api/v3"


@dataclass
class RegimeConfig:
    """Market regime filter parameters."""
    lookback: int = 20
    trend_threshold: float = 0.015
    spike_threshold: float = 0.03


@dataclass
class ToxicityConfig:
    """Adverse selection monitoring parameters."""
    window_seconds: int = 300
    threshold: float = 0.002
    edge_adverse_rate: float = 0.85
    edge_mean_threshold: float = 0.015
    edge_window: int = 30
    min_fills_for_halt: int = 8
    one_sided_fill_limit: int = 8
    immediate_drift_threshold: float = 0.02
    halt_cooldown: int = 90


@dataclass
class BalanceMonitorConfig:
    """Auto-merge balance monitoring parameters (live mode only)."""
    enabled: bool = True
    warn_balance: float = 20.0       # USDC balance to log warning
    merge_balance: float = 10.0      # USDC balance to trigger auto-merge
    min_merge_pairs: int = 5         # Minimum matched pairs to merge
    check_interval: float = 30.0     # Seconds between balance checks


@dataclass
class DryRunConfig:
    """Dry-run simulation parameters."""
    fill_probability: float = 0.60
    fill_delay_min: int = 2
    fill_delay_max: int = 10
    toxicity_multiplier: float = 2.0


@dataclass
class BotConfig:
    """Root configuration object."""
    mode: str = "dry-run"
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    assets: Dict[str, AssetConfig] = field(default_factory=dict)
    global_params: GlobalConfig = field(default_factory=GlobalConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    toxicity: ToxicityConfig = field(default_factory=ToxicityConfig)
    balance_monitor: BalanceMonitorConfig = field(default_factory=BalanceMonitorConfig)
    dry_run: DryRunConfig = field(default_factory=DryRunConfig)

    def validate(self):
        """Validate all configuration invariants."""
        if self.mode not in ["live", "dry-run"]:
            raise ValueError(f"Invalid mode: {self.mode}. Must be 'live' or 'dry-run'.")
            
        if self.mode == "live":
            if not self.credentials.private_key:
                raise ValueError("private_key is required in live mode")
            if not self.credentials.api_key:
                raise ValueError("api_key is required in live mode")
            if not self.credentials.api_secret:
                raise ValueError("api_secret is required in live mode")
            if not self.credentials.api_passphrase:
                raise ValueError("api_passphrase is required in live mode")
            if self.credentials.signature_type not in (0, 1, 2, 3):
                raise ValueError("signature_type must be one of 0, 1, 2, 3")
            if self.credentials.signature_type in (1, 2, 3) and not self.credentials.funder:
                raise ValueError("funder is required in live mode for proxy/safe/deposit wallets")
            if not self.credentials.collateral_token:
                raise ValueError("collateral_token is required in live mode")
                
        for name, asset in self.assets.items():
            if asset.min_spread >= asset.max_spread:
                raise ValueError(f"Asset {name}: min_spread ({asset.min_spread}) must be < max_spread ({asset.max_spread})")
            if asset.soft_limit >= asset.hard_limit or asset.hard_limit >= asset.emergency:
                raise ValueError(f"Asset {name}: Limits must be strictly increasing (soft < hard < emergency)")
            if asset.max_order_size <= 0:
                raise ValueError(f"Asset {name}: max_order_size must be > 0")
            if asset.min_order_size <= 0:
                raise ValueError(f"Asset {name}: min_order_size must be > 0")
            if asset.max_order_size < asset.min_order_size:
                raise ValueError(f"Asset {name}: max_order_size ({asset.max_order_size}) must be >= min_order_size ({asset.min_order_size})")
                
        if self.global_params.stop_quoting_seconds <= 0:
            raise ValueError("stop_quoting_seconds must be positive")


def _substitute_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} patterns with environment variable values."""
    if not isinstance(value, str):
        return value
    pattern = re.compile(r'\$\{(\w+)\}')
    def replacer(match):
        env_var = match.group(1)
        return os.environ.get(env_var, "")
    return pattern.sub(replacer, value)


def _recursive_env_sub(obj):
    """Recursively substitute env vars in a dict/list structure."""
    if isinstance(obj, dict):
        return {k: _recursive_env_sub(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_recursive_env_sub(item) for item in obj]
    elif isinstance(obj, str):
        return _substitute_env_vars(obj)
    return obj


def load_config(config_path: str = "config/default.yaml",
                override_path: Optional[str] = None) -> BotConfig:
    """
    Load configuration from YAML files with env var substitution.

    Args:
        config_path: Path to default config file.
        override_path: Optional path to override config (e.g., live.yaml).

    Returns:
        Fully populated BotConfig instance.
    """
    with open(config_path, 'r') as f:
        raw = yaml.safe_load(f)

    # Apply overrides if provided
    if override_path and os.path.exists(override_path):
        with open(override_path, 'r') as f:
            overrides = yaml.safe_load(f)
        if overrides:
            raw = _deep_merge(raw, overrides)

    # Substitute environment variables
    raw = _recursive_env_sub(raw)

    # Build typed config
    config = BotConfig()
    config.mode = raw.get("mode", "dry-run")

    # Credentials
    creds_raw = raw.get("credentials", {})
    pm = creds_raw.get("polymarket", {})
    bn = creds_raw.get("binance", {})
    builder = creds_raw.get("builder", {})
    config.credentials = CredentialsConfig(
        private_key=pm.get("private_key", ""),
        api_key=pm.get("api_key", ""),
        api_secret=pm.get("api_secret", ""),
        api_passphrase=pm.get("api_passphrase", ""),
        chain_id=pm.get("chain_id", 137),
        host=pm.get("host", "https://clob.polymarket.com"),
        signature_type=int(pm.get("signature_type", 3)),
        funder=pm.get("funder", pm.get("funder_address", "")),
        owner_private_key=pm.get("owner_private_key", ""),
        collateral_token=pm.get("collateral_token", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"),
        builder_api_key=builder.get("api_key", ""),
        builder_secret=builder.get("secret", ""),
        builder_passphrase=builder.get("passphrase", ""),
        builder_relayer_url=builder.get("relayer_url", "https://relayer-v2.polymarket.com"),
        relayer_api_key=builder.get("relayer_api_key", builder.get("api_key", "")),
        relayer_api_key_address=builder.get("relayer_api_key_address", builder.get("api_key_address", "")),
        polygon_rpc_url=pm.get("polygon_rpc_url", "https://polygon-bor.publicnode.com"),
        binance_ws_url=bn.get("ws_url", "wss://stream.binance.com:9443/ws"),
        binance_rest_url=bn.get("rest_url", "https://api.binance.com/api/v3"),
    )

    # Assets
    for name, params in raw.get("assets", {}).items():
        config.assets[name] = AssetConfig(
            enabled=params.get("enabled", True),
            symbol=params.get("symbol", ""),
            default_sigma=params.get("default_sigma", 0.60),
            gamma=params.get("gamma", 0.30),
            gamma_near_expiry=params.get("gamma_near_expiry", 1.0),
            min_spread=params.get("min_spread", 0.04),
            max_spread=params.get("max_spread", 0.25),
            min_order_size=params.get("min_order_size", 5),
            max_order_size=params.get("max_order_size", 30),
            max_dollar_delta=params.get("max_dollar_delta", 50.0),
            soft_limit=params.get("soft_limit", 25.0),
            hard_limit=params.get("hard_limit", 40.0),
            emergency=params.get("emergency", 48.0),
            auto_merge_dollar_threshold=params.get("auto_merge_dollar_threshold", 15.0),
        )

    # Global
    g = raw.get("global", {})
    config.global_params = GlobalConfig(
        refresh_interval=g.get("refresh_interval", 1),
        min_quote_interval=g.get("min_quote_interval", 0.25),
        min_order_update_interval=g.get("min_order_update_interval", 2.0),
        stop_quoting_seconds=g.get("stop_quoting_seconds", 120),
        reduce_size_seconds=g.get("reduce_size_seconds", 300),
        reprice_threshold=g.get("reprice_threshold", 0.005),
        max_daily_loss_pct=g.get("max_daily_loss_pct", 0.05),
        max_drawdown_pct=g.get("max_drawdown_pct", 0.03),
        max_capital_per_market=g.get("max_capital_per_market", 200.0),
        total_capital=g.get("total_capital", 200.0),
        starting_capital=g.get("starting_capital", 500.0),
        market_discovery_interval=g.get("market_discovery_interval", 30),
        vol_lookback_seconds=g.get("vol_lookback_seconds", 300),
        kappa_window_seconds=g.get("kappa_window_seconds", 300),
    )

    # Regime
    r = raw.get("regime", {})
    config.regime = RegimeConfig(
        lookback=r.get("lookback", 20),
        trend_threshold=r.get("trend_threshold", 0.015),
        spike_threshold=r.get("spike_threshold", 0.03),
    )

    # Toxicity
    t = raw.get("toxicity", {})
    config.toxicity = ToxicityConfig(
        window_seconds=t.get("window_seconds", 300),
        threshold=t.get("threshold", 0.002),
        edge_adverse_rate=t.get("edge_adverse_rate", 0.85),
        edge_mean_threshold=t.get("edge_mean_threshold", 0.015),
        edge_window=t.get("edge_window", 30),
        min_fills_for_halt=t.get("min_fills_for_halt", 8),
        one_sided_fill_limit=t.get("one_sided_fill_limit", 8),
        immediate_drift_threshold=t.get("immediate_drift_threshold", 0.02),
        halt_cooldown=t.get("halt_cooldown", 90),
    )

    # Balance monitor
    bm = raw.get("balance_monitor", {})
    config.balance_monitor = BalanceMonitorConfig(
        enabled=bm.get("enabled", True),
        warn_balance=bm.get("warn_balance", 20.0),
        merge_balance=bm.get("merge_balance", 10.0),
        min_merge_pairs=bm.get("min_merge_pairs", 5),
        check_interval=bm.get("check_interval", 30.0),
    )

    # Dry run
    d = raw.get("dry_run", {})
    config.dry_run = DryRunConfig(
        fill_probability=d.get("fill_probability", 0.60),
        fill_delay_min=d.get("fill_delay_min", 2),
        fill_delay_max=d.get("fill_delay_max", 10),
        toxicity_multiplier=d.get("toxicity_multiplier", 2.0),
    )

    # Validate config invariants
    config.validate()

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
