# /project:run-tests

Run the test suite and interpret results.

## Commands

```bash
pytest -q                        # full suite (153 tests, ~10 s, no hardware)
pytest -q -k "ble+cohn"          # filter by keyword
pytest tests/test_manager.py -q  # single file
pytest -s -v                     # verbose with print output
```

## Test architecture

All tests are fully mocked. No GoPro hardware required.

| Fixture | Location | What it provides |
|---------|----------|-----------------|
| `FakeDriver` | `conftest.py` | In-memory driver stub |
| `manager` | `conftest.py` | `CameraManager` with `cam-a`, `cam-b` (mode=ble) |
| `client` | `conftest.py` | `httpx.AsyncClient` against FastAPI app |
| `_patch_provision_driver` | `test_manager.py` | Stubs `WirelessGoProDriver` for provisioning |

## Extending FakeDriver

When adding a driver method, add a stub to `FakeDriver` in `conftest.py`:

```python
async def my_new_method(self, arg: str) -> dict:
    return {"result": "ok"}  # override via subclass in specific tests
```

## Test templates

```python
# Manager test
async def test_my_feature(manager: CameraManager):
    await manager.connect("cam-a")
    result = await manager.my_feature("cam-a")
    assert result.some_field == expected

# Route test
async def test_my_route(client, manager):
    await manager.connect("cam-a")
    resp = await client.post("/api/cameras/cam-a/my-endpoint", json={"param": "val"})
    assert resp.status_code == 200

# Wrong-mode rejection
async def test_wrong_mode_409(client, manager):
    await manager.connect("cam-a")   # cam-a defaults to mode="ble"
    resp = await client.post("/api/cameras/cam-a/sync-time")
    assert resp.status_code == 409
```

## Baseline

`pytest -q` must always exit 0 failures. Current baseline: **153 passing**.
