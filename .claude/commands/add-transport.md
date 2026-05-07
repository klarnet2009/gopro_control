# /project:add-transport

Add a new transport mode to GoPro Roll Call.

## Usage

```
/project:add-transport <mode-name> <description>
```

Example: `/project:add-transport usb "USB-C tethered control via open_gopro HTTP"`

## Checklist

### 1. `schemas.py`
```python
# Extend the Literal:
ConnectionMode = Literal["ble", "ble+wifi", "cohn", "ble+cohn", "<new-mode>"]
```

### 2. `driver.py`

Add transport helper properties if the new mode forms a new axis:
```python
@property
def _use_<transport>(self) -> bool:
    return self._mode in ("<new-mode>", "<new-mode>+<combo>")
```

Add an `open()` branch:
```python
if self._mode == "<new-mode>":
    self._gopro = WirelessGoPro(
        target=self._target,
        interfaces={Iface.<X>},
        wifi_adapter=_NullWifiController,  # unless the mode manages AP
        cohn_db=COHN_DB_PATH,
    )
    await self._gopro.open()
    self._model = await self._read_model()
    self._start_keepalive()
    if self._use_ble:  # only if BLE is involved
        self._start_ble_detail_observers()
    log.info("opened camera target=%s mode=<new-mode> model=%s", self._target, self._model)
    return
```

Update every existing transport guard that is relevant:
- `start_recording`, `stop_recording`, `get_status` — update `if not self._use_ble:`
- `sync_time`, `start_webcam_rtsp` — update `if self._use_cohn_http:`
- `get_rssi` — update `if not self._use_ble:`

### 3. `manager.py`

Update mode lists in `sync_time`, `sync_time_all`, `start_preview` if the new mode supports them.

### 4. `index.html`

Add a new `<label class="mode-opt">` radio button in the dialog.
If the mode is conditional (requires provisioning), add `hidden` attribute and show via JS.

### 5. `app.js`

- `updateCohnInfoVisibility()` — include new mode if it needs a setup wizard
- Mode badge tooltip in `renderCard()`
- `previewable` and `cohnCapable` predicates
- `openEditDialog()` — show the new radio when appropriate

### 6. `styles.css`

Add a badge style:
```css
.badge-mode[data-mode="<new-mode>"] {
  border-color: rgba(<R>, <G>, <B>, 0.55);
  color: #<hex>;
}
```

### 7. Tests

- Manager tests for the new mode's capabilities
- Route tests for mode-rejection (409) on incompatible endpoints
- `pytest -q` must be 100% green

### 8. `config.example.yaml`

Add the new mode to the inline comment on the `mode:` field.
