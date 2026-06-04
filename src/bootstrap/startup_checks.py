"""Startup validation hooks."""

from __future__ import annotations


def validate_config(config) -> bool:
    if not getattr(config, "assets", None):
        raise ValueError("at least one asset must be configured")
    return True


def validate_credentials(config) -> bool:
    mode = getattr(config, "mode", "dry-run")
    if mode == "live" and not getattr(getattr(config, "credentials", None), "private_key", ""):
        raise ValueError("live mode requires private_key")
    return True
