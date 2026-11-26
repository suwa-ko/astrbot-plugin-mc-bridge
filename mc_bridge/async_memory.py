"""Non-blocking async facade for MemoryStore using asyncio.to_thread."""
from __future__ import annotations

import asyncio
from typing import Any

from ..memory_store import MemoryStore


class AsyncMemoryStore:
    """Runs synchronous SQLite MemoryStore operations off the asyncio event loop.

    All underlying MemoryStore methods remain synchronously callable via ``.sync``
    while async callers (runtime, Coach, MCP server) can ``await`` any method
    without risking WebSocket heartbeat stalls during disk contention.
    """

    def __init__(self, store: MemoryStore):
        self.sync = store

    async def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.sync, method_name)
        return await asyncio.to_thread(fn, *args, **kwargs)

    def __getattr__(self, name: str):
        target = getattr(self.sync, name)
        if not callable(target):
            return target

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(target, *args, **kwargs)

        _wrapped.__name__ = name
        return _wrapped
