from __future__ import annotations

import asyncio
import time

import pytest

from gopro_mgmt.manager import CameraAlreadyExists, CameraManager, CameraNotFound, POST_STOP_RECOVERY_SEC
from gopro_mgmt.schemas import CameraConfig
from tests.conftest import FakeDriver


async def test_initial_state_disconnected(manager: CameraManager):
    statuses = manager.list_status()
    assert {s.id for s in statuses} == {"cam-a", "cam-b"}
    assert all(s.connection == "disconnected" for s in statuses)


async def test_connect_marks_connected_and_records_driver(manager: CameraManager):
    await manager.connect("cam-a")
    s = manager.get_status("cam-a")
    assert s.connection == "connected"
    assert s.last_error is None
    assert FakeDriver.instances[0].is_open is True


async def test_connect_failure_records_error():
    from gopro_mgmt.schemas import CameraConfig

    def factory(cfg: CameraConfig):
        d = FakeDriver(cfg.target)
        d.fail_open = RuntimeError("BLE scan timeout")
        return d

    mgr = CameraManager(
        [CameraConfig(id="cam-a", name="Cam A", target="1111")],
        driver_factory=factory,
    )

    with pytest.raises(RuntimeError, match="BLE scan timeout"):
        await mgr.connect("cam-a")
    s = mgr.get_status("cam-a")
    assert s.connection == "error"
    assert "BLE scan timeout" in (s.last_error or "")


async def test_mode_propagates_to_driver():
    from gopro_mgmt.schemas import CameraConfig

    def factory(cfg: CameraConfig):
        return FakeDriver(cfg.target, enable_wifi=(cfg.mode == "ble+wifi"))

    mgr = CameraManager(
        [
            CameraConfig(id="ble-only", name="BLE only", target="0001", mode="ble"),
            CameraConfig(id="combo", name="Combo", target="0002", mode="ble+wifi"),
        ],
        driver_factory=factory,
    )
    await mgr.connect("ble-only")
    await mgr.connect("combo")

    by_target = {d.target: d for d in FakeDriver.instances}
    assert by_target["0001"].enable_wifi is False
    assert by_target["0002"].enable_wifi is True
    assert mgr.get_status("ble-only").mode == "ble"
    assert mgr.get_status("combo").mode == "ble+wifi"


async def test_start_then_stop_one(manager: CameraManager):
    await manager.connect("cam-a")
    s = await manager.start("cam-a")
    assert s.encoding is True
    s = await manager.stop("cam-a")
    assert s.encoding is False


async def test_start_without_connect_raises(manager: CameraManager):
    with pytest.raises(RuntimeError, match="not connected"):
        await manager.start("cam-a")


async def test_start_all_only_targets_connected(manager: CameraManager):
    await manager.connect("cam-a")
    # cam-b stays disconnected
    out = await manager.start_all()
    assert [s.id for s in out] == ["cam-a"]
    assert out[0].encoding is True
    assert manager.get_status("cam-b").encoding is None


async def test_start_all_broadcasts_in_parallel(manager: CameraManager):
    await manager.connect("cam-a")
    await manager.connect("cam-b")
    out = await manager.start_all()
    assert len(out) == 2
    assert all(s.encoding is True for s in out)


async def test_unknown_camera_raises(manager: CameraManager):
    with pytest.raises(CameraNotFound):
        await manager.connect("ghost")


