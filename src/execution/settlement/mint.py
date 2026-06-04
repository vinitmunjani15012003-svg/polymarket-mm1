"""Mint operation facade."""

from __future__ import annotations


class MintService:
    def __init__(self, ctf_ops):
        self.ctf_ops = ctf_ops

    async def mint(self, *args, **kwargs):
        return await self.ctf_ops.mint_positions(*args, **kwargs)
