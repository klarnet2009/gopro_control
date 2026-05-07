from __future__ import annotations

import asyncio
import logging
import platform
import re
import subprocess
from collections.abc import Awaitable, Callable
from shutil import which

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
    """Build the REST router.

    Error response convention:
      • HTTPException — for HTTP-layer concerns: missing resource (404),
        request/state conflicts visible to the schema layer (409 — e.g.
        "must be COHN to sync time"), input validation against schema or
        manager-side capability checks (422). The body is FastAPI's standard
        ``{"detail": "..."}``.
      • CommandResult.failure(code, message) — for command-execution failures
        where the request was well-formed but the camera/SDK refused or the
        BLE/HTTP round-trip errored. Returned as HTTP 200 + envelope so the UI
        can surface the message inline next to the camera card without treating
        it as a transport error.

    Note: ``RuntimeError("not connected")`` from the manager intentionally
    falls into CommandResult.failure rather than 409 — the UI treats it as a
    per-card error (toast/error pill) rather than a global HTTP failure. If
    you want 409 semantics for a specific endpoint, catch CameraNotFound +
    a state-pre-check via ``mgr.get_status(...).connection`` before calling
    the command.

    The web client's api() helper handles both shapes. Keep new endpoints in
    line with this split; do not mix the two for the same failure mode.
    """
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
        except Exception as exc:
            # Camera existence was already verified above, so CameraNotFound
            # cannot fire here — only camera-side errors (BLE timeout, etc.).
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
            mgr = _mgr(request)
            bus = _bus(request)
            # Optimistic update: tell all clients the camera stopped immediately.
            # The actual BLE/HTTP command can take 5-10s while GoPro finalises
            # the file on SD — the real confirmed state follows once it completes.
            cur = mgr.get_status(cam_id)
            await bus.broadcast({"type": "status", "payload": cur.model_copy(update={"encoding": False}).model_dump()})
            status = await mgr.stop(cam_id)
            await bus.broadcast({"type": "status", "payload": status.model_dump()})
            return CommandResult.success(status.model_dump())
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except Exception as exc:
            # Restore accurate state so the UI reflects what actually happened.
            try:
                await _bus(request).broadcast({"type": "status", "payload": _mgr(request).get_status(cam_id).model_dump()})
            except Exception:
                pass
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
        mgr = _mgr(request)
        bus = _bus(request)
        # Optimistic broadcast for every recording camera before the slow BLE/HTTP stop
        for s in mgr.list_status():
            if s.encoding:
                await bus.broadcast({"type": "status", "payload": s.model_copy(update={"encoding": False}).model_dump()})
        statuses = await mgr.stop_all()
        payload = [s.model_dump() for s in statuses]
        for s in payload:
            await bus.broadcast({"type": "status", "payload": s})
        return CommandResult.success(payload)

    @router.get("/wifi-ssid")
    async def wifi_ssid(request: Request):
        """Return the SSID of the host machine's current Wi-Fi connection.

        Used to pre-fill the SSID field in the COHN provision wizard.
        Returns {ssid: "..."} on success, {ssid: null} when not connected
        or the platform is unsupported. Never raises — UI degrades gracefully.
        """
        ssid = await asyncio.get_running_loop().run_in_executor(None, _get_current_ssid)
        return CommandResult.success({"ssid": ssid})

    @router.post("/scan")
    async def scan(request: Request):
        scan_fn: ScanFn = getattr(request.app.state, "scan_fn", default_scan_gopros)
        try:
            results = await scan_fn(6.0)
        except Exception as exc:
            log.exception("BLE scan failed")
            return CommandResult.failure("scan_failed", str(exc))
        for status in _mgr(request).update_signal_from_scan(results):
            await _bus(request).broadcast({"type": "status", "payload": status.model_dump()})
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
        except ValueError as exc:
            # Manager-side validation against the camera's advertised
            # capabilities — a 422 lets the client surface a precise message
            # without polluting the CommandResult error envelope.
            raise HTTPException(status_code=422, detail=str(exc))
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
        except ValueError as exc:
            # Defense-in-depth: ModePayload's Literal already rejects unknown
            # modes at schema time; this catches direct manager calls bypassing
            # the API or schema loosening.
            raise HTTPException(status_code=422, detail=str(exc))
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
            if st.mode not in ("cohn", "ble+cohn"):
                raise HTTPException(status_code=409, detail="clock sync is only supported for COHN or BLE+COHN cameras")
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
    def _preview_procs(req: Request) -> dict[str, asyncio.subprocess.Process]:
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
            rtsp_url = await mgr.start_preview(cam_id)
        except CameraNotFound:
            raise HTTPException(status_code=404, detail=f"unknown camera: {cam_id}")
        except RuntimeError as exc:
            # Manager surfaces wrong-mode / not-connected as RuntimeError; map
            # to 409 so the client can distinguish from a 5xx camera error.
            raise HTTPException(status_code=409, detail=str(exc))
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
        # If ffmpeg produces no bytes for this many seconds, treat the stream
        # as stalled and tear it down. The camera is bursty (RTSP keyframes,
        # network jitter) but a healthy stream produces something every couple
        # of seconds; 8 s is well above noise but well below "user gave up".
        FFMPEG_STDOUT_IDLE_TIMEOUT = 8.0

        async def gen():
            try:
                buf = b""
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(65536),
                            timeout=FFMPEG_STDOUT_IDLE_TIMEOUT,
                        )
                    except TimeoutError:
                        log.warning(
                            "preview ffmpeg for %s produced no bytes in %.1fs — closing stream",
                            cam_id, FFMPEG_STDOUT_IDLE_TIMEOUT,
                        )
                        break
                    if not chunk:
                        break
                    buf += chunk
                    # Cap the parser buffer: if SOI/EOI markers never align
                    # (corrupt JPEG, ffmpeg error stream), drop everything to
                    # avoid unbounded memory growth.
                    if len(buf) > 4 * 1024 * 1024:
                        log.warning("preview ffmpeg for %s emitted 4 MiB without a complete frame — resetting buffer", cam_id)
                        buf = b""
                        continue
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
                # Clear our entry only if we still own this slot; another
                # request for the same cam_id may have replaced it.
                if procs.get(cam_id) is proc:
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
        await _mgr(request).stop_preview(cam_id)
        return CommandResult.success({"id": cam_id, "stopped": True})

    # ── ATEM status + auto-trigger toggle ─────────────────────────────────

    @router.get("/atem/status")
    async def atem_status(request: Request):
        """Return the current ATEM connection and recording state."""
        watcher = getattr(request.app.state, "atem_watcher", None)
        if watcher is None:
            return CommandResult.success({"enabled": False})
        return CommandResult.success({"enabled": True, **watcher.status})

    @router.post("/atem/auto")
    async def atem_set_auto(request: Request):
        """Enable or disable ATEM auto-trigger. Body: {"enabled": true|false}"""
        watcher = getattr(request.app.state, "atem_watcher", None)
        if watcher is None:
            raise HTTPException(status_code=404, detail="ATEM watcher not running")
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        watcher.set_auto(enabled)
        return CommandResult.success({"enabled": True, **watcher.status})

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
