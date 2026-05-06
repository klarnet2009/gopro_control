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

from .schemas import CameraConfig

log = logging.getLogger(__name__)

# Hard ceiling for any single BLE roundtrip. open_gopro 0.22 occasionally dead-
# locks its sync response queue when async push notifications interleave; a wall
# clock cap keeps a stuck command from hanging the whole server.
BLE_CMD_TIMEOUT = 8.0

# COHN (HTTP over home Wi-Fi) timeouts — generous because HTTPS over LAN
# adds round-trip overhead vs. local BLE.
HTTP_CMD_TIMEOUT     = 12.0
HTTP_SHUTTER_TIMEOUT = 15.0
COHN_KEEPALIVE_SEC   = 25.0   # camera idle-sleep is ~30s; pad a little

# Resolve cohn_db.json relative to project root, NOT cwd. From this file
# (src/gopro_mgmt/driver.py), parents[2] is the project root. open_gopro
# uses TinyDB to read/write per-camera credentials there.
COHN_DB_PATH = Path(__file__).resolve().parents[2] / "cohn_db.json"

# ── Settings maps ─────────────────────────────────────────────────────────────
# Maps the friendly key sent by the UI to the open_gopro enum attribute name.
# Add entries here when new GoPro models expose additional values.
_RESOLUTION_MAP: dict[str, str] = {
    # ── 5.3K (Hero 11 / Hero 12) ───────────────────────────────────────
    "5.3K":         "NUM_5_3K",
    "5.3K 4:3":     "NUM_5_3K_4_3",
    "5.3K 8:7":     "NUM_5_3K_8_7",
    # ── 4K ─────────────────────────────────────────────────────────────
    "4K":           "NUM_4K",
    "4K 4:3":       "NUM_4K_4_3",
    "4K 8:7":       "NUM_4K_8_7",
    # ── 2.7K ───────────────────────────────────────────────────────────
    "2.7K":         "NUM_2_7K",
    "2.7K 4:3":     "NUM_2_7K_4_3",
    # ── 1080p / 1440p / 720p ───────────────────────────────────────────
    "1440p":        "NUM_1440",
    "1080p":        "NUM_1080",
    "720p":         "NUM_720",
}
_FPS_MAP: dict[str, str] = {
    "240": "NUM_240_0",
    "120": "NUM_120_0",
    "100": "NUM_100_0",
    "60":  "NUM_60_0",
    "50":  "NUM_50_0",
    "30":  "NUM_30_0",
    "25":  "NUM_25_0",
    "24":  "NUM_24_0",
}

# Reverse maps: enum name → our friendly key (for decoding camera responses)
_RESOLUTION_REVERSE: dict[str, str] = {v: k for k, v in _RESOLUTION_MAP.items()}
_FPS_REVERSE: dict[str, str] = {v: k for k, v in _FPS_MAP.items()}

# Lens / stabilization display labels
_LENS_LABELS: dict[str, str] = {
    "WIDE":                    "Wide",
    "NARROW":                  "Narrow",
    "SUPERVIEW":               "SuperView",
    "LINEAR":                  "Linear",
    "MAX_SUPERVIEW":           "Max SuperView",
    "LINEAR_HORIZON_LEVELING": "Linear+Level",
    "HYPERVIEW":               "HyperView",
    "LINEAR_HORIZON_LOCK":     "Linear+Lock",
    "MAX_HYPERVIEW":           "Max HyperView",
    "ULTRA_SUPERVIEW":         "Ultra SuperView",
    "ULTRA_WIDE":              "Ultra Wide",
    "ULTRA_LINEAR":            "Ultra Linear",
    "ULTRA_HYPERVIEW":         "Ultra HyperView",
}
_HYPERSMOOTH_LABELS: dict[str, str] = {
    "OFF":        "Off",
    "LOW":        "Low",
    "STANDARD":   "Standard",
    "HIGH":       "High",
    "BOOST":      "Boost",
    "AUTO_BOOST": "AutoBoost",
}

