from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
import re
from shutil import which
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..manager import CameraAlreadyExists, CameraManager, CameraNotFound
from ..scanner import scan_gopros as default_scan_gopros
from ..schemas import (
    AppConfig,
    CameraConfig,
    CameraCreate,
    CameraSettingsPayload,
    CameraUpdate,
    CohnProvisionPayload,
    CommandResult,
    ModePayload,
    ScanResult,
)
from .ws import WSBroadcaster

log = logging.getLogger(__name__)

ScanFn = Callable[[float], Awaitable[list[ScanResult]]]


def _get_current_ssid() -> str | None:
    """Read the current Wi-Fi SSID from the host OS. Returns None on failure."""
    try:
        system = platform.system()
        if system == "Darwin":  # macOS
            # Try networksetup first (reliable, no root needed)
            for iface in ("en0", "en1", "en2"):
                try:
                    out = subprocess.check_output(
                        ["networksetup", "-getairportnetwork", iface],
                        stderr=subprocess.DEVNULL, timeout=3,
                    ).decode(errors="replace").strip()
                    # Output: "Current Wi-Fi Network: MySSID"
                    if ":" in out and "not associated" not in out.lower():
                        return out.split(":", 1)[1].strip() or None
                except (subprocess.CalledProcessError, FileNotFoundError):
                    continue
        elif system == "Windows":
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode(errors="replace")
            m = re.search(r"^\s+SSID\s+:\s+(.+)$", out, re.MULTILINE)
            if m:
                return m.group(1).strip() or None
        elif system == "Linux":
            # Try nmcli (NetworkManager)
            try:
                out = subprocess.check_output(
                    ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                    stderr=subprocess.DEVNULL, timeout=3,
                ).decode(errors="replace")
                for line in out.splitlines():
                    if line.startswith("yes:"):
                        return line[4:].strip() or None
            except FileNotFoundError:
                pass
            # Fallback: iwgetid
            out = subprocess.check_output(
                ["iwgetid", "-r"], stderr=subprocess.DEVNULL, timeout=3,
            ).decode(errors="replace").strip()
            return out or None
    except Exception as exc:
        log.debug("could not read Wi-Fi SSID: %s", exc)
    return None


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    def _mgr(request: Request) -> CameraManager:
        return request.app.state.manager

    def _bus(request: Request) -> WSBroadcaster:
        return request.app.state.broadcaster

    async def _persist(request: Request) -> None:
        store = getattr(request.app.state, "config_store", None)
        if store is None:
            return
        base: AppConfig = request.app.state.config
        snapshot = base.model_copy(update={"cameras": _mgr(request).export_configs()})
        await store.save(snapshot)

    @router.get("/cameras")
    async def list_cameras(request: Request):
        statuses = _mgr(request).list_status()
        return CommandResult.success([s.model_dump() for s in statuses])

    @router.get("/cameras/{cam_id}/status")
    async def get_status(cam_id: str, request: Request):
        try:
            mgr = _mgr(request)
            if cam_id in mgr.ids() and mgr.get_status(cam_id).connection == "connected":
                status = await mgr.refresh_status(cam_id)
            else:
                status = mgr.get_status(cam_id)
            return CommandResult.success(status.model_dump())
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")

    @router.post("/cameras", status_code=201)
    async def add_camera(payload: CameraCreate, request: Request):
        cfg = CameraConfig(**payload.model_dump())
        try:
            status = await _mgr(request).add(cfg)
        except CameraAlreadyExists:
            raise HTTPException(status_code=409, detail=f"camera id already exists: {cfg.id}")
        await _persist(request)
        await _bus(request).broadcast({"type": "camera_added", "payload": status.model_dump()})
        return CommandResult.success(status.model_dump())

    @router.patch("/cameras/{cam_id}")
    async def update_camera(cam_id: str, payload: CameraUpdate, request: Request):
        try:
            status = await _mgr(request).update(
                cam_id,
                name=payload.name,
                target=payload.target,
                mode=payload.mode,
            )
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        await _persist(request)
        await _bus(request).broadcast({"type": "camera_updated", "payload": status.model_dump()})
        return CommandResult.success(status.model_dump())

    @router.delete("/cameras/{cam_id}")
    async def delete_camera(cam_id: str, request: Request):
        try:
            await _mgr(request).remove(cam_id)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        await _persist(request)
        await _bus(request).broadcast({"type": "camera_removed", "payload": {"id": cam_id}})
        return CommandResult.success({"id": cam_id})

    @router.post("/cameras/{cam_id}/connect")
    async def connect(cam_id: str, request: Request):
        mgr = _mgr(request)
        bus = _bus(request)
        try:
            current = mgr.get_status(cam_id)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        # Push optimistic "connecting" so the UI updates immediately while the
        # BLE handshake (which can take 5–15s) is still in flight.
        optimistic = current.model_copy(update={"connection": "connecting", "last_error": None})
        await bus.broadcast({"type": "status", "payload": optimistic.model_dump()})
        try:
            status = await mgr.connect(cam_id)
            await bus.broadcast({"type": "status", "payload": status.model_dump()})
            return CommandResult.success(status.model_dump())
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            failed = mgr.get_status(cam_id)
            await bus.broadcast({"type": "status", "payload": failed.model_dump()})
            return CommandResult.failure("connect_failed", str(exc))

    @router.post("/cameras/{cam_id}/disconnect")
    async def disconnect(cam_id: str, request: Request):
        try:
            status = await _mgr(request).disconnect(cam_id)
            await _bus(request).broadcast({"type": "status", "payload": status.model_dump()})
            return CommandResult.success(status.model_dump())
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")

    @router.post("/cameras/{cam_id}/record/start")
    async def start_one(cam_id: str, request: Request):
        try:
            status = await _mgr(request).start(cam_id)
            await _bus(request).broadcast({"type": "status", "payload": status.model_dump()})
            return CommandResult.success(status.model_dump())
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            return CommandResult.failure("start_failed", str(exc))

    @router.post("/cameras/{cam_id}/record/stop")
    async def stop_one(cam_id: str, request: Request):
        try:
            status = await _mgr(request).stop(cam_id)
            await _bus(request).broadcast({"type": "status", "payload": status.model_dump()})
            return CommandResult.success(status.model_dump())
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            return CommandResult.failure("stop_failed", str(exc))

    @router.post("/cameras/record/start")
    async def start_all(request: Request):
        statuses = await _mgr(request).start_all()
        payload = [s.model_dump() for s in statuses]
        for s in payload:
            await _bus(request).broadcast({"type": "status", "payload": s})
        return CommandResult.success(payload)

    @router.post("/cameras/record/stop")
    async def stop_all(request: Request):
        statuses = await _mgr(request).stop_all()
        payload = [s.model_dump() for s in statuses]
        for s in payload:
            await _bus(request).broadcast({"type": "status", "payload": s})
        return CommandResult.success(payload)

    @router.get("/wifi-ssid")
    async def wifi_ssid(request: Request):
        """Return the SSID of the host machine's current Wi-Fi connection.

        Used to pre-fill the SSID field in the COHN provision wizard.
        Returns {ssid: "..."} on success, {ssid: null} when not connected
        or the platform is unsupported. Never raises — UI degrades gracefully.
        """
        ssid = await asyncio.get_event_loop().run_in_executor(None, _get_current_ssid)
        return CommandResult.success({"ssid": ssid})

    @router.post("/scan")
    async def scan(request: Request):
        scan_fn: ScanFn = getattr(request.app.state, "scan_fn", default_scan_gopros)
        try:
            results = await scan_fn(6.0)
        except Exception as exc:
            log.exception("BLE scan failed")
            return CommandResult.failure("scan_failed", str(exc))
        payload = [r.model_dump() for r in results]
        await _bus(request).broadcast({"type": "scan_result", "payload": payload})
        return CommandResult.success(payload)

    @router.get("/cameras/{cam_id}/settings")
    async def get_settings(cam_id: str, request: Request):
        """Return current resolution/fps and the lists the camera considers valid.

        Requires the camera to be connected. Returns an empty data dict when
        not connected (the UI shows the selector as disabled in that case).
        """
        try:
            caps = await _mgr(request).get_settings(cam_id)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        return CommandResult.success(caps)

    @router.post("/cameras/{cam_id}/settings")
    async def apply_settings(cam_id: str, payload: CameraSettingsPayload, request: Request):
        """Apply video resolution, fps, lens, and/or stabilization.

        Camera must be connected and not currently recording.
        At least one field must be provided.
        """
        if not any([payload.resolution, payload.fps, payload.lens, payload.hypersmooth]):
            raise HTTPException(
                status_code=422,
                detail="at least one of 'resolution', 'fps', 'lens', or 'hypersmooth' must be provided",
            )
        try:
            status = await _mgr(request).apply_settings(
                cam_id,
                resolution=payload.resolution,
                fps=payload.fps,
                lens=payload.lens,
                hypersmooth=payload.hypersmooth,
            )
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            return CommandResult.failure("settings_failed", str(exc))
        await _bus(request).broadcast({"type": "status", "payload": status.model_dump()})
        return CommandResult.success(status.model_dump())

    @router.post("/cameras/{cam_id}/mode")
    async def set_mode(cam_id: str, payload: ModePayload, request: Request):
        """Switch the camera's active preset group (video / photo / timelapse).

        Camera must be connected and not currently recording.
        """
        try:
            status = await _mgr(request).set_mode(cam_id, payload.mode)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            return CommandResult.failure("mode_failed", str(exc))
        await _bus(request).broadcast({"type": "status", "payload": status.model_dump()})
        return CommandResult.success(status.model_dump())

    # ── Clock sync ───────────────────────────────────────────────────────
    @router.post("/cameras/sync-time")
    async def sync_time_all(request: Request):
        """Sync the clock on all connected cameras to the server's current time.
        Returns {cam_id: "ok"|error_message} for each connected camera.
        """
        results = await _mgr(request).sync_time_all()
        return CommandResult.success(results)

    @router.post("/cameras/{cam_id}/sync-time")
    async def sync_time_one(cam_id: str, request: Request):
        """Sync the clock on one COHN camera to the server's current time."""
        try:
            mgr = _mgr(request)
            st = mgr.get_status(cam_id)
            if st.mode != "cohn":
                raise HTTPException(status_code=409, detail="clock sync is only supported for COHN cameras")
            await mgr.sync_time(cam_id)
        except HTTPException:
            raise
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            return CommandResult.failure("sync_time_failed", str(exc))
        return CommandResult.success({"id": cam_id})

    # ── COHN provisioning ─────────────────────────────────────────────────
    @router.post("/cameras/{cam_id}/provision-cohn")
    async def provision_cohn(cam_id: str, payload: CohnProvisionPayload, request: Request):
        """Run one-time BLE provisioning so the camera joins the home Wi-Fi
        and gets a COHN cert. Camera must be disconnected before calling.
        Takes 10–30 s; the camera will reboot.
        """
        try:
            status = await _mgr(request).provision_cohn(cam_id, payload.ssid, payload.password)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            return CommandResult.failure("provision_failed", str(exc))
        await _bus(request).broadcast({"type": "cohn_provisioned", "payload": status.model_dump()})
        await _bus(request).broadcast({"type": "status",           "payload": status.model_dump()})
        return CommandResult.success(status.model_dump())

    # ── Live preview (RTSP via ffmpeg → MJPEG over HTTP) ──────────────────
    def _preview_procs(req: Request) -> dict[str, "asyncio.subprocess.Process"]:
        if not hasattr(req.app.state, "preview_procs"):
            req.app.state.preview_procs = {}
        return req.app.state.preview_procs

    @router.get("/cameras/{cam_id}/preview")
    async def preview(cam_id: str, request: Request):
        """Stream MJPEG preview of one camera. COHN mode + connected + ffmpeg required."""
        if which("ffmpeg") is None:
            raise HTTPException(
                status_code=503,
                detail="ffmpeg is not installed on the server; preview unavailable.",
            )
        mgr = _mgr(request)
        try:
            st = mgr.get_status(cam_id)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        if st.connection != "connected" or st.mode != "cohn":
            raise HTTPException(status_code=409, detail="camera must be connected in COHN mode")

        # Acquire camera RTSP URL via driver. Intentional internal access:
        # the manager doesn't (yet) expose a high-level webcam helper.
        entry = mgr._entries[cam_id]            # type: ignore[attr-defined]
        async with entry.lock:
            if entry.driver is None:
                raise HTTPException(status_code=409, detail="driver not open")
            try:
                rtsp_url = await entry.driver.start_webcam_rtsp(resolution="720", fov="WIDE")
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"webcam_start failed: {exc}")

        # Kill any prior ffmpeg for this camera before starting a new one.
        procs = _preview_procs(request)
        old = procs.pop(cam_id, None)
        if old is not None and old.returncode is None:
            try: old.kill()
            except Exception: pass

        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-vf", "scale=640:-2",
            "-f", "mjpeg", "-q:v", "5", "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        procs[cam_id] = proc

        BOUNDARY = b"--frame"
        async def gen():
            try:
                buf = b""
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    # Split on JPEG SOI/EOI markers, emit one part per frame.
                    while True:
                        soi = buf.find(b"\xff\xd8")
                        eoi = buf.find(b"\xff\xd9", soi + 2) if soi >= 0 else -1
                        if soi < 0 or eoi < 0:
                            break
                        frame = buf[soi:eoi + 2]
                        buf = buf[eoi + 2:]
                        yield (
                            BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                            + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                            + frame + b"\r\n"
                        )
            finally:
                if proc.returncode is None:
                    try: proc.kill()
                    except Exception: pass
                procs.pop(cam_id, None)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @router.delete("/cameras/{cam_id}/preview")
    async def stop_preview(cam_id: str, request: Request):
        """Stop the MJPEG preview proxy and tell the camera to exit webcam mode."""
        procs = _preview_procs(request)
        proc = procs.pop(cam_id, None)
        if proc is not None and proc.returncode is None:
            try: proc.kill()
            except Exception: pass
        try:
            mgr = _mgr(request)
            entry = mgr._entries.get(cam_id)    # type: ignore[attr-defined]
            if entry and entry.driver is not None:
                async with entry.lock:
                    try: await entry.driver.stop_webcam()
                    except Exception: pass
        except Exception:
            pass
        return CommandResult.success({"id": cam_id, "stopped": True})

    return router


def build_ws_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        bus: WSBroadcaster = websocket.app.state.broadcaster
        mgr: CameraManager = websocket.app.state.manager
        await websocket.accept()
        await bus.add(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "payload": [s.model_dump() for s in mgr.list_status()],
                }
            )
            while True:
                # We don't expect inbound messages, but keep the connection live.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await bus.remove(websocket)

    return router
