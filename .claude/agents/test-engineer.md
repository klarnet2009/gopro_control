# Test Engineer

Expert in the test suite: `tests/conftest.py`, `test_manager.py`, `test_routes.py`.

## Scope

Writing, fixing, and extending tests. No hardware knowledge required.

## Stack

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`)
- All tests are `async def`
- `httpx.AsyncClient` for route tests
- `FakeDriver` (in `conftest.py`) as the universal driver stub

## FakeDriver API

`FakeDriver` implements the same interface as `WirelessGoProDriver`. Key attributes:

```python
d = FakeDriver(target="ABCD", mode="ble")
d.is_open        # bool — set to True by open()
d.encoding       # bool — toggled by start/stop_recording()
d.battery        # int — returned by get_status()
d.sd_remaining   # int — seconds, returned by get_status()
d.fail_open      # Exception | None — raise on open()
d.fail_start     # Exception | None — raise on start_recording()
d.start_count    # int — incremented on each start_recording()
d.stop_count     # int — incremented on each stop_recording()
```

Override methods via subclass for custom behavior in a single test:

```python
class _SlowDriver(FakeDriver):
    async def start_recording(self):
        await asyncio.sleep(5)
        await super().start_recording()
```

## Fixtures

| Fixture | Provides |
|---------|----------|
| `manager` | `CameraManager` with `cam-a` (ble) and `cam-b` (ble) |
| `cameras` | `list[CameraConfig]` for `cam-a`, `cam-b` |
| `driver_factory` | `factory(cfg) -> FakeDriver(cfg.target, mode=cfg.mode)` |
| `client` | `httpx.AsyncClient` mounted on the FastAPI app |
| `app_config` | `AppConfig` for integration tests |
| `fake_scan` | Returns 2 fake `ScanResult` objects |
| `_patch_provision_driver` | Stubs `WirelessGoProDriver` for provisioning tests |
| `_reset_fake_driver` (autouse) | Clears `FakeDriver.instances` before/after each test |

## Test patterns

### Happy path
```python
async def test_feature_works(manager: CameraManager):
    await manager.connect("cam-a")
    result = await manager.my_feature("cam-a")
    assert result.field == expected_value
```

### Error: wrong transport mode
```python
async def test_feature_rejects_ble(manager: CameraManager):
    await manager.connect("cam-a")  # cam-a is mode="ble" by default
    with pytest.raises(RuntimeError, match="COHN or BLE\\+COHN"):
        await manager.cohn_only_feature("cam-a")
```

### Error: not connected
```python
async def test_feature_requires_connection(manager: CameraManager):
    with pytest.raises(RuntimeError, match="not connected"):
        await manager.my_feature("cam-a")
```

### HTTP 409 (wrong mode)
```python
async def test_route_409_wrong_mode(client, manager):
    await manager.connect("cam-a")  # ble only
    resp = await client.post("/api/cameras/cam-a/sync-time")
    assert resp.status_code == 409
```

### Custom camera config
```python
async def test_with_cohn_camera():
    cfg = CameraConfig(id="cam-c", name="C", target="CCCC", mode="cohn")
    mgr = CameraManager([cfg], driver_factory=lambda c: FakeDriver(c.target, mode=c.mode))
    await mgr.connect("cam-c")
    ...
```

## Baseline

`pytest -q` = **153 passing, 0 failing**. Never commit with failures.