# Build reverse maps after the label dicts are defined
_LENS_REVERSE = {v: k for k, v in _LENS_LABELS.items()}
_HYPERSMOOTH_REVERSE = {v: k for k, v in _HYPERSMOOTH_LABELS.items()}

# ── Per-model capability tables ───────────────────────────────────────────────
# Used as a fallback when the BLE get_capabilities() query fails or returns
# an empty list. Keys are lowercase substrings matched against the model name
# returned by get_hardware_info() (e.g. "HERO12 Black" → matches "hero12").
# Sources: GoPro official spec sheets + Open GoPro BLE spec tables.
_MODEL_CAPS: dict[str, dict[str, list[str]]] = {
    "hero12": {
        "resolutions": ["5.3K", "5.3K 4:3", "4K", "4K 4:3", "4K 8:7", "2.7K 4:3", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level", "HyperView", "Linear+Lock"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost", "AutoBoost"],
    },
    "hero11": {
        "resolutions": ["5.3K", "5.3K 4:3", "5.3K 8:7", "4K", "4K 4:3", "4K 8:7", "2.7K 4:3", "1440p", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level", "HyperView", "Linear+Lock"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost", "AutoBoost"],
    },
    "hero10": {
        "resolutions": ["5.3K", "5.3K 4:3", "4K", "4K 4:3", "2.7K 4:3", "1440p", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost"],
    },
    "hero9": {
        "resolutions": ["5K", "5K 4:3", "4K", "4K 4:3", "2.7K", "2.7K 4:3", "1440p", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost"],
    },
}

def _model_caps_for(model: str | None) -> dict[str, list[str]] | None:
    """Return the capability table for a known model, or None if not recognised."""
    if not model:
        return None
    lower = model.lower()
    for key, caps in _MODEL_CAPS.items():
        if key in lower:
            return caps
    return None


def _enum_to_resolution(val: Any) -> str | None:
    """Convert a VideoResolution enum value to our friendly key, or None if unknown."""
    name = val.name if hasattr(val, "name") else str(val)
    return _RESOLUTION_REVERSE.get(name)


def _enum_to_fps(val: Any) -> str | None:
    """Convert a VideoFPS enum value to our friendly key, or None if unknown."""
    name = val.name if hasattr(val, "name") else str(val)
    return _FPS_REVERSE.get(name)


def _enum_to_lens(val: Any) -> str | None:
    """Convert a VideoLens enum to a human-readable label."""
    name = val.name if hasattr(val, "name") else str(val)
    return _LENS_LABELS.get(name, name.replace("_", " ").title() if name else None)


def _enum_to_hypersmooth(val: Any) -> str | None:
    """Convert a Hypersmooth enum to a human-readable label."""
    name = val.name if hasattr(val, "name") else str(val)
    return _HYPERSMOOTH_LABELS.get(name, name.replace("_", " ").title() if name else None)


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
    def __init__(self, target: str, *, mode: str = "ble") -> None:
        self._target = target
        self._mode = mode                  # "ble" | "ble+wifi" | "cohn"
        self._gopro: Any | None = None
        self._model: str | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

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

        # Workaround for open_gopro 0.22: WirelessGoPro.open() unconditionally
        # awaits self.cohn.wait_until_ready() with a 30s timeout, even when COHN
        # is not in `interfaces`. Cameras that don't support COHN respond
        # INVALID_PARAM and `_ready_event` is never set, so open() hangs
        # ~30s × 5 retries before giving up. Skip on BLE / BLE+WiFi only.
        async def _skip_cohn_wait() -> None:
            return

        self._gopro.cohn.wait_until_ready = _skip_cohn_wait

        await self._gopro.open()
        self._model = await self._read_model()
        log.info("opened camera target=%s mode=%s model=%s", self._target, self._mode, self._model)

    async def close(self) -> None:
        # Cancel keep-alive first so it doesn't try to use a closing session.
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
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
                    await asyncio.sleep(COHN_KEEPALIVE_SEC)
                    if self._gopro is None:
                        return
                    try:
                        # Polling get_camera_state resets the camera's idle timer
                        # — equivalent to a dedicated keep-alive on every firmware.
                        await asyncio.wait_for(
                            self._gopro.http_command.get_camera_state(),
                            timeout=HTTP_CMD_TIMEOUT,
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
                self._gopro.http_command.get_camera_info(), timeout=HTTP_CMD_TIMEOUT,
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
                timeout=BLE_CMD_TIMEOUT,
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

        if self._mode == "cohn":
            # ── HTTP path: ensure Video preset group via http_command ─────
            try:
                state_resp = await asyncio.wait_for(
                    self._gopro.http_command.get_camera_state(),
                    timeout=HTTP_CMD_TIMEOUT,
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
                        timeout=HTTP_CMD_TIMEOUT,
                    )
                    _check_resp(sw_resp)
            except Exception as exc:
                log.warning(
                    "could not ensure video mode for %s (cohn): %s — attempting record anyway",
                    self._target, exc,
                )
            resp = await asyncio.wait_for(
                self._gopro.http_command.set_shutter(shutter=Toggle.ENABLE),
                timeout=HTTP_SHUTTER_TIMEOUT,
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
                timeout=BLE_CMD_TIMEOUT,
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
                    timeout=BLE_CMD_TIMEOUT,
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
            timeout=BLE_CMD_TIMEOUT,
        )
        _check_resp(resp)

    async def stop_recording(self) -> None:
        self._require_open()
        from open_gopro.models.constants import Toggle

        if self._mode == "cohn":
            resp = await asyncio.wait_for(
                self._gopro.http_command.set_shutter(shutter=Toggle.DISABLE),
                timeout=HTTP_CMD_TIMEOUT,
            )
            _check_resp(resp)
            return

        resp = await asyncio.wait_for(
            self._gopro.ble_command.set_shutter(shutter=Toggle.DISABLE),
            timeout=BLE_CMD_TIMEOUT,
        )
        _check_resp(resp)

    async def get_status(self) -> dict[str, Any]:
        self._require_open()
        gp = self._gopro

        if self._mode == "cohn":
            # Single HTTP call returns all statuses at once.
            state_resp = await asyncio.wait_for(
                gp.http_command.get_camera_state(), timeout=HTTP_CMD_TIMEOUT,
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

        encoding    = _unwrap(await asyncio.wait_for(gp.ble_status.encoding.get_value(), BLE_CMD_TIMEOUT))
        battery     = _unwrap(await asyncio.wait_for(gp.ble_status.internal_battery_percentage.get_value(), BLE_CMD_TIMEOUT))
        sd_remaining = _unwrap(await asyncio.wait_for(gp.ble_status.remaining_video_time.get_value(), BLE_CMD_TIMEOUT))

        # Preset group: 1000=video, 1001=photo, 1002=timelapse
        preset_group: int | None = None
        try:
            pg = _unwrap(await asyncio.wait_for(gp.ble_status.preset_group.get_value(), BLE_CMD_TIMEOUT))
            preset_group = int(pg) if pg is not None else None
        except Exception:
            pass  # older firmware may not expose this status

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
        if self._gopro is None or self._mode == "cohn":
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

        if self._mode == "cohn":
            try:
                from open_gopro.models.constants.settings import (
                    FramesPerSecond, Hypersmooth, VideoLens, VideoResolution,
                )
            except Exception:
                return result
            try:
                state_resp = await asyncio.wait_for(
                    gp.http_command.get_camera_state(), timeout=HTTP_CMD_TIMEOUT,
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
                    timeout=BLE_CMD_TIMEOUT,
                ))
                if val is not None:
                    converted = converter(val)
                    if converted is not None:
                        result[key] = converted
            except Exception:
                log.debug("could not read %s for target=%s", ble_attr, self._target)

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

        if self._mode == "cohn":
            # ── COHN: read current values from camera state, fall back to
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
        try:
            val = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_resolution.get_value(), BLE_CMD_TIMEOUT,
            ))
            if val is not None:
                result["resolution"] = _enum_to_resolution(val)
        except Exception:
            log.debug("could not read current resolution for %s", self._target)

        try:
            val = _unwrap(await asyncio.wait_for(
                gp.ble_setting.frames_per_second.get_value(), BLE_CMD_TIMEOUT,
            ))
            if val is not None:
                result["fps"] = _enum_to_fps(val)
        except Exception:
            log.debug("could not read current fps for %s", self._target)

        # ── capability queries (Open GoPro BLE spec 0x13) ─────────────────
        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_resolution.get_capabilities(), BLE_CMD_TIMEOUT,
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
                gp.ble_setting.frames_per_second.get_capabilities(), BLE_CMD_TIMEOUT,
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
                gp.ble_setting.video_lens.get_value(), BLE_CMD_TIMEOUT,
            ))
            if val is not None:
                result["lens"] = _enum_to_lens(val)
        except Exception:
            log.debug("could not read current lens for %s", self._target)

        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.video_lens.get_capabilities(), BLE_CMD_TIMEOUT,
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
                gp.ble_setting.hypersmooth.get_value(), BLE_CMD_TIMEOUT,
            ))
            if val is not None:
                result["hypersmooth"] = _enum_to_hypersmooth(val)
        except Exception:
            log.debug("could not read current hypersmooth for %s", self._target)

        try:
            caps = _unwrap(await asyncio.wait_for(
                gp.ble_setting.hypersmooth.get_capabilities(), BLE_CMD_TIMEOUT,
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

        is_cohn = self._mode == "cohn"
        timeout = HTTP_CMD_TIMEOUT if is_cohn else BLE_CMD_TIMEOUT
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

        if self._mode == "cohn":
            # http_command.load_preset_group accepts a raw int group id.
            _MODE_MAP_INT = {"video": 1000, "photo": 1001, "timelapse": 1002}
            group_int = _MODE_MAP_INT.get(mode)
            if group_int is None:
                raise ValueError(f"Unknown mode '{mode}'. Valid: {list(_MODE_MAP_INT)}")
            resp = await asyncio.wait_for(
                self._gopro.http_command.load_preset_group(group=group_int),
                timeout=HTTP_CMD_TIMEOUT,
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
            timeout=BLE_CMD_TIMEOUT,
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

        if self._mode == "cohn":
            resp = await asyncio.wait_for(
                self._gopro.http_command.set_date_time(
                    date_time=now, tz_offset=tz_min, is_dst=is_dst,
                ),
                timeout=HTTP_CMD_TIMEOUT,
            )
            _check_resp(resp)
            log.info("synced time on %s (cohn) tz=%+d min dst=%s", self._target, tz_min, is_dst)
        else:
            resp = await asyncio.wait_for(
                self._gopro.ble_command.set_date_time(
                    date_time=now, tz_offset=tz_min, is_dst=is_dst,
                ),
                timeout=BLE_CMD_TIMEOUT,
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
        if self._mode != "cohn":
            raise RuntimeError("webcam preview is only supported in COHN mode")
        from open_gopro.models.streaming import (
            WebcamProtocol,
            WebcamResolution,
            WebcamFOV,
        )
        from open_gopro.models.constants import Toggle

        # Defensive: ensure camera is not recording before flipping into webcam.
        try:
            await asyncio.wait_for(
                self._gopro.http_command.set_shutter(shutter=Toggle.DISABLE),
                timeout=HTTP_CMD_TIMEOUT,
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
            timeout=HTTP_CMD_TIMEOUT,
        )
        _check_resp(resp)
        ip = self._gopro.ip_address
        return f"rtsp://{ip}:554/live"

    async def stop_webcam(self) -> None:
        """Stop RTSP webcam stream and exit webcam mode."""
        self._require_open()
        if self._mode != "cohn":
            return
        try:
            await asyncio.wait_for(
                self._gopro.http_command.webcam_stop(), timeout=HTTP_CMD_TIMEOUT,
            )
        except Exception as exc:
            log.warning("webcam_stop failed for %s: %s", self._target, exc)
        try:
            await asyncio.wait_for(
                self._gopro.http_command.webcam_exit(), timeout=HTTP_CMD_TIMEOUT,
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


def default_driver_factory(config: CameraConfig) -> CameraDriver:
    return WirelessGoProDriver(config.target, mode=config.mode)


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
