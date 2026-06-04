"""Fill processing service wrapper."""

from __future__ import annotations


class FillProcessor:
    def __init__(self, executor):
        self.executor = executor

    def process_fill(self, fill: dict, *args, **kwargs):
        return self.process_fills([fill], *args, **kwargs)

    def process_fills(self, fills: list[dict], *args, **kwargs):
        return self.executor.process_fills(fills, *args, **kwargs)

    def handle_partial_fill(self, fill: dict) -> dict:
        fill = dict(fill)
        fill["partial"] = True
        return fill
