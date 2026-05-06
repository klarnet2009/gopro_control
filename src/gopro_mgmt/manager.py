from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from .driver import COHN_DB_PATH, CameraDriver, WirelessGoProDriver, default_driver_factory
from .schemas import CameraConfig, CameraStatus, ScanResult

log = logging.getLogger(__name__)

DriverFactory = Callable[[CameraConfig], CameraDriver]


def _read_cohn_db_for(target: str) -> dict | None:
    """Read cohn_db.json (TinyDB format) and return credentials matching target.

    The TinyDB layout is:
        {"_default": {"1": {"serial": "ABCD", "credentials": {...}}, ...}}

    Returns the credentials dict (or whatever the SDK stores) for the entry whose
    serial endswith the target, or None on any failure / no match. Caller must
    treat None as 'no credentials' — never raise.
    """
    try:
        if not COHN_DB_PATH.exists():
            return None
        raw = COHN_DB_PATH.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        data = json.loads(raw)
        records = (data.get("_default") or {}) if isinstance(data, dict) else {}
        target_low = str(target).lower()
        for rec in records.values():
            if not isinstance(rec, dict):
                continue
            serial = str(rec.get("serial", "")).lower()
            if serial.endswith(target_low):
                creds = rec.get("credentials")
                if isinstance(creds, dict):
                    return creds
                return rec
    except Exception as exc:
        log.debug("could not read %s for target %s: %s", COHN_DB_PATH, target, exc)
    return None


@dataclass
class _Entry:
    config: CameraConfig
    status: CameraStatus
    driver: CameraDriver | None
    lock: asyncio.Lock


class CameraNotFound(KeyError):
    pass


class CameraAlreadyExists(ValueError):
    pass


