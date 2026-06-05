"""Merge operation facade."""

from __future__ import annotations


class MergeService:
    def __init__(self, merger):
        self.merger = merger

    async def merge(self, condition_id: str, amount: int, **kwargs):
        return await self.merger.merge_positions(condition_id, amount, **kwargs)
