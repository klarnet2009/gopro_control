from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .cohn_db import read_cohn_db_for as _read_cohn_db_for
from .driver import CameraDriver, WirelessGoProDriver, default_driver_factory
from .schemas import CameraConfig, CameraStatus, ScanResult, TimingConfig

log = logging.getLogger(__name__)

DriverFactory = Callable[[CameraConfig], CameraDriver]

# Re-exported for tests that want the canonical default values without
# instantiating a TimingConfig. Live code reads from manager._timing instead.
_DEFAULT_TIMING = TimingConfig()
STOP_GRACE = _DEFAULT_TIMING.stop_grace_sec
POST_STOP_RECOVERY_SEC = _DEFAULT_TIMING.post_stop_recovery_sec
RSSI_POLL_INTERVAL = _DEFAULT_TIMING.rssi_poll_interval_sec


@dataclass
class _Entry:
    config: CameraConfig
    status: CameraStatus
    driver: CameraDriver | None
    lock: asyncio.Lock
    rssi_updated_at: float = field(default=0.0)
    # Monotonic timestamp of the last confirmed stop. Poller ignores encoding=True
    # from the camera for timing.stop_grace_sec after this (camera still finalises file).
    stop_confirmed_at: float = field(default=0.0)
    # Earliest time a start command may be sent after a stop ACK.
    min_start_at: float = field(default=0.0)
    # Supported values per setting field, populated lazily by get_settings().
    # When present, apply_settings() validates payloads against this dict and
    # rejects unsupported values before they reach the SDK. When absent
    # (camera never queried for caps), validation is skipped.
    supported_caps: dict[str, list[str]] | None = field(default=None)


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
        timing: TimingConfig | None = None,
    ) -> None:
        self._timing = timing or TimingConfig()
        # The default factory needs the timing config; bind it via closure so
        # the public DriverFactory protocol stays Callable[[CameraConfig], ...]
        # and existing tests that pass a custom driver_factory keep working.
        if driver_factory is None:
            t = self._timing
            self._driver_factory = lambda cfg: default_driver_factory(cfg, timing=t)
        else:
            self._driver_factory = driver_factory
        self._entries: dict[str, _Entry] = {}
        self._mutate_lock = asyncio.Lock()
        # External "should be recording right now" signal — set by AtemWatcher
        # when the switcher is in record state. Late-connecting cameras consult
        # this so the operator doesn't have to nudge them manually.
        self._armed: bool = False
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

    def set_armed(self, armed: bool) -> None:
        """Tell the manager whether external trigger source is in record state.

        Used by AtemWatcher: when the switcher transitions to recording, calls
        ``set_armed(True)`` so cameras connecting afterwards auto-roll.
        """
        self._armed = armed

    @property
    def armed(self) -> bool:
        return self._armed

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

    async def _do_disconnect(self, e: _Entry, cam_id: str) -> None:
        """Tear down the driver and reset session-scoped entry state.

        Caller must hold ``e.lock``. ``rssi_dbm`` is intentionally preserved
        so the card still shows last-known signal while offline.
        """
        drv = e.driver
        e.driver = None
        if drv is not None:
            try:
                await drv.close()
            except Exception:
                log.exception("close failed for %s", cam_id)
        e.status.connection = "disconnected"
        e.status.encoding = None
        e.status.observers_alive = None
        e.status.observers_total = None
        e.min_start_at = 0.0
        # supported_caps was populated for the previous physical camera; the
        # next session may be a different model after a target/mode change.
        e.supported_caps = None

    async def _disconnect_locked(self, cam_id: str) -> None:
        """Internal helper — must be called only from within _mutate_lock."""
        e = self._entries.get(cam_id)
        if e is None:
            return
        async with e.lock:
            await self._do_disconnect(e, cam_id)

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
            connected_status = e.status.model_copy()

        # ATEM late-arm: if the external trigger is currently in record state
        # but this camera came online after the start command was issued, roll
        # it now so the operator doesn't have to chase it manually. Run after
        # releasing e.lock so _shutter() can re-acquire it cleanly.
        if self._armed and connected_status.encoding is not True:
            try:
                connected_status = await self._shutter(cam_id, on=True)
                log.info("late-armed %s: shutter started to match ATEM record state", cam_id)
            except Exception as exc:
                log.exception("late-arm shutter failed for %s", cam_id)
                # Surface the failure on the camera card; otherwise the user
                # sees a "connected" green tile that silently isn't recording.
                e.status.last_error = f"late-arm failed: {exc}"
                connected_status = e.status.model_copy()
        return connected_status

    async def disconnect(self, cam_id: str) -> CameraStatus:
        e = self._entry(cam_id)
        async with e.lock:
            await self._do_disconnect(e, cam_id)
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
        Caches the supported_* lists on the entry so apply_settings() can
        validate user input without re-querying the camera.
        """
        e = self._entry(cam_id)
        async with e.lock:
            if e.driver is None:
                return {}
            try:
                caps = await e.driver.get_video_capabilities()
            except Exception as exc:
                log.warning("get_settings failed for %s: %s", cam_id, exc)
                return {}
            supported: dict[str, list[str]] = {}
            for cap_key, payload_key in (
                ("supported_resolutions", "resolution"),
                ("supported_fps", "fps"),
                ("supported_lenses", "lens"),
                ("supported_hypersmooth", "hypersmooth"),
            ):
                values = caps.get(cap_key)
                if isinstance(values, list) and values:
                    supported[payload_key] = list(values)
            if supported:
                e.supported_caps = supported
            return caps

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
            if e.supported_caps:
                requested = {
                    "resolution": resolution,
                    "fps": fps,
                    "lens": lens,
                    "hypersmooth": hypersmooth,
                }
                for key, value in requested.items():
                    if value is None:
                        continue
                    allowed = e.supported_caps.get(key)
                    if allowed is None or value in allowed:
                        continue
                    raise ValueError(
                        f"{key}={value!r} is not supported by camera {cam_id} "
                        f"(allowed: {', '.join(allowed)})"
                    )
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

    async def start_preview(
        self,
        cam_id: str,
        *,
        resolution: str = "720",
        fov: str = "WIDE",
    ) -> str:
        """Open the COHN webcam mode and return the RTSP URL the client can stream.

        Camera must be connected and in COHN mode. Acquires the per-camera lock
        for the duration of the BLE/HTTP round-trip so concurrent shutter or
        settings commands don't interleave. The HTTP layer (routes.py) is
        responsible for the ffmpeg/MJPEG transcoding lifecycle on top of this.
        """
        e = self._entry(cam_id)
        if e.config.mode != "cohn":
            raise RuntimeError(f"camera {cam_id} must be in COHN mode for preview")
        if e.status.connection != "connected":
            raise RuntimeError(f"camera {cam_id} is not connected")
        async with e.lock:
            if e.driver is None:
                raise RuntimeError(f"camera {cam_id} driver is not open")
            return await e.driver.start_webcam_rtsp(resolution=resolution, fov=fov)

    async def stop_preview(self, cam_id: str) -> None:
        """Tell the camera to leave webcam mode. Best-effort; no-op if disconnected."""
        try:
            e = self._entry(cam_id)
        except CameraNotFound:
            return
        if e.driver is None:
            return
        async with e.lock:
            if e.driver is None:
                return
            try:
                await e.driver.stop_webcam()
            except Exception:
                log.exception("stop_webcam failed for %s", cam_id)

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
            tmp = WirelessGoProDriver(e.config.target, mode="ble", timing=self._timing)
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
                encoding = data.get("encoding")
                # Suppress spurious encoding=True while GoPro finalises the file
                if encoding and (time.monotonic() - e.stop_confirmed_at < self._timing.stop_grace_sec):
                    encoding = False
                e.status.encoding = encoding
                for key in ("battery_percent", "sd_remaining_sec", "preset_group"):
                    if key in data and data[key] is not None:
                        setattr(e.status, key, data[key])
                e.status.last_error = None
            except Exception as exc:
                e.status.last_error = str(exc)
                log.warning("status refresh failed for %s: %s", cam_id, exc)
            if time.monotonic() - e.rssi_updated_at >= self._timing.rssi_poll_interval_sec:
                try:
                    e.status.rssi_dbm = await e.driver.get_rssi()
                    e.rssi_updated_at = time.monotonic()
                except Exception as exc:
                    log.debug("rssi refresh failed for %s: %s", cam_id, exc)
            # Optional: drivers that track BLE telemetry observers expose health.
            # Driver Protocol does not require this, so check before calling.
            if hasattr(e.driver, "get_observer_health"):
                try:
                    health = e.driver.get_observer_health()
                    e.status.observers_alive = health.get("alive")
                    e.status.observers_total = health.get("total")
                except Exception as exc:
                    log.debug("observer health read failed for %s: %s", cam_id, exc)
            return e.status.model_copy()

    # --- internals -----------------------------------------------------------

    async def _shutter(self, cam_id: str, *, on: bool) -> CameraStatus:
        e = self._entry(cam_id)

        # Sleep BEFORE the lock for start commands so refresh_status can still
        # acquire the lock and serve status while we wait out the recovery window.
        if on:
            delay = e.min_start_at - time.monotonic()
            if delay > 0:
                log.debug(
                    "delaying start for %s by %.2fs (post-stop recovery)",
                    cam_id, delay,
                )
                await asyncio.sleep(delay)

        async with e.lock:
            if e.driver is None:
                raise RuntimeError(f"camera {cam_id} is not connected")

            # De-dup: if concurrent callers raced through the wait, only the
            # first one should actually send the BLE command.
            if on and e.status.encoding is True:
                return e.status.model_copy()
            if not on and e.status.encoding is False:
                return e.status.model_copy()

            try:
                if on:
                    await e.driver.start_recording()
                    e.status.encoding = True
                else:
                    await e.driver.stop_recording()
                    e.status.encoding = False
                    now = time.monotonic()
                    e.stop_confirmed_at = now
                    e.min_start_at = now + self._timing.post_stop_recovery_sec
                e.status.last_error = None
            except TimeoutError as exc:
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
                            # Apply the same post-stop bookkeeping as the
                            # success path: the camera DID reach the desired
                            # state, so encoding-finalize debouncing and the
                            # next-start cooldown must still fire.
                            if not on:
                                now = time.monotonic()
                                e.stop_confirmed_at = now
                                e.min_start_at = now + self._timing.post_stop_recovery_sec
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
                log.error(
                    "shutter %s timed out for %s — auto-disconnected",
                    "start" if on else "stop", cam_id,
                )
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
