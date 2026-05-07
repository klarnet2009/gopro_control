from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ConnectionState = Literal["disconnected", "connecting", "connected", "error"]
ConnectionMode = Literal["ble", "ble+wifi", "cohn", "ble+cohn"]

ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,30}$"
TARGET_PATTERN = r"^[A-Za-z0-9]{4}$"


class CameraConfig(BaseModel):
    id: str = Field(pattern=ID_PATTERN, description="Stable id: lowercase letters, digits, dashes (max 31).")
    name: str = Field(min_length=1, max_length=60)
    target: str = Field(
        pattern=TARGET_PATTERN,
        description="Last 4 alphanumeric chars of the camera's serial number (Open GoPro target identifier).",
    )
    mode: ConnectionMode = Field(
        default="ble",
        description="Transport: 'ble' (faster, sufficient for start/stop), 'ble+wifi' (also enables HTTP, media, streaming), 'cohn' (HTTP over home Wi-Fi, requires one-time provisioning, enables live preview), or 'ble+cohn' (dual: BLE for control commands + COHN for preview and clock sync, requires provisioning).",
    )


class CameraCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    target: str = Field(pattern=TARGET_PATTERN)
    mode: ConnectionMode = "ble"


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    target: str | None = Field(default=None, pattern=TARGET_PATTERN)
    mode: ConnectionMode | None = None


class ScanResult(BaseModel):
    name: str
    target: str
    rssi: int
    address: str | None = None


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class TimingConfig(BaseModel):
    """Tunable timing knobs.

    Defaults match the legacy hard-coded constants. Override in config.yaml
    under ``timing:`` to adapt to laggy networks or aggressive cameras.
    """
    # Manager: suppress spurious encoding=True from camera while file is being
    # finalised after a stop ACK.
    stop_grace_sec: float = 8.0
    # Manager: minimum gap between a stop ACK and the next start command.
    post_stop_recovery_sec: float = 4.0
    # Manager: how often refresh_status() re-reads BLE RSSI for connected cams.
    rssi_poll_interval_sec: float = 15.0
    # Driver: hard ceiling on any single BLE round-trip; protects against SDK
    # response-queue deadlocks.
    ble_cmd_timeout_sec: float = 8.0
    # Driver: short timeout for incidental BLE detail reads (battery, fps).
    ble_detail_timeout_sec: float = 1.5
    # Driver: HTTP command timeout over COHN.
    http_cmd_timeout_sec: float = 12.0
    # Driver: HTTP shutter (start/stop) timeout over COHN — generous because the
    # camera takes time to spin up the recorder.
    http_shutter_timeout_sec: float = 15.0
    # Driver: COHN keep-alive cadence (camera idle-sleep is ~30 s).
    cohn_keepalive_sec: float = 25.0


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    poll_interval_sec: float = 2.0
    cameras: list[CameraConfig] = Field(default_factory=list)
    atem_host: str | None = Field(
        default=None,
        description="ATEM IP address. If null, auto-discover via mDNS (_blackmagic._tcp.local.).",
    )
    timing: TimingConfig = Field(default_factory=TimingConfig)


class CameraStatus(BaseModel):
    id: str
    name: str
    target: str
    mode: ConnectionMode = "ble"
    connection: ConnectionState = "disconnected"
    encoding: bool | None = None
    battery_percent: int | None = None
    sd_remaining_sec: int | None = None
    rssi_dbm: int | None = None
    last_error: str | None = None
    # Active preset group reported by the camera (1000=video, 1001=photo, 1002=timelapse).
    # None when not connected or camera doesn't expose this status.
    preset_group: int | None = None
    # Human-readable model name read from BLE GATT on connect (e.g. "HERO12 Black").
    # None until the camera has been connected at least once.
    model: str | None = None
    # Current video settings — populated on connect, updated after apply_settings().
    resolution: str | None = None   # e.g. "4k", "1080"
    fps: str | None = None          # e.g. "60", "30"
    lens: str | None = None         # e.g. "Wide", "Linear", "HyperView"
    hypersmooth: str | None = None  # e.g. "Standard", "Boost", "Off"
    # COHN provisioning state: True once cohn_db.json has credentials for this camera's target.
    cohn_provisioned: bool = False
    # Last-known camera IP from cohn_db.json (informational only).
    cohn_ip: str | None = None
    # BLE telemetry observer health: how many of the seven push-notification
    # observers (battery, sd_remaining, preset_group, resolution, fps, lens,
    # hypersmooth) are currently delivering updates. ``None`` for COHN cameras
    # (no observers) and for cameras that pre-date driver health reporting.
    observers_alive: int | None = None
    observers_total: int | None = None


class CameraSettingsPayload(BaseModel):
    """Body for POST /api/cameras/{id}/settings — at least one field required."""
    resolution: str | None = Field(default=None, description="e.g. '4k', '1080', '720'")
    fps: str | None = Field(default=None, description="e.g. '60', '30', '24'")
    lens: str | None = Field(default=None, description="e.g. 'Wide', 'Linear', 'HyperView'")
    hypersmooth: str | None = Field(default=None, description="e.g. 'Off', 'Standard', 'Boost'")


class ModePayload(BaseModel):
    """Body for POST /api/cameras/{id}/mode — switch preset group."""
    mode: Literal["video", "photo", "timelapse"]


class CohnProvisionPayload(BaseModel):
    """Body for POST /api/cameras/{id}/provision-cohn."""
    ssid: str = Field(min_length=1, max_length=32, description="Home Wi-Fi SSID the camera should join.")
    password: str = Field(min_length=8, max_length=63, description="WPA2/3 PSK for that SSID.")


class CommandResult(BaseModel):
    ok: bool
    data: Any | None = None
    error: dict[str, str] | None = None

    @classmethod
    def success(cls, data: Any | None = None) -> CommandResult:
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, code: str, message: str) -> CommandResult:
        return cls(ok=False, error={"code": code, "message": message})


class WSEvent(BaseModel):
    type: Literal["status", "command", "hello", "camera_added", "camera_removed", "camera_updated", "scan_result", "cohn_provisioned", "atem_status", "atem_event"]
    payload: Any
