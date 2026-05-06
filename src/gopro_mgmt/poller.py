from __future__ import annotations

import asyncio
import logging

from .api.ws import WSBroadcaster
from .manager import CameraManager

log = logging.getLogger(__name__)


class StatusPoller:
    def __init__(
        self,
        manager: CameraManager,
        broadcaster: WSBroadcaster,
        interval_sec: float,
    ) -> None:
        self._manager = manager
        self._broadcaster = broadcaster
        self._interval = max(0.5, interval_sec)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="status-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._task = None

    async def _loop(self) -> None:
        log.info("status poller started (interval=%.1fs)", self._interval)
        try:
            while True:
                await asyncio.sleep(self._interval)
                for cid in self._manager.ids():
                    status = self._manager.get_status(cid)
                    if status.connection != "connected":
                        continue
                    refreshed = await self._manager.refresh_status(cid)
                    await self._broadcaster.broadcast(
                        {"type": "status", "payload": refreshed.model_dump()}
                    )
        except asyncio.CancelledError:
            log.info("status poller cancelled")
            raise
