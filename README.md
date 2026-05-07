# GoPro Roll Call

Web-based control panel for 2–8 GoPro cameras.  
Start, stop, monitor recording, sync clocks — all from one browser tab.

## Supported cameras

Cameras that implement the Open GoPro API:

- HERO9 Black, HERO10, HERO11, HERO12, HERO13 Black
- GoPro MAX

HERO8 and earlier are not supported.

## Requirements

- **macOS 12 Monterey or later** (Bluetooth LE via `bleak`)
- **Python 3.11–3.13**
- Each camera paired once via the GoPro Quik app before first use (recommended for stable BLE)

## Install (macOS)

```bash
bash install.sh
```

The script creates a `.venv`, installs all dependencies, and copies `config.example.yaml → config.yaml`.

Or double-click **GoPro Roll Call.command** in Finder (first time: right-click → Open to bypass Gatekeeper).

## Configure

Edit `config.yaml` and set `target` to the **last 4 characters** of each camera's serial number  
(Settings → About → Camera Info on the camera).

### Transport modes

| Mode | How it works | Best for |
|------|-------------|----------|
| `ble` | Bluetooth LE only | Fast connect, start/stop recording, battery/SD status |
| `cohn` | HTTP over your home Wi-Fi (Camera on Home Network) | Clock sync, live preview (requires ffmpeg), reliable on busy sets |
| `ble+cohn` | BLE for control + Wi-Fi for preview/sync | Best of both: fastest shutter + preview + clock sync |

#### Setting up COHN

COHN and `ble+cohn` require a one-time provisioning step per camera:

1. Add the camera with any mode in `config.yaml`
2. In the web UI, click the **⚙ Provision COHN** button on the camera card
3. Enter your Wi-Fi SSID and password — the camera will join the network and reboot (~30 s)
4. Change the camera's `mode` to `cohn` or `ble+cohn` in `config.yaml` (or edit it in the UI)

`ble+cohn` uses BLE for all control commands (shutter, settings) and Wi-Fi only for live preview
and clock sync — no extra setup beyond provisioning.

After provisioning, click **LINK** — the camera connects over HTTP instead of BLE.

## Run

```bash
./start.sh          # starts server, opens http://127.0.0.1:8000 in your browser
./stop.sh           # stops the background server
```

Per-camera flow:

1. Click **LINK** on a camera card — connects via BLE or COHN.
2. Click **ROLL** (or **ROLL ALL**) to start recording.
3. Click **CUT** (or **CUT ALL**) to stop.
4. Files land on each camera's SD card.

### Clock sync (COHN only)

Click **Sync Clocks** in the bottom bar to set every connected COHN camera's clock to the server's  
current time (including timezone + DST). Accuracy ~50–200 ms — enough for multi-cam edit matching  
by timecode, not quite frame-accurate.

For frame-accurate sync, use a hardware LTC device such as a Tentacle Sync E connected via the  
camera's 3.5 mm audio input (requires Media Mod on HERO10 and earlier).

### Live preview (COHN + ffmpeg)

Install ffmpeg (`brew install ffmpeg`), then click **PREVIEW** on any connected COHN camera card  
to see a low-latency MJPEG stream in the browser.

## Tests

```bash
pytest -q
```

Tests run fully mocked — no hardware required.

## Troubleshooting

- **BLE scan finds nothing.** Camera must be awake and in pairing mode (long-press Mode/Power).  
  Pair once via GoPro Quik to register with the OS Bluetooth stack.
- **macOS Bluetooth permission.** First BLE connect triggers a system prompt — click *Allow*.  
  If you missed it: System Settings → Privacy & Security → Bluetooth → enable Terminal.
- **COHN connect fails.** Check that the camera and Mac are on the same Wi-Fi network.  
  Verify the IP shown in the card matches what's in `cohn_db.json`.
- **`set_shutter` returns error.** Camera refuses `start` if already encoding.  
  Check `/api/cameras/{id}/status` first.
- **Cameras out of sync (BLE mode).** `asyncio.gather` starts cameras in parallel — best-effort  
  sync (≪1 s drift). Use COHN + Sync Clocks or hardware timecode for tighter alignment.

## Security note

No authentication. Bind to `127.0.0.1` (the default) — do not expose port 8000 on a shared LAN.
