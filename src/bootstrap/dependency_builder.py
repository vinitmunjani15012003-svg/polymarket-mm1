"""Dependency wiring helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ServiceContainer:
    services: dict

    def get(self, name: str, default=None):
        return self.services.get(name, default)


def build_container(**services) -> ServiceContainer:
    return ServiceContainer(services=services)
