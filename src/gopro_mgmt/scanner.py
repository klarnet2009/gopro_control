"""BLE discovery for nearby GoPro cameras.

Uses `bleak` (already a transitive dep via open_gopro). Filters advertisements
by the canonical GoPro BLE name format `GoPro <last4>` and returns results
ranked by RSSI (closest first).
"""
from __future__ import annotations

import logging
import re

from .schemas import ScanResult

log = logging.getLogger(__name__)

_GOPRO_NAME_RE = re.compile(r"^GoPro\s+([A-Za-z0-9]{4})\s*$")


async def scan_gopros(timeout: float = 6.0) -> list[ScanResult]:
    """Scan BLE for GoPro advertisements and return them sorted by RSSI."""
    from bleak import BleakScanner  # lazy import — keeps test imports cheap

    log.info("starting BLE scan (timeout=%.1fs)", timeout)
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    results: list[ScanResult] = []
    items = devices.values() if isinstance(devices, dict) else devices
    for item in items:
        if isinstance(item, tuple):
            device, adv = item
        else:
            device, adv = item, None
        name = (getattr(adv, "local_name", None) or getattr(device, "name", None) or "")
        match = _GOPRO_NAME_RE.match(name)
        if not match:
            continue
        rssi = int(getattr(adv, "rssi", None) or getattr(device, "rssi", 0) or 0)
        results.append(
            ScanResult(
                name=name,
                target=match.group(1),
                rssi=rssi,
                address=getattr(device, "address", None),
            )
        )

    results.sort(key=lambda r: -r.rssi)
    log.info("BLE scan finished: %d GoPro(s)", len(results))
    return results
