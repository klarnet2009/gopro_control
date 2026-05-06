from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gopro_mgmt.scanner import scan_gopros


def _device(name: str | None, address: str = "00:00:00:00:00:00", rssi: int = -60):
    return SimpleNamespace(name=name, address=address, rssi=rssi)


def _adv(local_name: str | None = None, rssi: int = -60):
    return SimpleNamespace(local_name=local_name, rssi=rssi)


class _FakeBleakScanner:
    def __init__(self, return_value):
        self._return_value = return_value

    async def discover(self, timeout=None, return_adv=False):  # noqa: ARG002
        return self._return_value


async def test_scan_filters_only_gopro_advertisements():
    devices = {
        "addr1": (_device("GoPro AB12", "addr1"), _adv("GoPro AB12", rssi=-40)),
        "addr2": (_device("Random Speaker", "addr2"), _adv("Random Speaker", rssi=-50)),
        "addr3": (_device(None, "addr3"), _adv(None, rssi=-90)),
        "addr4": (_device("GoPro CD34", "addr4"), _adv("GoPro CD34", rssi=-70)),
    }
    fake = _FakeBleakScanner(devices)

    with patch("bleak.BleakScanner", fake):
        results = await scan_gopros(timeout=0.1)

    assert [r.target for r in results] == ["AB12", "CD34"]
    assert results[0].rssi == -40
    assert results[1].rssi == -70
    assert results[0].address == "addr1"


async def test_scan_sorts_by_rssi_descending():
    devices = {
        "x": (_device("GoPro WEAK"), _adv("GoPro WEAK", rssi=-90)),
        "y": (_device("GoPro STRG"), _adv("GoPro STRG", rssi=-30)),
        "z": (_device("GoPro MIDD"), _adv("GoPro MIDD", rssi=-60)),
    }
    fake = _FakeBleakScanner(devices)
    with patch("bleak.BleakScanner", fake):
        results = await scan_gopros(timeout=0.1)
    assert [r.rssi for r in results] == [-30, -60, -90]


async def test_scan_empty():
    fake = _FakeBleakScanner({})
    with patch("bleak.BleakScanner", fake):
        results = await scan_gopros(timeout=0.1)
    assert results == []
