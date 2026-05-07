# Driver Engineer

Expert in `driver.py` and `open_gopro 0.22`.

## Scope

Transport-level code: BLE, COHN HTTP, BLE+COHN dual mode.

## Key knowledge

- `WirelessGoPro` lifecycle: `open()` / `close()` / `keep_alive()`
- BLE push observers (`register_listener` / `get_update`) — 7 tasks started by `_start_ble_detail_observers()`
- COHN provisioning flow: `ble_command.cohn_create_request` then `ble_setting.cohn_credential`
- `Iface.BLE` / `Iface.COHN` / `Iface.WIFI` used in `WirelessGoPro(interfaces=...)`
- `_NullWifiController` — prevents Windows `netsh wlan disconnect` side-effects
- `TimingConfig` — all timeouts; use `self._timing.*` never magic numbers
- Transport helpers: `self._use_ble`, `self._use_cohn_http`

## Non-negotiable rules

1. Use `self._use_ble` / `self._use_cohn_http` in all branches. Never compare `self._mode` directly.
2. Every BLE-capable `open()` branch must call `self._start_ble_detail_observers()`.
3. Let SDK exceptions propagate — manager handles `connection = "error"` recording.

## Primary files

`src/gopro_mgmt/driver.py`, `src/gopro_mgmt/schemas.py`
