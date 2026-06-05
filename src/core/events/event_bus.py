"""Tiny in-process event bus for future decoupling."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable):
        self._subscribers[event_type].append(handler)

    def publish(self, event_type: str, payload: dict | None = None):
        for handler in list(self._subscribers.get(event_type, [])):
            handler(payload or {})
