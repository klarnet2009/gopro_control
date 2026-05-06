from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..atem_watcher import AtemWatcher
from ..config_store import ConfigStore
from ..manager import CameraManager, DriverFactory
from ..poller import StatusPoller
from ..schemas import AppConfig
from .routes import ScanFn, build_router, build_ws_router
from .ws import WSBroadcaster

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(
    config: AppConfig,
    driver_factory: DriverFactory | None = None,
    config_store: ConfigStore | None = None,
    scan_fn: ScanFn | None = None,
) -> FastAPI:
    manager = CameraManager(
        config.cameras,
        driver_factory=driver_factory,
        timing=config.timing,
    )
    broadcaster = WSBroadcaster()
    poller = StatusPoller(manager, broadcaster, interval_sec=config.poll_interval_sec)
    atem_watcher = AtemWatcher(
        manager=manager,
        broadcaster=broadcaster,
        host=config.atem_host,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        poller.start()
        atem_watcher.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            # Kill any leftover ffmpeg preview processes so the server exits
            # cleanly even when a client never sent DELETE /preview.
            procs = getattr(_app.state, "preview_procs", None)
            if procs:
                for proc in list(procs.values()):
                    try:
                        if proc.returncode is None:
                            proc.kill()
                    except Exception:
                        pass
                procs.clear()
            atem_watcher.stop()
            await poller.stop()
            await manager.shutdown()

    app = FastAPI(title="GoPro Management", version="0.1.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.broadcaster = broadcaster
    app.state.poller = poller
    app.state.atem_watcher = atem_watcher
    app.state.config = config
    app.state.config_store = config_store
    if scan_fn is not None:
        app.state.scan_fn = scan_fn

    app.include_router(build_router())
    app.include_router(build_ws_router())

    if WEB_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(WEB_DIR)),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(str(WEB_DIR / "index.html"))

    return app