class CameraManager:
    """In-memory registry of cameras with per-camera command serialization."""

    def __init__(
        self,
        cameras: list[CameraConfig],
        driver_factory: DriverFactory | None = None,
    ) -> None:
        self._driver_factory = driver_factory or default_driver_factory
        self._entries: dict[str, _Entry] = {}
        self._mutate_lock = asyncio.Lock()
        for cfg in cameras:
            self._entries[cfg.id] = self._build_entry(cfg)

    @staticmethod
    def _build_entry(cfg: CameraConfig) -> _Entry:
        status = CameraStatus(id=cfg.id, name=cfg.name, target=cfg.target, mode=cfg.mode)
        creds = _read_cohn_db_for(cfg.target)
        if creds:
            status.cohn_provisioned = True
            status.cohn_ip = creds.get("ip_address") if isinstance(creds, dict) else None
        return _Entry(
            config=cfg,
            status=status,
            driver=None,
            lock=asyncio.Lock(),
        )

    # --- queries -------------------------------------------------------------

    def list_status(self) -> list[CameraStatus]:
        return [e.status.model_copy() for e in self._entries.values()]

    def get_status(self, cam_id: str) -> CameraStatus:
        return self._entry(cam_id).status.model_copy()

    def ids(self) -> list[str]:
        return list(self._entries.keys())

    def export_configs(self) -> list[CameraConfig]:
        return [e.config.model_copy() for e in self._entries.values()]

    def update_signal_from_scan(self, results: list[ScanResult]) -> list[CameraStatus]:
        """Store latest BLE RSSI for configured cameras, matched by target."""
        by_target = {r.target.lower(): r for r in results}
        changed: list[CameraStatus] = []
        for e in self._entries.values():
            result = by_target.get(e.config.target.lower())
            if result is None:
                continue
            if e.status.rssi_dbm != result.rssi:
                e.status.rssi_dbm = result.rssi
                changed.append(e.status.model_copy())
        return changed

    # --- registry mutations -------------------------------------------------

    async def add(self, cfg: CameraConfig) -> CameraStatus:
        async with self._mutate_lock:
            if cfg.id in self._entries:
                raise CameraAlreadyExists(cfg.id)
            entry = self._build_entry(cfg)
            self._entries[cfg.id] = entry
            log.info("added camera %s (target=%s, mode=%s)", cfg.id, cfg.target, cfg.mode)
            return entry.status.model_copy()

    async def remove(self, cam_id: str) -> None:
        async with self._mutate_lock:
            if cam_id not in self._entries:
                raise CameraNotFound(cam_id)
            await self._disconnect_locked(cam_id)
            del self._entries[cam_id]
            log.info("removed camera %s", cam_id)

    async def update(
        self,
        cam_id: str,
        *,
        name: str | None = None,
        target: str | None = None,
        mode: str | None = None,
    ) -> CameraStatus:
        async with self._mutate_lock:
            e = self._entry(cam_id)
            transport_changed = (
                (target is not None and target != e.config.target)
                or (mode is not None and mode != e.config.mode)
            )
            if transport_changed and e.driver is not None:
                log.info("auto-disconnecting %s due to transport change", cam_id)
                await self._disconnect_locked(cam_id)

            updates: dict[str, str] = {}
            if name is not None and name != e.config.name:
                updates["name"] = name
            if target is not None and target != e.config.target:
                updates["target"] = target
            if mode is not None and mode != e.config.mode:
                updates["mode"] = mode
            if updates:
                e.config = e.config.model_copy(update=updates)
                for k, v in updates.items():
                    setattr(e.status, k, v)
                log.info("updated camera %s: %s", cam_id, updates)
            return e.status.model_copy()

    async def _disconnect_locked(self, cam_id: str) -> None:
        """Internal helper — must be called only from within _mutate_lock."""
        e = self._entries.get(cam_id)
        if e is None:
            return
        async with e.lock:
            drv = e.driver
            e.driver = None
            if drv is not None:
                try:
                    await drv.close()
                except Exception:
                    log.exception("close failed for %s", cam_id)
            e.status.connection = "disconnected"
            e.status.encoding = None

    # --- lifecycle -----------------------------------------------------------

    async def connect(self, cam_id: str) -> CameraStatus:
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is not None:
                return e.status.model_copy()
            e.status.connection = "connecting"
            e.status.last_error = None
            try:
                drv = self._driver_factory(e.config)
                await drv.open()
                e.driver = drv
                e.status.connection = "connected"
                e.status.model = drv.get_model()
                try:
                    data = await drv.get_status()
                    for key in ("encoding", "battery_percent", "sd_remaining_sec", "preset_group"):
                        if key in data and data[key] is not None:
                            setattr(e.status, key, data[key])
                except Exception:
                    log.warning("could not fetch status on connect for %s", cam_id)
                try:
                    vid = await drv.get_current_video_settings()
                    for key in ("resolution", "fps", "lens", "hypersmooth"):
                        if key in vid and vid[key] is not None:
                            setattr(e.status, key, vid[key])
                except Exception:
                    log.warning("could not fetch video settings on connect for %s", cam_id)
                log.info(
                    "connected camera %s (target=%s, mode=%s, model=%s)",
                    cam_id, e.config.target, e.config.mode, e.status.model,
                )
            except Exception as exc:
                e.driver = None
                e.status.connection = "error"
                e.status.last_error = str(exc)
                log.exception("connect failed for %s", cam_id)
                raise
            return e.status.model_copy()

    async def disconnect(self, cam_id: str) -> CameraStatus:
        e = self._entry(cam_id)
        async with e.lock:
            drv = e.driver
            e.driver = None
            if drv is not None:
                try:
                    await drv.close()
                except Exception:
                    log.exception("close failed for %s", cam_id)
            e.status.connection = "disconnected"
            e.status.encoding = None
            return e.status.model_copy()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *[self.disconnect(cid) for cid in list(self._entries)],
            return_exceptions=True,
        )

    # --- commands ------------------------------------------------------------

    async def start(self, cam_id: str) -> CameraStatus:
        return await self._shutter(cam_id, on=True)

    async def stop(self, cam_id: str) -> CameraStatus:
        return await self._shutter(cam_id, on=False)

    async def start_all(self) -> list[CameraStatus]:
        return await self._broadcast(on=True)

    async def stop_all(self) -> list[CameraStatus]:
        return await self._broadcast(on=False)

    async def get_settings(self, cam_id: str) -> dict:
        """Return current + supported resolution/fps from the camera over BLE.

        Requires the camera to be connected. Returns an empty dict if not.
        """
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is None:
                return {}
            try:
                return await e.driver.get_video_capabilities()
            except Exception as exc:
                log.warning("get_settings failed for %s: %s", cam_id, exc)
                return {}

    async def apply_settings(
        self,
        cam_id: str,
        *,
        resolution: str | None,
        fps: str | None,
        lens: str | None = None,
        hypersmooth: str | None = None,
    ) -> CameraStatus:
        """Send resolution/fps/lens/hypersmooth BLE commands. Camera must be connected and not recording."""
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is None:
                raise RuntimeError(f"camera {cam_id} is not connected")
            if e.status.encoding:
                raise RuntimeError("cannot change settings while recording")
            try:
                await e.driver.set_video_settings(resolution, fps, lens=lens, hypersmooth=hypersmooth)
                # Mirror new values into status immediately
                if resolution is not None:
                    e.status.resolution = resolution
                if fps is not None:
                    e.status.fps = fps
                if lens is not None:
                    e.status.lens = lens
                if hypersmooth is not None:
                    e.status.hypersmooth = hypersmooth
                e.status.last_error = None
            except Exception as exc:
                e.status.last_error = str(exc)
                log.exception("apply_settings failed for %s", cam_id)
                raise
            return e.status.model_copy()

    async def provision_cohn(self, cam_id: str, ssid: str, password: str) -> CameraStatus:
        """Run BLE-based COHN provisioning for one camera.

        Camera config does NOT need to be mode='cohn' — provisioning works in any
        mode. The camera must be DISCONNECTED before provisioning (the BLE
        adapter is exclusive: an existing session would block a fresh one).
        """
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is not None:
                raise RuntimeError(f"camera {cam_id} must be disconnected before provisioning")
            tmp = WirelessGoProDriver(e.config.target, mode="ble")
            try:
                info = await tmp.provision_cohn(ssid, password)
                e.status.cohn_provisioned = True
                e.status.cohn_ip = info.get("ip_address")
                e.status.last_error = None
                log.info("COHN provisioned for %s (ip=%s)", cam_id, e.status.cohn_ip)
            except Exception as exc:
                e.status.last_error = str(exc)
                log.exception("provision_cohn failed for %s", cam_id)
                raise
            return e.status.model_copy()

    async def sync_time(self, cam_id: str) -> None:
        """Set the clock on one connected COHN camera to the server's current time."""
        e = self._entry(cam_id)
        async with e.lock:
            if e.config.mode != "cohn":
                raise RuntimeError(f"camera {cam_id} is not in COHN mode")
            if e.driver is None:
                raise RuntimeError(f"camera {cam_id} is not connected")
            await e.driver.sync_time()

    async def sync_time_all(self) -> dict[str, str]:
        """Set the clock on every connected COHN camera simultaneously.

        Returns a dict of cam_id → "ok" | error_message.
        """
        connected = [
            (cid, e) for cid, e in self._entries.items()
            if e.driver is not None and e.config.mode == "cohn"
        ]
        if not connected:
            return {}

        async def _one(cam_id: str) -> tuple[str, str]:
            try:
                await self.sync_time(cam_id)
                return cam_id, "ok"
            except Exception as exc:
                return cam_id, str(exc)

        results = await asyncio.gather(*[_one(cid) for cid, _ in connected])
        return dict(results)

    async def set_mode(self, cam_id: str, mode: str) -> CameraStatus:
        """Switch the camera's active preset group ('video', 'photo', 'timelapse')."""
        _MODE_TO_GROUP = {"video": 1000, "photo": 1001, "timelapse": 1002}
        if mode not in _MODE_TO_GROUP:
            raise ValueError(f"Unknown mode '{mode}'. Valid: {list(_MODE_TO_GROUP)}")
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is None:
                raise RuntimeError(f"camera {cam_id} is not connected")
            if e.status.encoding:
                raise RuntimeError("cannot change mode while recording")
            try:
                await e.driver.set_preset_group(mode)
                e.status.preset_group = _MODE_TO_GROUP[mode]
                e.status.last_error = None
                log.info("set mode=%s for %s", mode, cam_id)
            except Exception as exc:
                e.status.last_error = str(exc)
                log.exception("set_mode failed for %s", cam_id)
                raise
            return e.status.model_copy()

    async def refresh_status(self, cam_id: str) -> CameraStatus:
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is None:
                return e.status.model_copy()
            try:
                data = await e.driver.get_status()
                for key in ("encoding", "battery_percent", "sd_remaining_sec", "preset_group"):
                    if key in data and data[key] is not None:
                        setattr(e.status, key, data[key])
                e.status.last_error = None
            except Exception as exc:
                e.status.last_error = str(exc)
                log.warning("status refresh failed for %s: %s", cam_id, exc)
            return e.status.model_copy()

    # --- internals -----------------------------------------------------------

    async def _shutter(self, cam_id: str, *, on: bool) -> CameraStatus:
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is None:
                raise RuntimeError(f"camera {cam_id} is not connected")
            try:
                if on:
                    await e.driver.start_recording()
                    e.status.encoding = True
                else:
                    await e.driver.stop_recording()
                    e.status.encoding = False
                e.status.last_error = None
            except (TimeoutError, asyncio.TimeoutError) as exc:
                # BLE can report the shutter result through async status just
                # after the command path times out. If the desired recording
                # state is already visible, keep the session alive instead of
                # showing a false disconnect while the cameras are recording.
                if e.driver is not None:
                    try:
                        data = await e.driver.get_status()
                        for key in ("encoding", "battery_percent", "sd_remaining_sec", "preset_group"):
                            if key in data and data[key] is not None:
                                setattr(e.status, key, data[key])
                        if e.status.encoding is on:
                            e.status.connection = "connected"
                            e.status.last_error = None
                            log.warning(
                                "shutter %s timed out for %s, but camera reached desired state",
                                "start" if on else "stop",
                                cam_id,
                            )
                            return e.status.model_copy()
                    except Exception:
                        log.exception("status check after shutter timeout failed for %s", cam_id)

                # SDK BLE queue locked up and status didn't confirm success —
                # this session is no longer trustworthy.
                e.status.last_error = "BLE timed out — reconnect required"
                e.status.connection = "disconnected"
                drv = e.driver
                e.driver = None
                if drv is not None:
                    try:
                        await drv.close()
                    except Exception:
                        log.exception("close after timeout failed for %s", cam_id)
                log.error("shutter %s timed out for %s — auto-disconnected", "start" if on else "stop", cam_id)
                raise RuntimeError("BLE command timed out; camera auto-disconnected") from exc
            except Exception as exc:
                e.status.last_error = str(exc)
                log.exception("shutter %s failed for %s", "start" if on else "stop", cam_id)
                raise
            return e.status.model_copy()

    async def _broadcast(self, *, on: bool) -> list[CameraStatus]:
        targets = [cid for cid, e in self._entries.items() if e.driver is not None]
        if not targets:
            return []
        coros = [self._shutter(cid, on=on) for cid in targets]
        results = await asyncio.gather(*coros, return_exceptions=True)
        out: list[CameraStatus] = []
        for cid, res in zip(targets, results):
            if isinstance(res, Exception):
                out.append(self._entries[cid].status.model_copy())
            else:
                out.append(res)
        return out

    def _entry(self, cam_id: str) -> _Entry:
        try:
            return self._entries[cam_id]
        except KeyError as exc:
            raise CameraNotFound(cam_id) from exc
