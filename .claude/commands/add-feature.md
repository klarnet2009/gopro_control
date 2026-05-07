# /project:add-feature

Add a new full-stack feature to GoPro Roll Call.

## Usage

```
/project:add-feature <description of what to add>
```

## Steps

1. **Clarify scope** — camera capability (driver-level) or management feature (manager-level)?
   Does it require a new transport? New config field? New status field?

2. **`schemas.py`** — Add/extend Pydantic models first. Run `pytest -q` immediately.

3. **`driver.py`** — Implement in `WirelessGoProDriver`:
   - Always use `self._use_ble` / `self._use_cohn_http` guards (never `self._mode ==`)
   - Use `self._timing.*` for all timeouts
   - BLE transports: ensure `_start_ble_detail_observers()` is called in `open()`

4. **`manager.py`** — High-level async method:
   - `async with e.lock:` before touching `e.driver`
   - `if e.driver is None: raise RuntimeError(f"camera {cam_id} is not connected")`
   - Mode constraint: `if e.config.mode not in ("cohn", "ble+cohn"):`

5. **`routes.py`** — Thin FastAPI endpoint:
   - Delegate everything to manager
   - `CameraNotFound` → 404, `RuntimeError` for wrong mode → 409

6. **UI** — Markup in `<template id="camera-card-template">`, logic in `renderCard()` / `applyState()`

7. **Tests** — `test_manager.py` + `test_routes.py`; extend `FakeDriver` in `conftest.py` if needed

## Checklist

- [ ] `schemas.py` backward-compatible
- [ ] `driver.py` uses transport helper properties, not raw mode string
- [ ] `manager.py` acquires per-camera lock
- [ ] `routes.py` returns correct HTTP status codes
- [ ] `pytest -q` 100% green
- [ ] Atomic git commit
