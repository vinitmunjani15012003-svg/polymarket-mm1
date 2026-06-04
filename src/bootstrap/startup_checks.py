"""Declarative startup validation hooks.

These helpers intentionally perform read-only validation.  They do not load
secrets, mutate environment variables, construct clients, print, log, or exit;
callers keep control over compatibility behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Any


LIVE_CREDENTIAL_FIELDS: tuple[str, ...] = (
    "private_key",
    "api_key",
    "api_secret",
    "api_passphrase",
)


@dataclass(slots=True, frozen=True)
class StartupCheckResult:
    name: str
    ok: bool
    error: str = ""


@dataclass(slots=True, frozen=True)
class CredentialValidation:
    mode: str
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def failure_reason(self) -> str:
        if self.ok:
            return ""
        return f"missing live credentials: {', '.join(self.missing)}"


def validate_config(config: Any) -> bool:
    if not getattr(config, "assets", None):
        raise ValueError("at least one asset must be configured")
    return True


def missing_live_credentials(config: Any) -> tuple[str, ...]:
    """Return missing live credential field names using main.py's old fields."""
    credentials = getattr(config, "credentials", None)
    return tuple(
        field for field in LIVE_CREDENTIAL_FIELDS
        if not getattr(credentials, field, None)
    )


def validate_live_credentials(config: Any) -> CredentialValidation:
    mode = getattr(config, "mode", "dry-run")
    missing = missing_live_credentials(config) if mode == "live" else ()
    return CredentialValidation(mode=mode, missing=missing)


def validate_credentials(config: Any) -> bool:
    """Compatibility boolean/raising adapter for startup checks."""
    result = validate_live_credentials(config)
    if not result.ok:
        raise ValueError(result.failure_reason)
    return True


def require_live_credentials(config: Any) -> bool:
    return validate_credentials(config)


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
