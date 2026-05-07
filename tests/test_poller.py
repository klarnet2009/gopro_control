"""Integration tests for StatusPoller against FakeDriver.

The poller is a small loop, but the timing semantics matter:
  • only connected cameras are polled
  • each iteration broadcasts a status frame
  • cancellation is graceful and does not raise out of stop()
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gopro_mgmt.api.ws import WSBroadcaster
from gopro_mgmt.poller import StatusPoller


class _RecordingBroadcaster(WSBroadcaster):
    """WSBroadcaster that records every message instead of pushing to clients."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict[str, Any]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@pytest.fixture
def recording_bus() -> _RecordingBroadcaster:
    return _RecordingBroadcaster()


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll until predicate() is truthy or the timeout expires.

    StatusPoller clamps interval_sec to 0.5 s, so the first tick lands ~500 ms
    after start(). 5 s of headroom keeps the test stable on slow CI runners.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for predicate")


# Poller's interval is clamped to 0.5 s; tests pick what they're testing
# AGAINST the clamped value, not the requested value.
_TICK = 0.5


async def test_poller_skips_disconnected_cameras(manager, recording_bus):
    poller = StatusPoller(manager, recording_bus, interval_sec=0.1)
    poller.start()
    try:
        # Wait two clamped ticks. With nothing connected, neither tick should
        # produce status broadcasts.
        await asyncio.sleep(2 * _TICK + 0.1)
        status_msgs = [m for m in recording_bus.messages if m.get("type") == "status"]
        assert status_msgs == []
    finally:
        await poller.stop()


async def test_poller_broadcasts_status_for_connected_cameras(manager, recording_bus):
    await manager.connect("cam-a")

    poller = StatusPoller(manager, recording_bus, interval_sec=0.1)
    poller.start()
    try:
        await _wait_for(
            lambda: any(
                m.get("type") == "status" and m["payload"]["id"] == "cam-a"
                for m in recording_bus.messages
            )
        )
    finally:
        await poller.stop()


async def test_poller_stop_is_idempotent_and_graceful(manager, recording_bus):
    poller = StatusPoller(manager, recording_bus, interval_sec=0.1)
    poller.start()
    await asyncio.sleep(0.05)
    await poller.stop()
    # Calling stop a second time must not raise even though the task is gone.
    await poller.stop()


async def test_poller_only_polls_currently_connected_set(manager, recording_bus):
    """If cam-a connects, then disconnects, broadcasts for cam-a stop coming."""
    await manager.connect("cam-a")

    poller = StatusPoller(manager, recording_bus, interval_sec=0.05)
    poller.start()
    try:
        await _wait_for(
            lambda: any(
                m["payload"]["id"] == "cam-a"
                for m in recording_bus.messages
                if m.get("type") == "status"
            )
        )
        # Disconnect, then drain past the next tick so any in-flight refresh
        # finishes before we measure the boundary. With the 0.5 s clamp, one
        # tick is the minimum guaranteed quiescence window.
        await manager.disconnect("cam-a")
        await asyncio.sleep(_TICK + 0.1)
        boundary = len(recording_bus.messages)

        # After this point, cam-a is disconnected; no further status frames
        # should reference cam-a. Wait two ticks to be sure.
        await asyncio.sleep(2 * _TICK)
        new = recording_bus.messages[boundary:]
        cam_a_after_disconnect = [
            m for m in new
            if m.get("type") == "status" and m["payload"]["id"] == "cam-a"
        ]
        assert cam_a_after_disconnect == []
    finally:
        await poller.stop()


async def test_poller_clamps_interval_below_minimum():
    """Interval below 0.5 s gets clamped — protects the camera from BLE flooding."""
    bus = _RecordingBroadcaster()
    # Need a manager that the poller can ask for ids; we don't actually drive it.
    from gopro_mgmt.manager import CameraManager
    mgr = CameraManager([])
    poller = StatusPoller(mgr, bus, interval_sec=0.001)
    assert poller._interval == 0.5
