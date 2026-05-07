"""Thin adapter over open_gopro.WirelessGoPro.

Isolates SDK details so the manager and tests don't depend on open_gopro internals.
The real driver imports open_gopro lazily — tests inject a fake factory and never load it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .capabilities import _MODEL_CAPS, _model_caps_for
from .schemas import CameraConfig, TimingConfig
from .settings_map import (
    _FPS_MAP,
    _FPS_REVERSE,
    _HYPERSMOOTH_LABELS,
    _HYPERSMOOTH_REVERSE,
    _LENS_LABELS,
    _LENS_REVERSE,
    _PRESET_GROUPS,
    _RESOLUTION_MAP,
    _RESOLUTION_REVERSE,
    _enum_to_fps,
    _enum_to_hypersmooth,
    _enum_to_lens,
    _enum_to_resolution,
    _valid_status_value,
)

# Re-exported for backwards compatibility — code that did
# ``from gopro_mgmt.driver import _RESOLUTION_MAP`` keeps working.
__all__ = (
    "BLE_CMD_TIMEOUT",
    "BLE_DETAIL_TIMEOUT",
    "CameraDriver",
    "COHN_DB_PATH",
    "COHN_KEEPALIVE_SEC",
    "default_driver_factory",
    "HTTP_CMD_TIMEOUT",
    "HTTP_SHUTTER_TIMEOUT",
    "WirelessGoProDriver",
    "_check_resp",
    "_enum_to_fps",
    "_enum_to_hypersmooth",
    "_enum_to_lens",
    "_enum_to_resolution",
    "_FPS_MAP",
    "_FPS_REVERSE",
    "_HYPERSMOOTH_LABELS",
    "_HYPERSMOOTH_REVERSE",
    "_LENS_LABELS",
    "_LENS_REVERSE",
    "_MODEL_CAPS",
    "_model_caps_for",
    "_PRESET_GROUPS",
    "_RESOLUTION_MAP",
    "_RESOLUTION_REVERSE",
    "_unwrap",
    "_valid_status_value",
)

log = logging.getLogger(__name__)

# Module-level timing constants are kept as defaults so test stubs and
# legacy import sites still work; live code reads from self._timing instead.
# We derive them from TimingConfig so the two cannot drift apart silently.
_DEFAULT_TIMING = TimingConfig()
BLE_CMD_TIMEOUT      = _DEFAULT_TIMING.ble_cmd_timeout_sec
BLE_DETAIL_TIMEOUT   = _DEFAULT_TIMING.ble_detail_timeout_sec
HTTP_CMD_TIMEOUT     = _DEFAULT_TIMING.http_cmd_timeout_sec
HTTP_SHUTTER_TIMEOUT = _DEFAULT_TIMING.http_shutter_timeout_sec
COHN_KEEPALIVE_SEC   = _DEFAULT_TIMING.cohn_keepalive_sec

# Resolve cohn_db.json relative to project root, NOT cwd. From this file
# (src/gopro_mgmt/driver.py), parents[2] is the project root. open_gopro
# uses TinyDB to read/write per-camera credentials there.
COHN_DB_PATH = Path(__file__).resolve().parents[2] / "cohn_db.json"


class CameraDriver(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    def get_model(self) -> str | None: ...
    async def start_recording(self) -> None: ...
    async def stop_recording(self) -> None: ...
    async def get_status(self) -> dict[str, Any]: ...
    async def get_rssi(self) -> int | None: ...
    async def get_current_video_settings(self) -> dict[str, Any]: ...
    async def get_video_capabilities(self) -> dict[str, Any]: ...
    async def set_video_settings(
        self,
        resolution: str | None,
        fps: str | None,
        lens: str | None = None,
        hypersmooth: str | None = None,
    ) -> None: ...
    async def set_preset_group(self, mode: str) -> None: ...
    async def sync_time(self) -> None: ...
    async def provision_cohn(self, ssid: str, password: str) -> dict[str, Any]: ...
    async def start_webcam_rtsp(self, *, resolution: str = "720", fov: str = "WIDE") -> str: ...
    async def stop_webcam(self) -> None: ...


class WirelessGoProDriver:
    def __init__(
        self,
        target: str,
        *,
        mode: str = "ble",
        timing: TimingConfig | None = None,
    ) -> None:
        self._target = target
        self._mode = mode                  # "ble" | "ble+wifi" | "cohn" | "ble+cohn"
        self._timing = timing or TimingConfig()
        self._gopro: Any | None = None
        self._model: str | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._ble_detail_tasks: list[asyncio.Task[None]] = []
        self._ble_register_lock = asyncio.Lock()
        # _ble_*_cache is read by status/settings APIs and written by 7
        # observer tasks. Single-key get/set is atomic under asyncio (no
        # preempt mid-op), but iterating the dict races with writes and can
        # raise "dictionary changed size during iteration". Always snapshot
        # via dict(...) before iterating, and use _snapshot_ble_caches()
        # when callers need a coherent multi-key view.
        self._ble_status_cache: dict[str, Any] = {}
        self._ble_setting_cache: dict[str, Any] = {}
        # Observer health: maps the BLE field name to one of
        # {"starting", "alive", "retrying", "dead"}. Populated by the
        # observer wrapper; surfaced via get_observer_health() so the manager
        # can show "5/7 telemetry sources alive" in the UI.
        self._observer_status: dict[str, str] = {}

    # ── Transport helpers ────────────────────────────────────────────────
    @property
    def _use_ble(self) -> bool:
        """True when BLE is the primary control transport for this driver."""
        return self._mode in ("ble", "ble+wifi", "ble+cohn")

    @property
    def _use_cohn_http(self) -> bool:
        """True when COHN HTTP is available (COHN-only or BLE+COHN dual mode)."""
        return self._mode in ("cohn", "ble+cohn")

    async def open(self) -> None:
        from open_gopro import WirelessGoPro  # lazy import
        Iface = WirelessGoPro.Interface

        if self._mode == "cohn":
            # COHN-only mode: HTTP transport via stored cohn_db credentials.
            # The SDK's wait_until_ready() must run normally so cohn._supported
            # flips to True; do NOT install the _skip_cohn_wait hack.
            self._gopro = WirelessGoPro(
                target=self._target,
                interfaces={Iface.COHN},
                wifi_adapter=_NullWifiController,
                cohn_db=COHN_DB_PATH,
            )
            await self._gopro.open()
            self._model = await self._read_model_http()
            self._start_keepalive()
            log.info("opened camera target=%s mode=cohn model=%s", self._target, self._model)
            return

        if self._mode == "ble+cohn":
            # Dual mode: BLE for control (shutter, status, settings) +
            # COHN HTTP for preview and clock sync.
            # Do NOT skip cohn.wait_until_ready — the camera is provisioned and
            # will respond. NullWifiController is correct here because we connect
            # to the camera's home network IP, not a camera-hosted AP.
            self._gopro = WirelessGoPro(
                target=self._target,
                interfaces={Iface.BLE, Iface.COHN},
                wifi_adapter=_NullWifiController,
                cohn_db=COHN_DB_PATH,
            )
            await self._gopro.open()
            self._model = await self._read_model()   # BLE is available → prefer it
            self._start_keepalive()
            # Start BLE push-notification observers so battery/SD/settings flow
            # in real-time from the BLE connection, same as pure BLE mode.
            self._start_ble_detail_observers()
            log.info("opened camera target=%s mode=ble+cohn model=%s", self._target, self._model)
            return

        # ── BLE / BLE+WiFi paths (legacy behaviour) ──────────────────────
        interfaces = {Iface.BLE}
        if self._mode == "ble+wifi":
            interfaces.add(Iface.WIFI_AP)

        # CRITICAL: open_gopro 0.22 unconditionally constructs a WifiCli even
        # when WIFI_AP is not in `interfaces`. On Windows, WifiCli (NetshWireless)
        # has a __del__ that runs `netsh wlan disconnect` *without an interface
        # argument*, which kicks every Wi-Fi adapter on the host off its current
        # network. We swap in a no-op controller for BLE-only mode.
        kwargs: dict[str, Any] = {"cohn_db": COHN_DB_PATH}
        if self._mode == "ble":
            kwargs["wifi_adapter"] = _NullWifiController

        self._gopro = WirelessGoPro(target=self._target, interfaces=interfaces, **kwargs)

        # Workaround for open_gopro 0.22: CohnFeature.open() starts a BLE COHN
        # status observer even when COHN is not requested. On COHN-capable
        # cameras this observer can interleave RESPONSE_GET_COHN_STATUS packets
        # with normal BLE setting/status responses, locking the SDK response
        # queue and leaving battery/settings blank. BLE / BLE+WiFi sessions do
        # not need this feature, so make it inert for the long-lived connection.
        async def _skip_cohn_open(*_args: Any, **_kwargs: Any) -> None:
            return

        async def _skip_cohn_wait() -> None:
            return

        self._gopro.cohn.open = _skip_cohn_open
        self._gopro.cohn.wait_until_ready = _skip_cohn_wait

        await self._gopro.open()
        self._model = await self._read_model()
        self._start_ble_detail_observers()
        log.info("opened camera target=%s mode=%s model=%s", self._target, self._mode, self._model)

    async def close(self) -> None:
        # Cancel keep-alive first so it doesn't try to use a closing session.
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                # The task we just cancelled — expected. Do NOT swallow
                # CancelledError if it bubbled up from our own outer task
                # being cancelled; that gets re-raised below by the bare path.
                pass
            except Exception:
                log.exception("keepalive task raised during close")
            self._keepalive_task = None
        for task in self._ble_detail_tasks:
            task.cancel()
        if self._ble_detail_tasks:
            await asyncio.gather(*self._ble_detail_tasks, return_exceptions=True)
            self._ble_detail_tasks.clear()
        if self._gopro is None:
            return
        try:
            await self._gopro.close()
        finally:
            self._gopro = None

    # ── Keep-alive (COHN only) ────────────────────────────────────────────
    def _start_keepalive(self) -> None:
        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(self._timing.cohn_keepalive_sec)
                    if self._gopro is None:
                        return
                    try:
                        # Polling get_camera_state resets the camera's idle timer
                        # — equivalent to a dedicated keep-alive on every firmware.
                        await asyncio.wait_for(
                            self._gopro.http_command.get_camera_state(),
                            timeout=self._timing.http_cmd_timeout_sec,
                        )
                    except Exception as exc:
                        log.debug("cohn keepalive failed for %s: %s", self._target, exc)
            except asyncio.CancelledError:
                return
        self._keepalive_task = asyncio.create_task(_loop(), name=f"cohn-keepalive-{self._target}")

    async def _read_model_http(self) -> str | None:
        """Read camera model via the HTTP get_camera_info endpoint."""
        if self._gopro is None:
            return None
        try:
            resp = await asyncio.wait_for(
                self._gopro.http_command.get_camera_info(), timeout=self._timing.http_cmd_timeout_sec,
            )
            info = _unwrap(resp)
            for attr in ("model_name", "modelName", "model"):
                v = getattr(info, attr, None)
                if v is None and isinstance(info, dict):
                    v = info.get(attr)
                if v:
                    return str(v)
        except Exception as exc:
            log.debug("http get_camera_info failed for %s: %s", self._target, exc)
        return None

    def get_model(self) -> str | None:
        """Return the cached camera model name (populated during open())."""
        return self._model

    def _cached_encoding(self) -> bool | None:
        if self._gopro is None:
            return None
        value = getattr(self._gopro, "_encoding", None)
        return bool(value) if value is not None else None

    def _cached_busy(self) -> bool | None:
        if self._gopro is None:
            return None
        value = getattr(self._gopro, "_busy", None)
        return bool(value) if value is not None else None

    # Observer auto-restart: SDK observables can drop with transient errors
    # (push notification overrun, momentary BLE stack hiccup). We retry with
    # exponential backoff until the session closes; after MAX_RETRIES in the
    # current "burst" we give up and let the field go stale.
    _OBSERVER_BACKOFF_BASE_SEC = 1.0
    _OBSERVER_BACKOFF_MAX_SEC = 30.0
    _OBSERVER_MAX_RETRIES = 6

    def _start_ble_detail_observers(self) -> None:
        if self._gopro is None or self._mode == "cohn":
            return
        gp = self._gopro
        specs = (
            ("status", "battery_percent", gp.ble_status.internal_battery_percentage, int),
            ("status", "sd_remaining_sec", gp.ble_status.remaining_video_time, int),
            ("status", "preset_group", gp.ble_status.preset_group, int),
            ("setting", "resolution", gp.ble_setting.video_resolution, _enum_to_resolution),
            ("setting", "fps", gp.ble_setting.frames_per_second, _enum_to_fps),
            ("setting", "lens", gp.ble_setting.video_lens, _enum_to_lens),
            ("setting", "hypersmooth", gp.ble_setting.hypersmooth, _enum_to_hypersmooth),
        )
        for cache_name, key, attr, converter in specs:
            self._observer_status[key] = "starting"
            self._ble_detail_tasks.append(
                asyncio.create_task(
                    self._observe_ble_value_with_restart(cache_name, key, attr, converter),
                    name=f"ble-observe-{key}-{self._target}",
                )
            )

    def get_observer_health(self) -> dict[str, int]:
        """Return ``{"alive": n, "total": m}`` for the BLE telemetry observers.

        ``alive`` includes observers in ``"alive"`` or ``"retrying"`` (still
        attempting); ``"starting"`` and ``"dead"`` count as not alive. COHN
        mode reports ``{"alive": 0, "total": 0}`` since no observers run.
        """
        snapshot = dict(self._observer_status)
        total = len(snapshot)
        alive = sum(1 for s in snapshot.values() if s in ("alive", "retrying"))
        return {"alive": alive, "total": total}

    async def _observe_ble_value_with_restart(
        self, cache_name: str, key: str, attr: Any, converter: Any,
    ) -> None:
        """Run the observer loop and restart it after non-cancellation errors.

        After MAX_RETRIES consecutive failures we stop trying — the field will
        report stale data, but the session remains usable for commands.
        """
        retries = 0
        while True:
            # Stay in "starting" until _observe_ble_value flips us to "alive"
            # right after the SDK observable is registered — that way
            # get_observer_health() doesn't claim a source is live before any
            # value has actually flowed.
            self._observer_status[key] = "starting"
            try:
                await self._observe_ble_value(cache_name, key, attr, converter)
                # Clean close: the SDK observable yielded no more values. In
                # open_gopro 0.22 this normally means the BLE session is going
                # away (close()), but transient queue overflow can also close
                # an observable. Treat it as a retryable event so a flaky
                # session doesn't permanently freeze telemetry.
                log.warning(
                    "BLE observer for %s on target=%s closed cleanly — restarting",
                    key, self._target,
                )
                self._observer_status[key] = "retrying"
            except asyncio.CancelledError:
                self._observer_status[key] = "dead"
                raise
            except Exception as exc:
                retries += 1
                if retries > self._OBSERVER_MAX_RETRIES:
                    self._observer_status[key] = "dead"
                    log.error(
                        "BLE observer for %s on target=%s gave up after %d retries: %s "
                        "— telemetry stale until reconnect",
                        key, self._target, self._OBSERVER_MAX_RETRIES, exc,
                    )
                    return
                self._observer_status[key] = "retrying"
                delay = min(
                    self._OBSERVER_BACKOFF_BASE_SEC * (2 ** (retries - 1)),
                    self._OBSERVER_BACKOFF_MAX_SEC,
                )
                log.warning(
                    "BLE observer for %s on target=%s exited (%s) — restart %d/%d in %.1fs",
                    key, self._target, exc, retries, self._OBSERVER_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                continue
            # Successful clean-close path: brief backoff before reattaching
            # so we don't hot-loop if the SDK closes the observable repeatedly.
            await asyncio.sleep(self._OBSERVER_BACKOFF_BASE_SEC)

    async def _observe_ble_value(self, cache_name: str, key: str, attr: Any, converter: Any) -> None:
        async with self._ble_register_lock:
            result = await asyncio.wait_for(attr.get_value_observable(), timeout=self._timing.ble_cmd_timeout_sec)
        observable = result.unwrap()
        # Flip to "alive" only once the observable is in hand. The first cached
        # value (if any) and the streaming loop both deliver real telemetry.
        self._observer_status[key] = "alive"
        self._cache_ble_value(cache_name, key, getattr(observable, "current", None), converter)
        async for value in observable.observe(debug_id=f"{self._target}:{key}"):
            self._cache_ble_value(cache_name, key, value, converter)

    def _cache_ble_value(self, cache_name: str, key: str, raw: Any, converter: Any) -> None:
        if raw is None:
            return
        try:
            value = converter(raw) if converter is not None else raw
        except Exception:
            log.debug("could not convert BLE %s=%r for target=%s", key, raw, self._target)
            return
        if value is None:
            return
        if cache_name == "status":
            value = _valid_status_value(key, value)
            if value is None:
                log.debug("discarding invalid BLE %s=%r for target=%s", key, raw, self._target)
                return
        cache = self._ble_status_cache if cache_name == "status" else self._ble_setting_cache
        cache[key] = value

    async def _read_model(self) -> str | None:
        """Read camera model via the official Open GoPro BLE GET_HW_INFO command.

        Method 1 (primary): ble_command.get_hardware_info() → CameraInfo.model_name
          Returns the official model string, e.g. "HERO12 Black".

        Method 2 (fallback): BLE advertisement name from BleClient._device.name.
          Cameras typically advertise as "GoPro <target>" without model info, so
          this path usually yields nothing useful and is skipped when result == "GoPro".

        Returns None silently on any failure.
        """
        if self._gopro is None:
            return None

        # Method 1: official BLE hardware-info command
        try:
            resp = await asyncio.wait_for(
                self._gopro.ble_command.get_hardware_info(),
                timeout=self._timing.ble_cmd_timeout_sec,
            )
            info = _unwrap(resp)
            model_name = getattr(info, "model_name", None)
            if model_name and str(model_name).strip():
                result = str(model_name).strip()
                log.debug("model from get_hardware_info for target=%s: %r", self._target, result)
                return result
        except Exception as exc:
            log.debug("get_hardware_info failed for target=%s: %s", self._target, exc)

        # Method 2: BLE advertisement name (low-value fallback)
        try:
            ble = getattr(self._gopro, "_ble", None)
            device = getattr(ble, "_device", None)
            raw_name: str | None = getattr(device, "name", None)
            if not raw_name:
                identifier = getattr(ble, "identifier", None) or ""
                if ": " in identifier:
                    raw_name = identifier.split(": ", 1)[1]
            if raw_name:
                clean = re.sub(r"\s+[A-Za-z0-9]{4}$", "", raw_name).strip()
                result = clean or raw_name
                if result and result.lower() != "gopro":
                    return result
        except Exception as exc:
            log.debug("BLE name fallback failed for target=%s: %s", self._target, exc)

        return None

    async def start_recording(self) -> None:
        self._require_open()
        from open_gopro.models.constants import Toggle

        if not self._use_ble:
            # ── HTTP path (COHN-only mode): ensure Video preset group ─────
            try:
                state_resp = await asyncio.wait_for(
                    self._gopro.http_command.get_camera_state(),
                    timeout=self._timing.http_cmd_timeout_sec,
                )
                state = _unwrap(state_resp)
                statuses = (state.get("status") or {}) if isinstance(state, dict) else {}
                # Status keys come back as either int or string depending on firmware.
                current_group = statuses.get(96, statuses.get("96"))
                if current_group is not None and int(current_group) != 1000:
                    log.info(
                        "camera %s is in preset group %s — switching to VIDEO (cohn) before rolling",
                        self._target, current_group,
                    )
                    sw_resp = await asyncio.wait_for(
                        self._gopro.http_command.load_preset_group(group=1000),
                        timeout=self._timing.http_cmd_timeout_sec,
                    )
                    _check_resp(sw_resp)
            except Exception as exc:
                log.warning(
                    "could not ensure video mode for %s (cohn): %s — attempting record anyway",
                    self._target, exc,
                )
            resp = await asyncio.wait_for(
                self._gopro.http_command.set_shutter(shutter=Toggle.ENABLE),
                timeout=self._timing.http_shutter_timeout_sec,
            )
            _check_resp(resp)
            return

        # ── BLE path (existing behaviour) ─────────────────────────────────
        from open_gopro.models import proto  # protobuf enums — not in constants

        # ── ensure Video preset group is active before rolling ────────────
        # If the camera is in Photo or Timelapse mode, set_shutter(ENABLE) would
        # take a photo / start a timelapse instead of a video recording.
        # Status ID 96 returns the active preset group as a raw int:
        #   1000 = PRESET_GROUP_ID_VIDEO
        #   1001 = PRESET_GROUP_ID_PHOTO
        #   1002 = PRESET_GROUP_ID_TIMELAPSE
        try:
            group_resp = await asyncio.wait_for(
                self._gopro.ble_status.preset_group.get_value(),
                timeout=self._timing.ble_cmd_timeout_sec,
            )
            current_group = _unwrap(group_resp)
            video_group_id = proto.EnumPresetGroup.PRESET_GROUP_ID_VIDEO
            if current_group != video_group_id:
                log.info(
                    "camera %s is in preset group %s — switching to VIDEO before rolling",
                    self._target, current_group,
                )
                switch_resp = await asyncio.wait_for(
                    self._gopro.ble_command.load_preset_group(group=video_group_id),
                    timeout=self._timing.ble_cmd_timeout_sec,
                )
                _check_resp(switch_resp)
                log.info("switched camera %s to VIDEO preset group", self._target)
        except Exception as exc:
            # Non-fatal: if we cannot read/switch mode, try to record anyway.
            # The worst case is the camera ignores the shutter command.
            log.warning(
                "could not ensure video mode for %s: %s — attempting record anyway",
                self._target, exc,
            )

        resp = await asyncio.wait_for(
            self._gopro.ble_command.set_shutter(shutter=Toggle.ENABLE),
            timeout=self._timing.ble_cmd_timeout_sec,
        )
        _check_resp(resp)

    async def stop_recording(self) -> None:
        self._require_open()
        from open_gopro.models.constants import Toggle

        if not self._use_ble:
            resp = await asyncio.wait_for(
                self._gopro.http_command.set_shutter(shutter=Toggle.DISABLE),
                timeout=self._timing.http_cmd_timeout_sec,
            )
            _check_resp(resp)
            return

        resp = await asyncio.wait_for(
            self._gopro.ble_command.set_shutter(shutter=Toggle.DISABLE),
            timeout=self._timing.ble_cmd_timeout_sec,
        )
        _check_resp(resp)

    async def get_status(self) -> dict[str, Any]:
        self._require_open()
        gp = self._gopro

        if not self._use_ble:
            # Single HTTP call returns all statuses at once.
            state_resp = await asyncio.wait_for(
                gp.http_command.get_camera_state(), timeout=self._timing.http_cmd_timeout_sec,
            )
            state = _unwrap(state_resp)
            statuses = (state.get("status") or {}) if isinstance(state, dict) else {}

            def _get(sid: int) -> Any:
                # Keys may be int or string depending on firmware revision.
                return statuses.get(sid, statuses.get(str(sid)))

            encoding = _get(8)
            battery  = _get(70)
            sd       = _get(69)
            pg       = _get(96)
            return {
                "encoding":         bool(encoding) if encoding is not None else None,
                "battery_percent":  int(battery)   if battery  is not None else None,
                "sd_remaining_sec": int(sd)        if sd       is not None else None,
                "preset_group":     int(pg)        if pg       is not None else None,
            }

        ble_detail_timeout = self._timing.ble_detail_timeout_sec

        async def _read_status(attr: Any, timeout: float = ble_detail_timeout) -> Any:
            try:
                return _unwrap(await asyncio.wait_for(attr.get_value(), timeout))
            except Exception as exc:
                log.debug("could not read BLE status for target=%s: %s", self._target, exc)
                return None

        encoding = self._cached_encoding()
        if encoding is None:
            raw_encoding = await _read_status(gp.ble_status.encoding)
            encoding = bool(raw_encoding) if raw_encoding is not None else None

        battery, sd_remaining, pg = await asyncio.gather(
            _read_status(gp.ble_status.internal_battery_percentage),
            _read_status(gp.ble_status.remaining_video_time),
            _read_status(gp.ble_status.preset_group),
        )

        battery = _valid_status_value("battery_percent", battery)
        sd_remaining = _valid_status_value("sd_remaining_sec", sd_remaining)
        pg = _valid_status_value("preset_group", pg)

        if battery is not None:
            self._ble_status_cache["battery_percent"] = battery
        else:
            battery = self._ble_status_cache.get("battery_percent")
        if sd_remaining is not None:
            self._ble_status_cache["sd_remaining_sec"] = sd_remaining
        else:
            sd_remaining = self._ble_status_cache.get("sd_remaining_sec")
        if pg is not None:
            self._ble_status_cache["preset_group"] = pg
        else:
            pg = self._ble_status_cache.get("preset_group")
        preset_group = int(pg) if pg is not None else None

        return {
            "encoding":      bool(encoding)      if encoding    is not None else None,
            "battery_percent": int(battery)       if battery     is not None else None,
            "sd_remaining_sec": int(sd_remaining) if sd_remaining is not None else None,
            "preset_group":  preset_group,
        }

    async def get_rssi(self) -> int | None:
        """Return BLE RSSI in dBm for the connected device, or None if unavailable.

        COHN mode has no BLE connection, so always returns None.
        On BLE/BLE+WiFi: WirelessGoPro._ble is GoProBle (communicator_interface.py),
        which holds a BleClient at _ble._handle (bleak.BleakClient). On macOS/
        CoreBluetooth, BleakClient.get_rssi() issues a readRSSI() request.
        """
        if self._gopro is None or not self._use_ble:
            return None
        try:
            # open_gopro path: WirelessGoPro._ble (GoProBle) → ._ble (BleClient) → ._handle (BleakClient)
            ble_communicator = getattr(self._gopro, "_ble", None)
            ble_client = getattr(ble_communicator, "_ble", None)
            handle = getattr(ble_client, "_handle", None)
            if handle is not None and hasattr(handle, "get_rssi"):
                rssi = await asyncio.wait_for(handle.get_rssi(), timeout=3.0)
                if isinstance(rssi, (int, float)):
                    return int(rssi)
        except Exception as exc:
            log.debug("get_rssi failed for target=%s: %s", self._target, exc)
        return None

    async def get_current_video_settings(self) -> dict[str, Any]:
        """Read current resolution, fps, lens, and hypersmooth.

        BLE: queries each ble_setting.<x>.get_value() individually.
        COHN: parses the cached camera state JSON (settings IDs 2/3/121/135).

        Returns a dict with whichever keys were successfully read:
          resolution, fps, lens, hypersmooth.
        Missing values are omitted. Used to populate CameraStatus on connect.
        """
        self._require_open()
        gp = self._gopro
        result: dict[str, Any] = {}

        if not self._use_ble:
            try:
                from open_gopro.models.constants.settings import (
                    FramesPerSecond,
                    Hypersmooth,
                    VideoLens,
                    VideoResolution,
                )
            except Exception:
                return result
            try:
                state_resp = await asyncio.wait_for(
                    gp.http_command.get_camera_state(), timeout=self._timing.http_cmd_timeout_sec,
                )
                state = _unwrap(state_resp)
                settings = (state.get("settings") or {}) if isinstance(state, dict) else {}
            except Exception as exc:
                log.debug("cohn get_camera_state failed for %s: %s", self._target, exc)
                return result

            def _settings_get(sid: int) -> Any:
                return settings.get(sid, settings.get(str(sid)))

            _ID_MAP = (
                (2,   "resolution",  VideoResolution,  _enum_to_resolution),
                (3,   "fps",         FramesPerSecond,  _enum_to_fps),
                (121, "lens",        VideoLens,        _enum_to_lens),
                (135, "hypersmooth", Hypersmooth,      _enum_to_hypersmooth),
            )
            for sid, key, enum_cls, converter in _ID_MAP:
                raw = _settings_get(sid)
                if raw is None:
                    continue
                try:
                    enum_val = enum_cls(int(raw))
                    converted = converter(enum_val)
                    if converted is not None:
                        result[key] = converted
                except Exception:
                    log.debug("could not parse %s id=%s val=%s", key, sid, raw)
            return result

        _reads = (
            ("video_resolution", "resolution", _enum_to_resolution),
            ("frames_per_second", "fps",        _enum_to_fps),
            ("video_lens",        "lens",        _enum_to_lens),
            ("hypersmooth",       "hypersmooth", _enum_to_hypersmooth),
        )
        for ble_attr, key, converter in _reads:
            try:
                val = _unwrap(await asyncio.wait_for(
                    getattr(gp.ble_setting, ble_attr).get_value(),
                    timeout=self._timing.ble_detail_timeout_sec,
                ))
                if val is not None:
                    converted = converter(val)
                    if converted is not None:
                        result[key] = converted
                        self._ble_setting_cache[key] = converted
            except Exception:
                log.debug("could not read %s for target=%s", ble_attr, self._target)

        # Snapshot: observers may be writing concurrently to the cache.
        for key, value in dict(self._ble_setting_cache).items():
            result.setdefault(key, value)

        return result

    async def get_video_capabilities(self) -> dict[str, Any]:
        """Query current resolution/fps and the lists the camera considers valid.

        Returns a dict with keys:
          resolution          – current value as friendly key, e.g. "1080"
          fps                 – current value as friendly key, e.g. "60"
          supported_resolutions – sorted list of friendly keys the camera supports
          supported_fps         – sorted list of friendly keys the camera supports

        Falls back to our full static list when the camera doesn't expose
        capabilities (older firmware) or the BLE query fails.
        """
        self._require_open()
        gp = self._gopro
        result: dict[str, Any] = {}

        if not self._use_ble:
            # ── COHN-only: read current values from camera state, fall back to
            #    static model caps for the supported lists. http_setting in
            #    SDK 0.22 doesn't reliably expose get_capabilities_values().
            cur = await self.get_current_video_settings()
            result.update(cur)
            mc = _model_caps_for(self._model)
            result["supported_resolutions"] = mc["resolutions"] if mc else list(_RESOLUTION_MAP.keys())
            result["supported_fps"]         = mc["fps"]         if mc else list(_FPS_MAP.keys())
            result["supported_lenses"]      = mc["lenses"]      if mc else list(_LENS_LABELS.values())
            result["supported_hypersmooth"] = mc["hypersmooth"] if mc else list(_HYPERSMOOTH_LABELS.values())
            log.info(
                "cohn capabilities for %s: res=%s fps=%s lens=%s hs=%s",
                self._target,
                result.get("resolution"),
                result.get("fps"),
                result.get("lens"),
                result.get("hypersmooth"),
            )
            return result

        # ── current values ────────────────────────────────────────────────
        # Snapshot guards against concurrent observer writes during update.
        result.update(dict(self._ble_setting_cache))

        try:
            val = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_resolution.get_value(), self._timing.ble_detail_timeout_sec,
            ))
            if val is not None:
                converted = _enum_to_resolution(val)
                if converted is not None:
                    result["resolution"] = converted
                    self._ble_setting_cache["resolution"] = converted
        except Exception:
            log.debug("could not read current resolution for %s", self._target)

        try:
            val = _unwrap(await asyncio.wait_for(
                gp.ble_setting.frames_per_second.get_value(), self._timing.ble_detail_timeout_sec,
            ))
            if val is not None:
                converted = _enum_to_fps(val)
                if converted is not None:
                    result["fps"] = converted
                    self._ble_setting_cache["fps"] = converted
        except Exception:
            log.debug("could not read current fps for %s", self._target)

        # ── capability queries (Open GoPro BLE spec 0x13) ─────────────────
        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_resolution.get_capabilities(), self._timing.ble_detail_timeout_sec,
            ))
            if caps:
                result["supported_resolutions"] = sorted(
                    [k for k in (_enum_to_resolution(v) for v in caps) if k is not None],
                    key=lambda k: list(_RESOLUTION_MAP.keys()).index(k)
                    if k in _RESOLUTION_MAP else 99,
                )
            else:
                raise ValueError("empty capabilities list")
        except Exception:
            log.debug("resolution capabilities unavailable for %s — using model/static fallback", self._target)
            mc = _model_caps_for(self._model)
            result["supported_resolutions"] = mc["resolutions"] if mc else list(_RESOLUTION_MAP.keys())

        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.frames_per_second.get_capabilities(), self._timing.ble_detail_timeout_sec,
            ))
            if caps:
                result["supported_fps"] = sorted(
                    [k for k in (_enum_to_fps(v) for v in caps) if k is not None],
                    key=lambda k: int(k),
                    reverse=True,
                )
            else:
                raise ValueError("empty capabilities list")
        except Exception:
            log.debug("fps capabilities unavailable for %s — using model/static fallback", self._target)
            mc = _model_caps_for(self._model)
            result["supported_fps"] = mc["fps"] if mc else list(_FPS_MAP.keys())

        # ── lens current + capabilities ───────────────────────────────────
        try:
            val = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_lens.get_value(), self._timing.ble_detail_timeout_sec,
            ))
            if val is not None:
                converted = _enum_to_lens(val)
                if converted is not None:
                    result["lens"] = converted
                    self._ble_setting_cache["lens"] = converted
        except Exception:
            log.debug("could not read current lens for %s", self._target)

        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_lens.get_capabilities(), self._timing.ble_detail_timeout_sec,
            ))
            if caps:
                result["supported_lenses"] = [
                    lbl for lbl in (_enum_to_lens(v) for v in caps) if lbl is not None
                ]
            else:
                raise ValueError("empty capabilities list")
        except Exception:
            log.debug("lens capabilities unavailable for %s — using model/static fallback", self._target)
            mc = _model_caps_for(self._model)
            result["supported_lenses"] = mc["lenses"] if mc else list(_LENS_LABELS.values())

        # ── hypersmooth current + capabilities ────────────────────────────
        try:
            val = _unwrap(await asyncio.wait_for(
                gp.ble_setting.hypersmooth.get_value(), self._timing.ble_detail_timeout_sec,
            ))
            if val is not None:
                converted = _enum_to_hypersmooth(val)
                if converted is not None:
                    result["hypersmooth"] = converted
                    self._ble_setting_cache["hypersmooth"] = converted
        except Exception:
            log.debug("could not read current hypersmooth for %s", self._target)

        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.hypersmooth.get_capabilities(), self._timing.ble_detail_timeout_sec,
            ))
            if caps:
                result["supported_hypersmooth"] = [
                    lbl for lbl in (_enum_to_hypersmooth(v) for v in caps) if lbl is not None
                ]
            else:
                raise ValueError("empty capabilities list")
        except Exception:
            log.debug("hypersmooth capabilities unavailable for %s — using model/static fallback", self._target)
            mc = _model_caps_for(self._model)
            result["supported_hypersmooth"] = mc["hypersmooth"] if mc else list(_HYPERSMOOTH_LABELS.values())

        log.info(
            "capabilities for %s: res=%s fps=%s lens=%s hs=%s",
            self._target,
            result.get("resolution"),
            result.get("fps"),
            result.get("lens"),
            result.get("hypersmooth"),
        )
        return result

    async def set_video_settings(
        self,
        resolution: str | None,
        fps: str | None,
        lens: str | None = None,
        hypersmooth: str | None = None,
    ) -> None:
        """Apply video resolution, frame-rate, lens, and/or stabilization.

        BLE: ble_setting.<x>.set(...). COHN: http_setting.<x>.set(...).

        Raises ValueError for unknown keys, RuntimeError if the camera rejects
        the combination (e.g. fps not valid for that resolution).
        """
        self._require_open()
        # All video setting enums live in open_gopro.models.constants.settings
        from open_gopro.models.constants.settings import (  # type: ignore[import]
            FramesPerSecond,
            Hypersmooth,
            VideoLens,
            VideoResolution,
        )

        is_cohn = not self._use_ble   # True only in COHN-only mode
        timeout = (
            self._timing.http_cmd_timeout_sec
            if is_cohn
            else self._timing.ble_cmd_timeout_sec
        )
        setting_root = self._gopro.http_setting if is_cohn else self._gopro.ble_setting

        if resolution is not None:
            res_key = _RESOLUTION_MAP.get(resolution)
            if res_key is None:
                raise ValueError(f"Unknown resolution '{resolution}'. Valid: {list(_RESOLUTION_MAP)}")
            res_enum = getattr(VideoResolution, res_key, None)
            if res_enum is None:
                raise ValueError(f"Resolution '{resolution}' not available in this SDK version")
            resp = await asyncio.wait_for(
                setting_root.video_resolution.set(res_enum),
                timeout=timeout,
            )
            _check_resp(resp)
            log.info("set resolution=%s on target=%s", resolution, self._target)

        if fps is not None:
            fps_key = _FPS_MAP.get(fps)
            if fps_key is None:
                raise ValueError(f"Unknown fps '{fps}'. Valid: {list(_FPS_MAP)}")
            fps_enum = getattr(FramesPerSecond, fps_key, None)
            if fps_enum is None:
                raise ValueError(f"FPS '{fps}' not available in this SDK version")
            resp = await asyncio.wait_for(
                setting_root.frames_per_second.set(fps_enum),
                timeout=timeout,
            )
            _check_resp(resp)
            log.info("set fps=%s on target=%s", fps, self._target)

        if lens is not None:
            enum_name = _LENS_REVERSE.get(lens)
            if enum_name is None:
                raise ValueError(f"Unknown lens '{lens}'. Valid: {list(_LENS_REVERSE)}")
            lens_enum = getattr(VideoLens, enum_name, None)
            if lens_enum is None:
                raise ValueError(f"Lens '{lens}' not available in this SDK version")
            resp = await asyncio.wait_for(
                setting_root.video_lens.set(lens_enum),
                timeout=timeout,
            )
            _check_resp(resp)
            log.info("set lens=%s on target=%s", lens, self._target)

        if hypersmooth is not None:
            enum_name = _HYPERSMOOTH_REVERSE.get(hypersmooth)
            if enum_name is None:
                raise ValueError(f"Unknown hypersmooth '{hypersmooth}'. Valid: {list(_HYPERSMOOTH_REVERSE)}")
            hs_enum = getattr(Hypersmooth, enum_name, None)
            if hs_enum is None:
                raise ValueError(f"Hypersmooth '{hypersmooth}' not available in this SDK version")
            resp = await asyncio.wait_for(
                setting_root.hypersmooth.set(hs_enum),
                timeout=timeout,
            )
            _check_resp(resp)
            log.info("set hypersmooth=%s on target=%s", hypersmooth, self._target)

    async def set_preset_group(self, mode: str) -> None:
        """Switch the camera's active preset group: 'video', 'photo', or 'timelapse'."""
        self._require_open()

        if not self._use_ble:
            # http_command.load_preset_group accepts a raw int group id.
            _MODE_MAP_INT = {"video": 1000, "photo": 1001, "timelapse": 1002}
            group_int = _MODE_MAP_INT.get(mode)
            if group_int is None:
                raise ValueError(f"Unknown mode '{mode}'. Valid: {list(_MODE_MAP_INT)}")
            resp = await asyncio.wait_for(
                self._gopro.http_command.load_preset_group(group=group_int),
                timeout=self._timing.http_cmd_timeout_sec,
            )
            _check_resp(resp)
            log.info("set preset group=%s on target=%s (cohn)", mode, self._target)
            return

        from open_gopro.models import proto  # type: ignore[import]

        _MODE_MAP = {
            "video":     proto.EnumPresetGroup.PRESET_GROUP_ID_VIDEO,
            "photo":     proto.EnumPresetGroup.PRESET_GROUP_ID_PHOTO,
            "timelapse": proto.EnumPresetGroup.PRESET_GROUP_ID_TIMELAPSE,
        }
        group_id = _MODE_MAP.get(mode)
        if group_id is None:
            raise ValueError(f"Unknown mode '{mode}'. Valid: {list(_MODE_MAP)}")
        resp = await asyncio.wait_for(
            self._gopro.ble_command.load_preset_group(group=group_id),
            timeout=self._timing.ble_cmd_timeout_sec,
        )
        _check_resp(resp)
        log.info("set preset group=%s on target=%s", mode, self._target)

    # ── Clock sync ───────────────────────────────────────────────────────

    async def sync_time(self) -> None:
        """Set the camera's clock to the server's current local time.

        Works in all modes (BLE and COHN). Uses the server's local timezone
        so the camera's timestamps match the recording machine's clock.
        """
        self._require_open()
        now = datetime.now()
        tz_min, is_dst = _local_tz_offset_minutes()

        if self._use_cohn_http:
            # Prefer HTTP time sync when COHN is available (cohn or ble+cohn).
            resp = await asyncio.wait_for(
                self._gopro.http_command.set_date_time(
                    date_time=now, tz_offset=tz_min, is_dst=is_dst,
                ),
                timeout=self._timing.http_cmd_timeout_sec,
            )
            _check_resp(resp)
            log.info("synced time on %s (%s/http) tz=%+d min dst=%s", self._target, self._mode, tz_min, is_dst)
        else:
            resp = await asyncio.wait_for(
                self._gopro.ble_command.set_date_time(
                    date_time=now, tz_offset=tz_min, is_dst=is_dst,
                ),
                timeout=self._timing.ble_cmd_timeout_sec,
            )
            _check_resp(resp)
            log.info("synced time on %s (ble) tz=%+d min dst=%s", self._target, tz_min, is_dst)

    # ── COHN-specific operations ─────────────────────────────────────────

    async def provision_cohn(self, ssid: str, password: str) -> dict[str, Any]:
        """Run BLE provisioning. Stores credentials in cohn_db.json.

        This must NOT use the long-lived COHN session — it opens a fresh
        BLE-only session, runs provisioning, then closes. The provisioning
        flow is:
          1. access_point.connect(ssid, password) — camera joins home Wi-Fi
          2. cohn.configure(force_reprovision=True) — provision cert + creds

        Returns a small dict describing the resulting credentials (ip, etc.).
        """
        from open_gopro import WirelessGoPro
        from returns.pipeline import is_successful  # type: ignore[import]
        Iface = WirelessGoPro.Interface

        async with WirelessGoPro(
            target=self._target,
            interfaces={Iface.BLE},
            wifi_adapter=_NullWifiController,
            cohn_db=COHN_DB_PATH,
        ) as gp:
            # Note: do NOT install _skip_cohn_wait — provisioning needs the real
            # wait_until_ready() so cohn._supported flips to True.
            result = await gp.access_point.connect(ssid, password)
            if not is_successful(result):
                raise RuntimeError(f"camera failed to join '{ssid}': {result.failure()}")
            result = await gp.cohn.configure(force_reprovision=True)
            if not is_successful(result):
                raise RuntimeError(f"COHN provisioning failed: {result.failure()}")
            creds = gp.cohn.credentials
        return {
            "ip_address": getattr(creds, "ip_address", None),
            "username": getattr(creds, "username", None),
            "has_certificate": bool(getattr(creds, "certificate", None)),
        }

    async def start_webcam_rtsp(self, *, resolution: str = "720", fov: str = "WIDE") -> str:
        """Start RTSP webcam stream. Returns rtsp://{ip}:554/live."""
        self._require_open()
        if not self._use_cohn_http:
            raise RuntimeError("webcam preview is only supported in COHN or BLE+COHN mode")
        from open_gopro.models.constants import Toggle
        from open_gopro.models.streaming import (
            WebcamFOV,
            WebcamProtocol,
            WebcamResolution,
        )

        # Defensive: ensure camera is not recording before flipping into webcam.
        try:
            await asyncio.wait_for(
                self._gopro.http_command.set_shutter(shutter=Toggle.DISABLE),
                timeout=self._timing.http_cmd_timeout_sec,
            )
        except Exception:
            pass

        res_enum = {
            "480":  WebcamResolution.RES_480,
            "720":  WebcamResolution.RES_720,
            "1080": WebcamResolution.RES_1080,
        }.get(resolution, WebcamResolution.RES_720)
        fov_enum = {
            "WIDE":      WebcamFOV.WIDE,
            "NARROW":    WebcamFOV.NARROW,
            "SUPERVIEW": WebcamFOV.SUPERVIEW,
            "LINEAR":    WebcamFOV.LINEAR,
        }.get(fov, WebcamFOV.WIDE)
        resp = await asyncio.wait_for(
            self._gopro.http_command.webcam_start(
                protocol=WebcamProtocol.RTSP,
                resolution=res_enum,
                fov=fov_enum,
            ),
            timeout=self._timing.http_cmd_timeout_sec,
        )
        _check_resp(resp)
        ip = self._gopro.ip_address
        return f"rtsp://{ip}:554/live"

    async def stop_webcam(self) -> None:
        """Stop RTSP webcam stream and exit webcam mode."""
        self._require_open()
        if not self._use_cohn_http:
            return
        try:
            await asyncio.wait_for(
                self._gopro.http_command.webcam_stop(), timeout=self._timing.http_cmd_timeout_sec,
            )
        except Exception as exc:
            log.warning("webcam_stop failed for %s: %s", self._target, exc)
        try:
            await asyncio.wait_for(
                self._gopro.http_command.webcam_exit(), timeout=self._timing.http_cmd_timeout_sec,
            )
        except Exception:
            pass

    def _require_open(self) -> None:
        if self._gopro is None:
            raise RuntimeError("driver not open")


