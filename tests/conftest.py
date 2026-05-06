from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gopro_mgmt.driver import CameraDriver
from gopro_mgmt.manager import CameraManager
from gopro_mgmt.schemas import AppConfig, CameraConfig, ServerConfig


class FakeDriver:
    """In-memory stand-in for WirelessGoPro used across the test suite."""

    instances: list["FakeDriver"] = []

    def __init__(self, target: str, *, mode: str = "ble", enable_wifi: bool = False) -> None:
        # Accept BOTH old (enable_wifi=) and new (mode=) ctor signatures so
        # we don't have to fix every call site at once. New driver factory
        # passes mode=; legacy tests pass enable_wifi=.
        self.target = target
        if mode != "ble":
            self.mode = mode
        elif enable_wifi:
            self.mode = "ble+wifi"
        else:
            self.mode = "ble"
        self.enable_wifi = (self.mode == "ble+wifi")
        self.is_open = False
        self.encoding = False
        self.battery = 87
        self.sd_remaining = 3600
        self.fail_open: Exception | None = None
        self.fail_start: Exception | None = None
        self.fail_provision: Exception | None = None
        self.last_provision_args: tuple[str, str] | None = None
        self.start_count = 0
        self.stop_count = 0
        FakeDriver.instances.append(self)

    async def open(self) -> None:
        if self.fail_open:
            raise self.fail_open
        await asyncio.sleep(0)
        self.is_open = True

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.is_open = False

    def get_model(self) -> str | None:
        return None

    async def get_current_video_settings(self) -> dict[str, Any]:
        return {}

    async def get_video_capabilities(self) -> dict[str, Any]:
        return {}

    async def set_video_settings(
        self,
        resolution: str | None,
        fps: str | None,
        lens: str | None = None,
        hypersmooth: str | None = None,
    ) -> None:
        pass

    async def set_preset_group(self, mode: str) -> None:
        pass

    async def start_recording(self) -> None:
        if self.fail_start:
            raise self.fail_start
        if not self.is_open:
            raise RuntimeError("not open")
        self.encoding = True
        self.start_count += 1

    async def stop_recording(self) -> None:
        if not self.is_open:
            raise RuntimeError("not open")
        self.encoding = False
        self.stop_count += 1

    async def get_status(self) -> dict[str, Any]:
        return {
            "encoding": self.encoding,
            "battery_percent": self.battery,
            "sd_remaining_sec": self.sd_remaining,
        }

    async def get_rssi(self) -> int | None:
        return None

    async def sync_time(self) -> None:
        return None

    async def provision_cohn(self, ssid: str, password: str) -> dict:
        if self.fail_provision:
            raise self.fail_provision
        self.last_provision_args = (ssid, password)
        return {"ip_address": "192.168.1.42", "username": "gopro", "has_certificate": True}

    async def start_webcam_rtsp(self, *, resolution: str = "720", fov: str = "WIDE") -> str:
        if not self.is_open:
            raise RuntimeError("not open")
        return "rtsp://192.168.1.42:554/live"

    async def stop_webcam(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_fake_driver():
    FakeDriver.instances.clear()
    yield
    FakeDriver.instances.clear()


@pytest.fixture
def cameras() -> list[CameraConfig]:
    return [
        CameraConfig(id="cam-a", name="Cam A", target="1111"),
        CameraConfig(id="cam-b", name="Cam B", target="2222"),
    ]


@pytest.fixture
def driver_factory():
    def factory(cfg: CameraConfig) -> CameraDriver:
        return FakeDriver(cfg.target, mode=cfg.mode)
    return factory


@pytest.fixture
def manager(cameras, driver_factory) -> CameraManager:
    return CameraManager(cameras, driver_factory=driver_factory)


@pytest.fixture
def app_config(cameras) -> AppConfig:
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        poll_interval_sec=0.1,
        cameras=cameras,
    )


@pytest.fixture
def fake_scan():
    from gopro_mgmt.schemas import ScanResult

    async def _scan(_timeout: float):
        return [
            ScanResult(name="GoPro AB12", target="AB12", rssi=-42, address="aa:bb:cc:01"),
            ScanResult(name="GoPro CD34", target="CD34", rssi=-71, address="aa:bb:cc:02"),
        ]

    return _scan
