"""Dependency wiring helpers.

These helpers are compatibility seams for gradually moving construction out of
``main.py``. They only hold already-created objects and perform no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


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


def build_container(**services) -> ServiceContainer:
    return ServiceContainer(services=dict(services))