def _local_tz_offset_minutes() -> tuple[int, bool]:
    """Return (tz_offset_minutes_east_of_UTC, is_dst) for the server's local timezone."""
    is_dst = bool(_time.daylight and _time.localtime().tm_isdst > 0)
    # time.timezone / time.altzone are seconds WEST of UTC — negate to get east.
    offset_sec = -(_time.altzone if is_dst else _time.timezone)
    return offset_sec // 60, is_dst


def _check_resp(resp: Any) -> None:
    ok = getattr(resp, "ok", True)
    if ok is False:
        # Extract a human-readable setting/command name from the response ID
        setting_id = getattr(resp, "id", None)
        if setting_id is not None:
            # e.g. SettingId.VIDEO_RESOLUTION → "Video Resolution"
            raw = str(setting_id).split(".")[-1]
            name = raw.replace("_", " ").title()
            raise RuntimeError(f"Camera rejected {name} — value not supported in current mode")
        raise RuntimeError("Camera rejected command — value not supported in current mode")


def _unwrap(resp: Any) -> Any:
    return getattr(resp, "data", resp)


def default_driver_factory(
    config: CameraConfig,
    timing: TimingConfig | None = None,
) -> CameraDriver:
    return WirelessGoProDriver(config.target, mode=config.mode, timing=timing)


class _NullWifiController:
    """No-op WifiController for BLE-only mode.

    open_gopro 0.22's WirelessGoPro.__init__ constructs a WifiController even
    when WIFI_AP is excluded from `interfaces`. The default Windows backend
    (NetshWireless) issues `netsh wlan disconnect` in __del__, which drops
    every host Wi-Fi adapter regardless of which one was used. This stub
    satisfies the abstract interface but never touches the OS.
    """

    def __init__(self, interface: str | None = None, password: str | None = None) -> None:
        self._interface = interface or ""
        self._password = password
        self.ssid: str | None = None

    async def open(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def close(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:  # noqa: ARG002
        return False

    async def disconnect(self) -> bool:
        return True

    def current(self) -> tuple[str | None, Any]:
        return (None, None)

    def available_interfaces(self) -> list[str]:
        return []

    def power(self, power: bool) -> bool:  # noqa: ARG002
        return True

    @property
    def is_on(self) -> bool:
        return False

    @property
    def interface(self) -> str:
        return self._interface

    @property
    def is_connected(self) -> bool:
        return False
