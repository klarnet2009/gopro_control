# Frontend Engineer

Expert in `src/gopro_mgmt/web/` — vanilla JS, CSS, HTML.

## Scope

All client-side code: card rendering, WebSocket state sync, UI interactions.

## Architecture

```
WebSocket (ws://127.0.0.1:8000/ws)
  └─ receives full camera list on every poll tick (~2 s)
     └─ app.js applyState(cameras) diffs old vs new state
        └─ renderCard(cam) or updateCard(card, cam) per camera
```

## Key functions in `app.js`

| Function | Purpose |
|----------|---------|
| `applyState(cameras)` | Top-level state diff; calls `renderCard` for new cameras |
| `renderCard(cam)` | Clones `<template id="camera-card-template">`, populates, inserts into grid |
| `updateCard(card, cam)` | Updates an existing card's fields in-place |
| `formatSDTime(secs)` | Formats seconds → `3h 42m left` / `45s left` |
| `updateCohnInfoVisibility()` | Shows/hides COHN setup info in the add-camera dialog |
| `openEditDialog(cam)` | Populates the edit dialog with existing camera data |
| `emitToast(msg, type)` | Shows a transient notification (info / warning / error / success) |

## Design system

Palette (CSS custom properties in `:root`):
- `--ink` / `--ink-2` / `--ink-3` — warm blacks
- `--brass` / `--brass-dim` — primary accent
- `--ivory` / `--ivory-dim` — text
- `--tally-red` — recording indicator
- `--amber` — connecting/standby
- `--green-go` — connected/OK
- `--err` — error state

Fonts: `Geist` (UI) + `Geist Mono` (data values)

## Card state machine

```
offline → linking → live → recording
                  ↘ error → (reconnect) → live
```

The tally `data-state` attribute drives the visual:
`offline` | `linking` | `live` | `recording` | `error`

## Transport mode badges

```js
// In renderCard():
modeEl.dataset.mode = cam.mode;  // "ble" | "cohn" | "ble+cohn" | "ble+wifi"
```

CSS targets `.badge-mode[data-mode="ble+cohn"]` etc. for per-mode colors.

## Rules

1. The `<template>` in `index.html` is the canonical card structure. Never build card HTML via JS string concatenation.
2. All data values (battery %, SD time, RSSI) use `Geist Mono` via `.readout-value` class.
3. Any new camera state rendered on the card must also be updated in `updateCard()`, not just `renderCard()`.
4. Transport-gated features (PREVIEW, Sync Clocks, PROVISION button): check `cam.mode` in the JS predicate.

## Primary files

`src/gopro_mgmt/web/index.html`, `src/gopro_mgmt/web/app.js`, `src/gopro_mgmt/web/styles.css`
