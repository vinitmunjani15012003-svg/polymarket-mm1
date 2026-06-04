"""Startup validation hooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(slots=True, frozen=True)
class StartupCheckResult:
    name: str
    ok: bool
    error: str = ""


def validate_config(config) -> bool:
    if not getattr(config, "assets", None):
        raise ValueError("at least one asset must be configured")
    return True


def validate_credentials(config) -> bool:
    mode = getattr(config, "mode", "dry-run")
    if mode == "live" and not getattr(getattr(config, "credentials", None), "private_key", ""):
        raise ValueError("live mode requires private_key")
    return True


def run_startup_checks(checks: Iterable[tuple[str, Callable[[], bool]]]) -> list[StartupCheckResult]:
    """Run already-bound checks and return structured results.

    This keeps startup validation observable without changing whether callers
    choose to raise, log, or abort.
    """
    results: list[StartupCheckResult] = []
    for name, check in checks:
        try:
            ok = bool(check())
            results.append(StartupCheckResult(name=name, ok=ok, error="" if ok else "returned false"))
        except Exception as exc:  # callers can decide how to surface failures
            results.append(StartupCheckResult(name=name, ok=False, error=str(exc) or exc.__class__.__name__))
    return results
