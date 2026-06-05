"""CLOB auth facade."""

from __future__ import annotations


class ClobAuth:
    def __init__(self, client):
        self.client = client

    async def login(self):
        init = getattr(self.client, "initialize", None)
        if callable(init):
            return await init()
        return True

    async def refresh(self):
        refresh = getattr(self.client, "refresh", None)
        if callable(refresh):
            return await refresh()
        return True
