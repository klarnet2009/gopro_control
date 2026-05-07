# GoPro Roll Call — Project Context for Claude

## What this project is

**GoPro Roll Call** is a web-based control panel for 2–8 GoPro cameras.
A FastAPI server runs on macOS, speaks to cameras over Bluetooth LE (via `open_gopro 0.22`)
and optionally over local Wi-Fi (COHN), and serves a vanilla-JS single-page UI.

Primary workflow: LINK a camera → ROLL → CUT → files land on the SD card.

---

## Architecture in one diagram

```
Browser (vanilla JS + WebSocket)
        │
        ▼
FastAPI (uvicorn)   ── HTTP ──► GoPro COHN (Wi-Fi)
  routes.py               ▲
  ws.py ──► poller        │
        │                 │ open_gopro SDK
        ▼            driver.py (WirelessGoProDriver)
  manager.py                    │
  (CameraManager)         ──────┤ BLE (bleak)
        │                       ▼
  schemas.py              GoPro HERO9-13
  config_store.py
  cohn_db.py (TinyDB)
  atem_watcher.py  ──── ATEM switcher (pyatem, mDNS)
```

---

## Transport modes

| Mode | Transport | Capabilities |
|------|-----------|--------------|
| `ble` | Bluetooth LE only | start/stop, battery %, SD remaining, BLE push observers |
| `cohn` | HTTP over local Wi-Fi | all BLE caps + clock sync + live MJPEG preview |
| `ble+cohn` | BLE for control + Wi-Fi for preview | fastest control + preview + clock sync (requires provisioning) |
| `ble+wifi` | BLE + camera AP (legacy) | start/stop only, no preview |

**Key transport helpers in `driver.py`:**
```python
@property
def _use_ble(self) -> bool:        # True for ble, ble+wifi, ble+cohn
    return self._mode in ("ble", "ble+wifi", "ble+cohn")

@property
def _use_cohn_http(self) -> bool:  # True for cohn, ble+cohn
    return self._mode in ("cohn", "ble+cohn")
```
**Always** use these properties in new driver code — never compare `self._mode` directly.

---

## Key files

| File | Role |
|------|------|
| `src/gopro_mgmt/driver.py` | `WirelessGoProDriver` — one instance per camera, wraps `open_gopro.WirelessGoPro` |
| `src/gopro_mgmt/manager.py` | `CameraManager` — registry of cameras, per-camera async locks, shutter dedup |
| `src/gopro_mgmt/api/routes.py` | FastAPI routes — thin HTTP layer, delegates to manager |
| `src/gopro_mgmt/api/ws.py` | WebSocket broadcast loop |
| `src/gopro_mgmt/schemas.py` | Pydantic models: `CameraConfig`, `CameraStatus`, `ConnectionMode` |
| `src/gopro_mgmt/capabilities.py` | `CAPABILITIES` dict mapping resolution/fps/lens/hypersmooth display names → API values |
| `src/gopro_mgmt/settings_map.py` | Maps open_gopro setting IDs ↔ human-readable names |
| `src/gopro_mgmt/cohn_db.py` | TinyDB read/write for COHN credentials (`cohn_db.json`) |
| `src/gopro_mgmt/config_store.py` | Persist camera list to `config.yaml` |
| `src/gopro_mgmt/atem_watcher.py` | Watches ATEM switcher tally, auto-triggers roll/cut |
| `src/gopro_mgmt/web/index.html` | Single-page UI skeleton + `<template id="camera-card-template">` |
| `src/gopro_mgmt/web/app.js` | All client-side logic: WebSocket, card rendering, state diffs |
| `src/gopro_mgmt/web/styles.css` | Broadcast-console aesthetic: warm-black + brass + tally-red |
| `tests/conftest.py` | `FakeDriver` — in-memory driver stub used across all tests |
| `tests/test_manager.py` | Manager-level async tests (no hardware) |
| `tests/test_routes.py` | HTTP route tests via `httpx.AsyncClient` |

---

## Adding a feature — canonical flow

Every feature touches exactly these layers in order:

1. **`schemas.py`** — add/extend Pydantic model if new config or status field
2. **`driver.py`** — implement at the transport level (`WirelessGoProDriver`)
3. **`manager.py`** — expose a high-level async method with per-camera lock
4. **`routes.py`** — thin HTTP endpoint: validate, delegate to manager, return JSON
5. **`index.html`** — add markup (use existing `<template>` pattern)
6. **`app.js`** — wire event handler, update `renderCard()` / `applyState()`
7. **`tests/test_manager.py`** — unit test with `FakeDriver`
8. **`tests/test_routes.py`** — integration test via `httpx.AsyncClient`

---

## Testing conventions

- All tests are fully mocked — no hardware required
- `FakeDriver` in `tests/conftest.py` is the canonical stub; extend it for new driver methods
- Tests are `async def` with `asyncio_mode = "auto"` (pytest-asyncio)
- Manager tests use the `manager` fixture (2 cameras: `cam-a`, `cam-b`)
- Route tests use the `client` fixture from `tests/conftest.py`
- Run: `pytest -q` → should always be 153+ passing, 0 failing

---

## Patterns to follow

### Per-camera lock
```python
async with e.lock:   # e = self._entry(cam_id)
    if e.driver is None:
        raise RuntimeError(f"camera {cam_id} is not connected")
    await e.driver.some_method()
```

### Transport guard
```python
# In driver.py method:
if not self._use_ble:
    raise RuntimeError("requires BLE transport")

# In manager.py method:
if e.config.mode not in ("cohn", "ble+cohn"):
    raise RuntimeError(f"camera {cam_id} must be in COHN or BLE+COHN mode")
```

### Route → 409 for wrong mode
```python
try:
    await mgr.some_cohn_method(cam_id)
except RuntimeError as exc:
    raise HTTPException(status_code=409, detail=str(exc))
```

### BLE push observers (real-time telemetry)
`_start_ble_detail_observers()` in `driver.py` starts 7 async tasks that subscribe to
BLE notifications for: battery, SD card, preset group, resolution, FPS, lens, hypersmooth.
Any new BLE mode (including `ble+cohn`) **must** call this after `open()`.

---

## ATEM integration

`atem_watcher.py` monitors an ATEM video switcher via `pyatem` (mDNS auto-discovery or
explicit `atem_host` in config). When the switcher goes live/off-air, it triggers
`manager.set_armed()` which auto-rolls any camera that connects while armed.

---

## What NOT to do

- Never compare `self._mode` directly in driver methods — use `_use_ble` / `_use_cohn_http`
- Never call `WirelessGoPro` with `wifi_adapter` that does real netsh/airport commands on Windows/Linux — use `_NullWifiController`
- Never skip `_start_ble_detail_observers()` in a new BLE transport branch
- Never import `open_gopro` at module level in tests — keep it behind `FakeDriver`
- Never add business logic in `routes.py` — it belongs in `manager.py`