async def test_disconnect_releases_driver(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    await manager.disconnect("cam-a")
    assert drv.is_open is False
    assert manager.get_status("cam-a").connection == "disconnected"


async def test_refresh_status_pulls_telemetry(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    drv.battery = 42
    drv.encoding = True
    drv.sd_remaining = 1234
    s = await manager.refresh_status("cam-a")
    assert s.battery_percent == 42
    assert s.encoding is True
    assert s.sd_remaining_sec == 1234


async def test_shutdown_disconnects_all(manager: CameraManager):
    await manager.connect("cam-a")
    await manager.connect("cam-b")
    await manager.shutdown()
    assert all(not d.is_open for d in FakeDriver.instances)


# ---- CRUD: add / remove / update --------------------------------------------


async def test_add_camera_appears_in_list(manager: CameraManager):
    new = CameraConfig(id="cam-c", name="Cam C", target="3333", mode="ble+wifi")
    status = await manager.add(new)
    assert status.id == "cam-c"
    assert status.mode == "ble+wifi"
    assert "cam-c" in manager.ids()


async def test_add_duplicate_id_raises(manager: CameraManager):
    with pytest.raises(CameraAlreadyExists):
        await manager.add(CameraConfig(id="cam-a", name="Dup", target="9999"))


async def test_remove_camera_removes_from_registry(manager: CameraManager):
    await manager.remove("cam-a")
    assert "cam-a" not in manager.ids()
    with pytest.raises(CameraNotFound):
        manager.get_status("cam-a")


async def test_remove_disconnects_first(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    await manager.remove("cam-a")
    assert drv.is_open is False
    assert "cam-a" not in manager.ids()


async def test_remove_unknown_raises(manager: CameraManager):
    with pytest.raises(CameraNotFound):
        await manager.remove("ghost")


async def test_update_name_no_disconnect(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    s = await manager.update("cam-a", name="Renamed")
    assert s.name == "Renamed"
    assert s.connection == "connected"
    assert drv.is_open is True


async def test_update_target_auto_disconnects(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    assert drv.is_open is True
    s = await manager.update("cam-a", target="0000")
    assert drv.is_open is False
    assert s.connection == "disconnected"
    assert s.target == "0000"


async def test_update_mode_auto_disconnects(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    s = await manager.update("cam-a", mode="ble+wifi")
    assert drv.is_open is False
    assert s.mode == "ble+wifi"


async def test_update_unchanged_target_does_not_disconnect(manager: CameraManager):
    await manager.connect("cam-a")
    drv = FakeDriver.instances[0]
    s = await manager.update("cam-a", target="1111")  # same value as fixture
    assert drv.is_open is True
    assert s.connection == "connected"


async def test_update_unknown_raises(manager: CameraManager):
    with pytest.raises(CameraNotFound):
        await manager.update("ghost", name="x")


async def test_export_configs_round_trips(manager: CameraManager):
    await manager.add(CameraConfig(id="cam-c", name="Cam C", target="ABCD", mode="ble+wifi"))
    cfgs = manager.export_configs()
    assert {c.id for c in cfgs} == {"cam-a", "cam-b", "cam-c"}
    cam_c = next(c for c in cfgs if c.id == "cam-c")
    assert cam_c.target == "ABCD"
    assert cam_c.mode == "ble+wifi"


# ---- COHN provisioning ------------------------------------------------------


@pytest.fixture
def _patch_provision_driver(monkeypatch):
    """Stub out WirelessGoProDriver so provisioning tests never load open_gopro.

    The manager's provision_cohn() instantiates a transient WirelessGoProDriver
    in BLE mode. We replace it with a tiny stub that mimics the new interface.
    """
    from gopro_mgmt import manager as mgr_mod

    class _StubDriver:
        def __init__(self, target, *, mode="ble"):
            self.target = target
            self.mode = mode

        async def provision_cohn(self, ssid, password):
            return {
                "ip_address": "192.168.1.42",
                "username": "gopro",
                "has_certificate": True,
            }

    monkeypatch.setattr(mgr_mod, "WirelessGoProDriver", _StubDriver, raising=False)
    yield


async def test_provision_cohn_marks_provisioned(_patch_provision_driver):
    from gopro_mgmt.manager import CameraManager
    from gopro_mgmt.schemas import CameraConfig

    def factory(cfg: CameraConfig):
        return FakeDriver(cfg.target, mode=cfg.mode)

    mgr = CameraManager(
        [CameraConfig(id="cam-c", name="Cam C", target="3333", mode="cohn")],
        driver_factory=factory,
    )
    s = await mgr.provision_cohn("cam-c", "MyWifi", "longerthan8")
    assert s.cohn_provisioned is True
    assert s.cohn_ip == "192.168.1.42"


async def test_provision_cohn_blocked_when_connected(manager, _patch_provision_driver):
    await manager.connect("cam-a")
    with pytest.raises(RuntimeError, match="must be disconnected"):
        await manager.provision_cohn("cam-a", "MyWifi", "longerthan8")


# ---- Post-stop recovery delay -----------------------------------------------


async def test_start_after_stop_sets_min_start_at(manager: CameraManager):
    """After a stop ACK, min_start_at is set POST_STOP_RECOVERY_SEC into the future."""
    await manager.connect("cam-a")
    await manager.start("cam-a")
    before = time.monotonic()
    await manager.stop("cam-a")
    entry = manager._entry("cam-a")
    assert entry.min_start_at >= before + POST_STOP_RECOVERY_SEC - 0.1


async def test_start_before_recovery_window_sleeps(manager: CameraManager, monkeypatch):
    """If min_start_at is in the future, _shutter sleeps with a positive delay."""
    positive_sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        if delay > 0:
            positive_sleeps.append(delay)

    # Connect first (FakeDriver.open calls sleep(0) which we don't count)
    await manager.connect("cam-a")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    manager._entry("cam-a").min_start_at = time.monotonic() + 2.0
    await manager.start("cam-a")

    assert len(positive_sleeps) == 1
    assert positive_sleeps[0] > 0


async def test_start_after_recovery_window_is_immediate(manager: CameraManager, monkeypatch):
    """If min_start_at is in the past, no positive sleep is performed."""
    positive_sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        if delay > 0:
            positive_sleeps.append(delay)

    await manager.connect("cam-a")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    manager._entry("cam-a").min_start_at = time.monotonic() - 1.0
    await manager.start("cam-a")

    assert positive_sleeps == []


async def test_dedup_concurrent_starts_fire_once(manager: CameraManager, monkeypatch):
    """Concurrent start calls during recovery window only send one BLE command."""
    await manager.connect("cam-a")
    await manager.start("cam-a")
    await manager.stop("cam-a")

    drv = FakeDriver.instances[0]
    initial_start_count = drv.start_count

    # Make sleep a no-op so both coroutines pass through instantly
    async def noop_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", noop_sleep)

    manager._entry("cam-a").min_start_at = time.monotonic() + 2.0
    await asyncio.gather(
        manager.start("cam-a"),
        manager.start("cam-a"),
    )

    # Only one BLE start_recording should have been sent
    assert drv.start_count == initial_start_count + 1


async def test_disconnect_clears_min_start_at(manager: CameraManager):
    """Disconnecting resets min_start_at so a fresh connect has no inherited delay."""
    await manager.connect("cam-a")
    await manager.start("cam-a")
    await manager.stop("cam-a")
    # min_start_at is now set
    assert manager._entry("cam-a").min_start_at > 0

    await manager.disconnect("cam-a")
    assert manager._entry("cam-a").min_start_at == 0.0
