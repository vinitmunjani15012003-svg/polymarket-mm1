"""Settlement orchestration facade."""

from __future__ import annotations


class SettlementManager:
    def __init__(self, ctf_ops=None, gasless_merger=None):
        self.ctf_ops = ctf_ops
        self.gasless_merger = gasless_merger

    async def merge(self, condition_id: str, amount: int, **kwargs):
        if self.gasless_merger and getattr(self.gasless_merger, "is_available", False):
            return await self.gasless_merger.merge_positions(condition_id, amount, **kwargs)
        if self.ctf_ops:
            return await self.ctf_ops.merge_positions(condition_id, amount, **kwargs)
        return None
