"""Redeem operation facade."""

from __future__ import annotations


class RedeemService:
    def __init__(self, ctf_ops):
        self.ctf_ops = ctf_ops

    async def redeem(self, *args, **kwargs):
        return await self.ctf_ops.redeem_positions(*args, **kwargs)
