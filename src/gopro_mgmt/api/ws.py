from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class WSBroadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._lock:
            stale: list[WebSocket] = []
            for ws in self._clients:
                try:
                    await ws.send_json(message)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._clients.discard(ws)
                log.debug("dropped stale WebSocket client")
