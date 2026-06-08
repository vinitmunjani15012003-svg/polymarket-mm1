"""Pure dependency wiring helpers for startup.

The production constructors still live in ``main.py`` for now.  This module is
an architecture seam: it describes startup selections and holds already-created
objects without importing concrete bot services, which keeps it safe to import in
unit tests and avoids circular dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


@dataclass(slots=True)
class ServiceContainer:
    services: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default=None):
        return self.services.get(name, default)

    def require(self, name: str):
        if name not in self.services:
            raise KeyError(f"required service missing: {name}")
        return self.services[name]

    def register(self, name: str, service: Any) -> Any:
        self.services[name] = service
        return service

    def names(self) -> Iterable[str]:
        return tuple(self.services.keys())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.services)


def build_container(**services) -> ServiceContainer:
    """Return a simple container for already-constructed services."""
    return ServiceContainer(services=dict(services))


def select_active_assets(config: Any, assets_filter: list[str] | None = None) -> dict[str, Any]:
    """Select enabled assets, preserving ``main.py``'s historical semantics."""
    active_assets: dict[str, Any] = {}
    for name, ac in getattr(config, "assets", {}).items():
        if not getattr(ac, "enabled", False):
            continue
        if assets_filter and name not in assets_filter:
            continue
        active_assets[name] = ac
    return active_assets


def active_symbols(active_assets: Mapping[str, Any]) -> list[str]:
    """Return price-feed symbols for selected assets in insertion order."""
    return [ac.symbol for ac in active_assets.values()]


def symbol_to_asset(active_assets: Mapping[str, Any]) -> dict[str, str]:
    """Return upper-case symbol to asset-name lookup for live price routing."""
    return {ac.symbol.upper(): name for name, ac in active_assets.items()}


def mt5_bridge_log_fields(config: Any, env: Mapping[str, str], loaded_env_files: list[str] | None = None) -> dict[str, Any]:
    """Build non-secret MT5 bridge observability fields.

    Mirrors the inline logging shape from ``main.py`` while keeping URL handling
    declarative and secret-safe (host only; never the API key).
    """
    credentials = config.credentials
    bridge_url = getattr(credentials, "mt5_bridge_url", "") or ""
    parsed = urlparse(bridge_url) if bridge_url.startswith("http") else None
    return {
        "configured": bool(bridge_url),
        "url_host": parsed.netloc if parsed else "",
        "has_api_key": bool(getattr(credentials, "mt5_bridge_api_key", "")),
        "stale_seconds": getattr(credentials, "mt5_bridge_stale_seconds", None),
        "symbol_map": getattr(credentials, "mt5_bridge_symbol_map", {}) or {},
        "loaded_env_files": loaded_env_files or [],
        "mt5_env_url_present": bool(env.get("MT5_BRIDGE_URL")),
        "mt5_env_key_present": bool(env.get("MT5_BRIDGE_API_KEY")),
    }


def should_disable_onchain_ctf_fallback(credentials: Any) -> bool:
    """Proxy/deposit-wallet funder modes cannot safely use EOA on-chain fallback."""
    return bool(getattr(credentials, "signature_type", None) in (1, 2, 3) and getattr(credentials, "funder", ""))


def balance_monitor_address(credentials: Any) -> str:
    """Return explicit live balance address using the existing main.py rule."""
    if getattr(credentials, "signature_type", None) in (1, 2, 3):
        return getattr(credentials, "funder", "") or ""
    return ""
