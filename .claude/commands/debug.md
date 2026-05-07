# /project:debug

Debug a GoPro Roll Call issue.

## Usage

```
/project:debug <symptom or error message>
```

## Diagnostic trees

### BLE won't connect ("scan timeout" / "not found")

1. Is the camera awake and in pairing mode? Long-press Mode/Power until the Quik logo appears.
2. Has it been paired with GoPro Quik at least once? The OS BLE stack requires a prior pairing.
3. macOS Bluetooth permission: System Settings → Privacy & Security → Bluetooth → enable Terminal.
4. Check `driver.py` `open()` — does `WirelessGoPro(target=self._target)` match the last-4 serial?
5. Enable verbose logging: `--log-level debug` in `start.sh` or `config.yaml`. Look for `bleak` scan output.
6. Try `scanner.py` in isolation: `python -c "import asyncio; from gopro_mgmt.scanner import scan_cameras; print(asyncio.run(scan_cameras(10)))"`

### COHN connect fails

1. Same Wi-Fi? Camera and Mac must be on the same network.
2. Check `cohn_db.json` — does the `ip_address` still match? Camera may have gotten a new DHCP lease.
3. Curl test: `curl -k -u gopro:<password> https://<ip>/gopro/camera/state`
4. Certificate check: `has_certificate` must be `true` in `cohn_db.json`.
5. Re-provision: click ⚙ on the camera card → enter SSID/password again.

### Recording starts but camera reports an error

1. Is the camera already recording from a previous session? `GET /api/cameras/{id}/status` → check `encoding`.
2. SD card full? Check `sd_remaining_sec` — if `0`, the camera rejects start.
3. Battery < 10%? Camera auto-refuses shutter at critically low battery.
4. Mode mismatch: ensure camera is in Video mode (`set_preset_group("video")` in driver).

### WebSocket shows stale state

1. Check `poller.py` — is `poll_interval_sec` in `config.yaml` reasonable? Default is 2 s.
2. Look for `asyncio` task cancellation in logs — poller may have crashed silently.
3. Check `ws.py` `broadcast()` — it catches all exceptions; a serialization error silently drops updates.

### Tests failing

1. `FakeDriver` in `conftest.py` may be missing a method the real driver now has.
   Add a stub that returns a sane default.
2. `asyncio_mode = "auto"` is set in `pyproject.toml`; if a test hangs, it likely has an unresolved await.
3. Import errors → `open_gopro` missing from `.venv`? Run `pip install -e ".[dev]"`.

## Useful one-liners

```bash
# Check what cameras the BLE adapter can see right now
python -c "import asyncio; from gopro_mgmt.scanner import scan_cameras; import json; print(json.dumps([s.model_dump() for s in asyncio.run(scan_cameras(8))], indent=2))"

# Dump current camera state from the API
curl -s http://127.0.0.1:8000/api/cameras | python -m json.tool

# Tail the server log
tail -f /tmp/gopro-mgmt.log   # adjust path to match your start.sh LOG_FILE

# Check COHN credentials on disk
python -c "import json; print(json.dumps(json.load(open('cohn_db.json')), indent=2))"
```
