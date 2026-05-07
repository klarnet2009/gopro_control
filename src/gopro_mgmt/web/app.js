// ─── DOM refs ────────────────────────────────────────────────────────────
const cardsEl       = document.getElementById("cameras");
const tpl           = document.getElementById("camera-card-template");
const wsIndicator   = document.getElementById("ws-indicator");
const wsLabelEl     = wsIndicator.querySelector(".ws-label");
const railLive      = document.getElementById("rail-live");
const railLiveLabel = railLive.querySelector(".rail-live-label");
const railClock     = document.getElementById("rail-clock");
const emptyHint     = document.getElementById("empty-hint");
const toastStack    = document.getElementById("toast-stack");
const dialog        = document.getElementById("camera-dialog");
const dialogForm    = document.getElementById("camera-form");
const dialogTitle   = document.getElementById("dialog-title");
const dialogEyebrow = document.getElementById("dialog-eyebrow");
const dialogWarn    = document.getElementById("dialog-warn");
const dialogError   = document.getElementById("dialog-error");
const dialogSave    = document.getElementById("dialog-save");
const scanSection   = document.getElementById("scan-section");
const scanList      = document.getElementById("scan-list");
const scanEmpty     = document.getElementById("scan-empty");
const dialogRescan  = document.getElementById("dialog-rescan");
const btnScan       = document.getElementById("btn-scan");
const btnSyncTime   = document.getElementById("btn-sync-time");
const atemIndicator = document.getElementById("atem-indicator");
const atemLblEl     = atemIndicator.querySelector(".atem-lbl");
const atemConnEl    = document.getElementById("atem-conn");
const atemHostEl    = document.getElementById("atem-host");
const atemLastEl    = document.getElementById("atem-last");
const atemEventsEl  = document.getElementById("atem-events");
const atemAutoBtn   = document.getElementById("atem-auto-btn");
const btnAdd        = document.getElementById("btn-add");
const btnRollAll    = document.getElementById("btn-roll-all");
const btnCutAll     = document.getElementById("btn-cut-all");
const countArmed    = document.getElementById("count-armed");
const countTotal    = document.getElementById("count-total");
const countRolling  = document.getElementById("count-rolling");
const busHint       = document.getElementById("bus-hint");
const cohnInfo      = document.getElementById("cohn-info");


// ─── State ───────────────────────────────────────────────────────────────
const cardsById        = new Map(); // id -> {card, idx, errDismissed, wasConnected, settingsLoaded, autoReconnect, manualDisconnect}
const lastStatus       = new Map(); // id -> previous status (for diffing)
const recTimers        = new Map(); // id -> interval handle
const linkTimers       = new Map(); // id -> {t1, t2}
const autoReconTimers  = new Map(); // id -> setTimeout handle
let atemEvents = [];
let dialogMode = "add";
let nextChannelIdx = 1;

const AUTO_RECONNECT_KEY = id => `auto_reconnect_${id}`;
const CAM_MODEL_KEY      = id => `cam_model_${id}`;
const CAM_STATUS_KEY     = id => `cam_status_${id}`;

function cleanTelemetry(key, value) {
  if (value == null) return null;
  if (key === "battery_percent") {
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 && n <= 100 ? n : null;
  }
  if (key === "sd_remaining_sec") {
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 && n <= 7 * 24 * 3600 ? n : null;
  }
  if (key === "preset_group") {
    const n = Number(value);
    return [1000, 1001, 1002].includes(n) ? n : null;
  }
  return value;
}

function hydrateStatus(s) {
  const prev = lastStatus.get(s.id) || {};
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(CAM_STATUS_KEY(s.id)) || "{}"); }
  catch { saved = {}; }

  const telemetry = ["battery_percent", "sd_remaining_sec", "preset_group"];
  const stable = ["resolution", "fps", "lens", "hypersmooth", "model"];
  const out = { ...s };
  for (const key of telemetry) {
    out[key] = cleanTelemetry(key, out[key]);
    const prevValue = cleanTelemetry(key, prev[key]);
    if (out[key] == null && prevValue != null) out[key] = prevValue;
  }
  for (const key of stable) {
    if (out[key] == null) out[key] = prev[key] ?? saved[key] ?? out[key];
  }
  const snapshot = {};
  for (const key of stable) {
    if (out[key] != null) snapshot[key] = out[key];
  }
  if (Object.keys(snapshot).length) {
    localStorage.setItem(CAM_STATUS_KEY(s.id), JSON.stringify(snapshot));
  } else if (Object.keys(saved).some(key => telemetry.includes(key))) {
    localStorage.removeItem(CAM_STATUS_KEY(s.id));
  }
  return out;
}

// ─── REST helper ────────────────────────────────────────────────────────
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.detail || data?.error?.message || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  // CommandResult.failure() returns HTTP 200 with {ok: false, error: {message}}
  if (data?.ok === false) {
    throw new Error(data?.error?.message || "Command failed");
  }
  return data;
}

// ─── Format helpers ─────────────────────────────────────────────────────
function formatSDTime(sec) {
  if (sec == null || isNaN(sec)) return "—";
  if (sec < 60)  return `${sec}s left`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${sec%60}s left`;
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  return `${h}h ${m}m left`;
}

function batteryLevel(pct) {
  if (pct == null) return "ok";
  if (pct < 10) return "crit";
  if (pct < 20) return "crit";
  if (pct < 40) return "low";
  return "ok";
}

function rssiLevel(rssi) {
  if (rssi == null || isNaN(rssi)) return "unknown";
  if (rssi >= -55) return "ok";
  if (rssi >= -70) return "low";
  return "crit";
}

function fmtRecTime(elapsedSec) {
  const h = Math.floor(elapsedSec/3600);
  const m = Math.floor((elapsedSec%3600)/60);
  const s = elapsedSec%60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

// ─── REC timer (localStorage-persistent) ────────────────────────────────
function recStartKey(id)  { return `rec_start_${id}`; }
function ensureRecStart(id) {
  let ts = parseInt(localStorage.getItem(recStartKey(id)) || "0", 10);
  if (!ts) {
    ts = Date.now();
    localStorage.setItem(recStartKey(id), String(ts));
  }
  return ts;
}
function clearRecStart(id) {
  localStorage.removeItem(recStartKey(id));
}
function startRecTimer(id, timerEl) {
  const ts = ensureRecStart(id);
  const tick = () => {
    const elapsed = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    timerEl.textContent = fmtRecTime(elapsed);
  };
  tick();
  stopRecTimer(id);
  recTimers.set(id, setInterval(tick, 1000));
}
function stopRecTimer(id) {
  const h = recTimers.get(id);
  if (h) clearInterval(h);
  recTimers.delete(id);
}

// ─── LINKING escalation ─────────────────────────────────────────────────
function startLinkingEscalation(card, id) {
  stopLinkingEscalation(id);
  const tally = card.querySelector(".tally");
  let hintEl = card.querySelector(".connecting-hint");
  if (!hintEl) {
    hintEl = document.createElement("p");
    hintEl.className = "connecting-hint";
    tally.insertAdjacentElement("afterend", hintEl);
  }
  const t1 = setTimeout(() => {
    hintEl.textContent = "Pairing camera…";
    hintEl.classList.add("show");
  }, 5000);
  const t2 = setTimeout(() => {
    hintEl.textContent = "Still trying — make sure the camera is in pairing mode (Preferences → Connections → GoPro App).";
    hintEl.classList.add("show");
  }, 15000);
  linkTimers.set(id, { t1, t2, hintEl });
}
function stopLinkingEscalation(id) {
  const t = linkTimers.get(id);
  if (!t) return;
  clearTimeout(t.t1); clearTimeout(t.t2);
  if (t.hintEl) t.hintEl.remove(); // remove from DOM entirely so it doesn't linger in a11y tree
  linkTimers.delete(id);
}

// ─── COHN info visibility (Add/Edit dialog) ──────────────────────────────
function updateCohnInfoVisibility(mode, editingId = null) {
  if (!cohnInfo) return;
  const needsCohn = mode === "cohn" || mode === "ble+cohn";
  if (!needsCohn) { cohnInfo.classList.add("hidden"); return; }
  // If editing an already-provisioned camera, no need for wizard info
  if (editingId) {
    const s = lastStatus.get(editingId);
    if (s?.cohn_provisioned) { cohnInfo.classList.add("hidden"); return; }
  }
  cohnInfo.classList.remove("hidden");
}

// ─── Tally / badge resolver ─────────────────────────────────────────────
function resolveTally(s) {
  if (s.connection === "connecting") return { state: "connecting", label: "LINKING" };
  if (s.connection === "error")      return { state: "error",      label: "ERROR" };
  if (s.connection === "connected" && s.encoding === true)  return { state: "recording", label: "RECORDING" };
  if (s.connection === "connected" && s.encoding === false) return { state: "standby",   label: "STANDBY" };
  if (s.connection === "connected") return { state: "standby", label: "STANDBY" };
  return { state: "offline", label: "OFFLINE" };
}

// ─── Render a card ──────────────────────────────────────────────────────
function renderStatus(s) {
  s = hydrateStatus(s);
  let entry = cardsById.get(s.id);
  if (!entry) {
    const card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.id = s.id;
    const idx = nextChannelIdx++;
    card.style.setProperty("--i", String(idx - 1));
    cardsEl.appendChild(card);
    const savedAuto = localStorage.getItem(AUTO_RECONNECT_KEY(s.id)) === "1";
    entry = { card, idx, errDismissed: null, autoReconnect: savedAuto, manualDisconnect: false };
    cardsById.set(s.id, entry);
    wireCard(entry);
  }
  const { card, idx } = entry;

  // Channel number
  card.querySelector(".ch-num").textContent = String(idx).padStart(2, "0");

  // Title
  card.querySelector(".cam-name").textContent = s.name;
  card.querySelector(".cam-target").textContent = `#${s.target}`;

  // Model name — persist to localStorage so it survives offline state
  const modelEl = card.querySelector(".cam-model");
  const modelSepEl = card.querySelector(".cam-model-sep");
  if (s.model) {
    localStorage.setItem(CAM_MODEL_KEY(s.id), s.model);
  }
  const displayModel = s.model || localStorage.getItem(CAM_MODEL_KEY(s.id));
  if (displayModel) {
    modelEl.textContent = displayModel;
    modelSepEl.hidden = false;
  } else {
    modelEl.textContent = "";
    modelSepEl.hidden = true;
  }

  // LED
  const led = card.querySelector(".led");
  led.dataset.state = s.connection;

  // Transport mode badge (BLE / BLE+WIFI / COHN)
  const modeEl = card.querySelector(".cam-mode");
  modeEl.textContent = (s.mode || "ble").toUpperCase();
  modeEl.dataset.mode = s.mode || "ble";
  modeEl.title =
    (s.mode === "cohn")      ? "Transport: COHN only (HTTP over home Wi-Fi, enables live preview)"
  : (s.mode === "ble+cohn")  ? "Transport: BLE+COHN dual (BLE for control, Wi-Fi for preview & clock sync)"
  : (s.mode === "ble+wifi")  ? "Transport: BLE + Wi-Fi (also enables HTTP commands and media access)"
  :                             "Transport: BLE only (faster, sufficient for start/stop)";

  // Camera preset-group badge (VIDEO / PHOTO / TIMELAPSE)
  // preset_group: 1000=video, 1001=photo, 1002=timelapse; null=unknown
  const PRESET_LABELS = { 1000: "VIDEO", 1001: "PHOTO", 1002: "TIMELAPSE" };
  let presetBadge = card.querySelector(".cam-preset-group");
  if (!presetBadge) {
    presetBadge = document.createElement("span");
    presetBadge.className = "cam-preset-group badge-preset-group";
    presetBadge.setAttribute("tabindex", "0");
    modeEl.insertAdjacentElement("afterend", presetBadge);
    card.querySelector(".strip-meta").insertBefore(
      Object.assign(document.createElement("span"), { className: "dot-sep", textContent: "·" }),
      presetBadge
    );
  }
  const pgLabel = s.preset_group != null ? (PRESET_LABELS[s.preset_group] || `PG${s.preset_group}`) : null;
  if (pgLabel) {
    presetBadge.textContent = pgLabel;
    presetBadge.dataset.group = String(s.preset_group);
    presetBadge.title = s.preset_group === 1000
      ? "Camera is in Video mode — ready to record"
      : `Camera is in ${pgLabel} mode — will auto-switch to Video on ROLL`;
    presetBadge.hidden = false;
  } else {
    presetBadge.hidden = true;
  }

  // Tally
  const tally = card.querySelector(".tally");
  const tallyState = entry.stopping ? { state: "stopping", label: "STOPPING" } : resolveTally(s);
  tally.dataset.state = tallyState.state;
  tally.querySelector(".tally-label").textContent = tallyState.label;

  // REC timer
  const timerEl = tally.querySelector(".tally-timer");
  if (tallyState.state === "recording") {
    startRecTimer(s.id, timerEl);
  } else {
    stopRecTimer(s.id);
    clearRecStart(s.id);
    timerEl.textContent = tallyState.state === "standby" ? "READY" : "--:--";
  }

  // Battery
  const batteryBar = card.querySelector(".battery-bar");
  const battEl     = card.querySelector(".cam-batt");
  if (s.battery_percent != null) {
    const segments = batteryBar.querySelectorAll("i");
    const fill = Math.max(0, Math.min(8, Math.round(s.battery_percent / 12.5)));
    segments.forEach((seg, i) => {
      seg.classList.toggle("on", i < fill);
    });
    const lvl = batteryLevel(s.battery_percent);
    batteryBar.dataset.level = lvl;
    battEl.textContent = `${s.battery_percent}%`;
    battEl.dataset.warn = lvl === "crit" ? "crit" : (lvl === "low" ? "low" : "");
    battEl.title = s.battery_percent < 15
      ? "Low battery — BLE may become unreliable. Charge soon."
      : "";
  } else {
    batteryBar.querySelectorAll("i").forEach(i => i.classList.remove("on"));
    batteryBar.dataset.level = "ok";
    battEl.textContent = "—";
    battEl.dataset.warn = "";
  }

  // SD remaining
  const sdEl  = card.querySelector(".cam-sd");
  const diskBar = card.querySelector(".disk-bar i");
  if (s.sd_remaining_sec != null) {
    sdEl.textContent = formatSDTime(s.sd_remaining_sec);
    sdEl.dataset.warn = s.sd_remaining_sec < 60 ? "crit" : "";
    // Reference fill: 6h ≈ 21600 sec corresponds to ~256GB-ish
    const pct = Math.max(0, Math.min(100, (s.sd_remaining_sec / 21600) * 100));
    diskBar.style.width = `${pct}%`;
  } else {
    sdEl.textContent = "—";
    diskBar.style.width = "0%";
  }

  // BLE signal strength
  const sigRow  = card.querySelector(".cam-sig-row");
  const rssiEl  = card.querySelector(".cam-rssi");
  const rssiBar = card.querySelector(".cam-rssi-bar");
  sigRow.dataset.stale = (s.rssi_dbm != null && s.connection !== "connected") ? "true" : "false";
  if (s.rssi_dbm != null && s.mode !== "cohn") {
    rssiEl.textContent = `${s.rssi_dbm} dBm`;
    rssiEl.dataset.warn = rssiLevel(s.rssi_dbm);
    rssiBar.innerHTML = rssiBars(s.rssi_dbm);
    rssiBar.title = `BLE signal: ${s.rssi_dbm} dBm`;
  } else {
    rssiEl.textContent = "—";
    rssiEl.dataset.warn = "";
    rssiBar.innerHTML = rssiBars(null);
    rssiBar.title = "";
  }
  sigRow.hidden = s.mode === "cohn";

  // Resolution + FPS readout
  const resRow = card.querySelector(".cam-res-row");
  const resVal = card.querySelector(".cam-res-val");
  const resText = [
    s.resolution ? s.resolution.toUpperCase() : null,
    s.fps        ? `${s.fps} fps`              : null,
  ].filter(Boolean).join(" · ");
  if (resText) {
    resVal.textContent = resText;
    resRow.hidden = false;
  } else {
    resRow.hidden = true;
  }

  // Lens readout
  const lensRow = card.querySelector(".cam-lens-row");
  const lensVal = card.querySelector(".cam-lens-val");
  if (s.lens) {
    lensVal.textContent = s.lens;
    lensRow.hidden = false;
  } else {
    lensRow.hidden = true;
  }

  // Stabilization readout
  const hsRow = card.querySelector(".cam-hs-row");
  const hsVal = card.querySelector(".cam-hs-val");
  if (s.hypersmooth) {
    hsVal.textContent = s.hypersmooth;
    hsRow.hidden = false;
  } else {
    hsRow.hidden = true;
  }

  // Error block
  const errBlock = card.querySelector(".error-block");
  const errMsgEl = card.querySelector(".cam-err");
  const showErr = !!s.last_error && entry.errDismissed !== s.last_error;
  if (showErr) {
    errBlock.classList.remove("hidden");
    errMsgEl.textContent = s.last_error;
  } else {
    errBlock.classList.add("hidden");
  }

  // Buttons
  const btnLink = card.querySelector(".btn-link");
  const btnRoll = card.querySelector(".btn-roll");
  const isConnecting = s.connection === "connecting";
  const isConnected  = s.connection === "connected";
  const isRecording  = isConnected && s.encoding === true;

  if (isConnecting) {
    btnLink.dataset.state = "linking";
    btnLink.textContent = "CANCEL";
    btnLink.disabled = false;          // allow click to cancel
    startLinkingEscalation(card, s.id);
  } else if (isConnected) {
    btnLink.dataset.state = "linked";
    btnLink.textContent = "UNLINK";
    btnLink.disabled = false;
    stopLinkingEscalation(s.id);
  } else {
    btnLink.dataset.state = "";
    btnLink.textContent = "LINK";
    btnLink.disabled = false;
    stopLinkingEscalation(s.id);
  }

  if (isRecording) {
    btnRoll.dataset.mode = "cut";
    btnRoll.textContent = "■ CUT";
    btnRoll.disabled = false;
  } else {
    btnRoll.dataset.mode = "roll";
    btnRoll.textContent = "▶ ROLL";
    btnRoll.disabled = !isConnected;
  }

  // Settings panel — mode buttons + 4 selects
  const wasConnected = entry.wasConnected ?? false;
  entry.wasConnected = isConnected;

  // Mode buttons: highlight active, enable/disable
  card.querySelectorAll(".btn-mode").forEach(btn => {
    const modeId = parseInt(btn.dataset.mode, 10);
    btn.dataset.active = (s.preset_group === modeId) ? "true" : "false";
    btn.disabled = !isConnected || isRecording;
    btn.title = !isConnected ? "Connect camera to change mode"
      : isRecording ? "Cannot change mode while recording" : "";
  });

  const settingsStat = card.querySelector(".settings-status");
  const vidSelectors = [
    card.querySelector(".cam-res-sel"),
    card.querySelector(".cam-fps-sel"),
    card.querySelector(".cam-lens-sel"),
    card.querySelector(".cam-hs-sel"),
  ];

  if (!isConnected && wasConnected) {
    // Just disconnected — reset all selects
    vidSelectors.forEach(sel => {
      if (sel) { sel.innerHTML = '<option value="">—</option>'; sel.disabled = true; }
    });
    settingsStat.textContent   = "";
    settingsStat.dataset.state = "";
    entry.settingsLoaded = false;
  }
  // Fetch capabilities once per connect session
  if (isConnected && !entry.settingsLoaded) {
    entry.settingsLoaded = true;
    loadCameraSettings(s.id, card, entry);
  }
  // Keep selects disabled while recording or disconnected
  vidSelectors.forEach(sel => {
    if (!sel) return;
    sel.disabled = !isConnected || isRecording;
    sel.title = isRecording ? "Cannot change settings while recording"
      : isConnected ? "" : "Connect camera to change settings";
  });

  // PREVIEW button (visible for connected COHN or BLE+COHN cameras)
  const btnPreview = card.querySelector(".btn-preview");
  if (btnPreview) {
    const previewable = isConnected && (s.mode === "cohn" || s.mode === "ble+cohn");
    btnPreview.hidden = !previewable;
    if (!previewable) {
      // Force-hide the panel if camera left COHN/disconnected mid-stream
      stopPreview(entry, /*silent*/ true);
    }
  }

  // PROVISION icon (header) — visible for unprovisioned COHN-capable cameras
  const btnProv = card.querySelector(".btn-provision");
  if (btnProv) {
    const cohnCapable = s.mode === "cohn" || s.mode === "ble+cohn";
    btnProv.hidden = !(cohnCapable && !s.cohn_provisioned);
  }

  // Auto-reconnect check — must run before lastStatus.set so we can diff
  checkAutoReconnect(s, lastStatus.get(s.id), entry);

  // Diff toasts and previous-state record
  emitDiffToasts(s, lastStatus.get(s.id));
  lastStatus.set(s.id, s);

  refreshGlobalState();
}

function removeCard(id) {
  const entry = cardsById.get(id);
  if (entry) entry.card.remove();
  cardsById.delete(id);
  lastStatus.delete(id);
  stopRecTimer(id);
  stopLinkingEscalation(id);
  clearRecStart(id);
  const reconHandle = autoReconTimers.get(id);
  if (reconHandle) { clearTimeout(reconHandle); autoReconTimers.delete(id); }
  refreshGlobalState();
}

// ─── Auto-reconnect ──────────────────────────────────────────────────────
function checkAutoReconnect(curr, prev, entry) {
  if (!prev) return;
  if (!entry.autoReconnect) return;
  if (entry.manualDisconnect) return;
  const wasConnected = prev.connection === "connected";
  const nowDropped   = curr.connection === "disconnected" || curr.connection === "error";
  if (wasConnected && nowDropped) scheduleAutoReconnect(curr.id, entry);
}

function scheduleAutoReconnect(id, entry) {
  const existing = autoReconTimers.get(id);
  if (existing) clearTimeout(existing);
  const name = lastStatus.get(id)?.name || id;
  toast("info", `${name}: connection lost — auto-reconnecting in 3 s…`);
  const h = setTimeout(async () => {
    autoReconTimers.delete(id);
    const s = lastStatus.get(id);
    if (!s) return;
    if (s.connection === "connected") return;   // recovered on its own
    if (!entry.autoReconnect) return;           // user turned it off while waiting
    if (entry.manualDisconnect) return;         // user clicked UNLINK in the meantime
    try {
      await api("POST", `/api/cameras/${id}/connect`);
    } catch (err) {
      toast("error", `${name}: auto-reconnect failed — ${err.message}`);
    }
  }, 3000);
  autoReconTimers.set(id, h);
}

// ─── Wire card buttons ──────────────────────────────────────────────────
function wireCard(entry) {
  const id = entry.card.dataset.id;
  entry.card.querySelector(".btn-link").addEventListener("click", () => onLinkPress(entry));
  entry.card.querySelector(".btn-roll").addEventListener("click", () => onRollPress(entry));
  entry.card.querySelector(".btn-edit").addEventListener("click", () => openEditDialog(id));
  entry.card.querySelector(".btn-remove").addEventListener("click", () => confirmRemove(id));
  entry.card.querySelector(".btn-dismiss").addEventListener("click", () => {
    const s = lastStatus.get(id);
    entry.errDismissed = s?.last_error || null;
    entry.card.querySelector(".error-block").classList.add("hidden");
  });
  entry.card.querySelector(".btn-recover").addEventListener("click", () => onRecover(entry));
  entry.card.querySelectorAll(".btn-mode").forEach(btn => {
    btn.addEventListener("click", () => onModeChange(entry, parseInt(btn.dataset.mode, 10)));
  });

  // Auto-reconnect checkbox
  const autoCheckbox = entry.card.querySelector(".cam-auto-reconnect");
  if (autoCheckbox) {
    autoCheckbox.checked = entry.autoReconnect;
    autoCheckbox.addEventListener("change", () => {
      entry.autoReconnect = autoCheckbox.checked;
      localStorage.setItem(AUTO_RECONNECT_KEY(id), autoCheckbox.checked ? "1" : "0");
    });
  }

  entry.card.querySelector(".cam-res-sel").addEventListener("change", () => onSettingChange(entry, "resolution"));
  entry.card.querySelector(".cam-fps-sel").addEventListener("change", () => onSettingChange(entry, "fps"));
  entry.card.querySelector(".cam-lens-sel").addEventListener("change", () => onSettingChange(entry, "lens"));
  entry.card.querySelector(".cam-hs-sel").addEventListener("change",  () => onSettingChange(entry, "hypersmooth"));

  // PREVIEW button + stop button (COHN mode only)
  const btnPreview = entry.card.querySelector(".btn-preview");
  if (btnPreview) {
    btnPreview.addEventListener("click", () => onPreviewToggle(entry));
  }
  const btnPreviewStop = entry.card.querySelector(".btn-preview-stop");
  if (btnPreviewStop) {
    btnPreviewStop.addEventListener("click", () => stopPreview(entry));
  }
  // PROVISION (header icon) — only fires for unprovisioned COHN cameras
  const btnProv = entry.card.querySelector(".btn-provision");
  if (btnProv) {
    btnProv.addEventListener("click", () => onProvisionCohn(entry));
  }
}

async function onLinkPress(entry) {
  const id = entry.card.dataset.id;
  const s  = lastStatus.get(id);
  const isConnected  = s?.connection === "connected";
  const isConnecting = s?.connection === "connecting";
  try {
    if (isConnected || isConnecting) {
      // UNLINK or CANCEL — mark as manual so auto-reconnect doesn't fire
      entry.manualDisconnect = true;
      await api("POST", `/api/cameras/${id}/disconnect`);
    } else {
      // LINK — reset manual flag
      entry.manualDisconnect = false;
      await api("POST", `/api/cameras/${id}/connect`);
    }
  } catch (err) {
    toast("error", `${entry.card.querySelector(".cam-name").textContent}: ${err.message}`);
  }
}

async function onRollPress(entry) {
  const id = entry.card.dataset.id;
  const s  = lastStatus.get(id);
  const stopping = s?.encoding === true;
  const path = stopping ? `/api/cameras/${id}/record/stop` : `/api/cameras/${id}/record/start`;

  if (stopping) {
    entry.stopping = true;
    renderStatus({ ...s, encoding: false });
  }

  let stopOk = false;
  try {
    const r = await api("POST", path);
    if (r?.error) toast("error", r.error.message);
    else stopOk = true;
  } catch (err) {
    toast("error", err.message);
  } finally {
    if (stopping) {
      entry.stopping = false;
      const latest = lastStatus.get(id);
      if (latest) {
        // If stop succeeded, force encoding=false — lastStatus may still carry a
        // stale encoding=true from a poller update that arrived mid-flight.
        renderStatus(stopOk ? { ...latest, encoding: false } : latest);
      }
    }
  }
}

async function onRecover(entry) {
  const id = entry.card.dataset.id;
  const btn = entry.card.querySelector(".btn-recover");
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "RECONNECTING…";
  try {
    await api("POST", `/api/cameras/${id}/disconnect`).catch(() => {});
    await api("POST", `/api/cameras/${id}/connect`);
    entry.errDismissed = lastStatus.get(id)?.last_error || null;
    toast("info", `${entry.card.querySelector(".cam-name").textContent}: reconnect attempted`);
  } catch (err) {
    toast("error", `Reconnect failed: ${err.message}`);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

// ─── Camera settings ─────────────────────────────────────────────────────
async function loadCameraSettings(id, card, entry) {
  const selRes  = card.querySelector(".cam-res-sel");
  const selFps  = card.querySelector(".cam-fps-sel");
  const selLens = card.querySelector(".cam-lens-sel");
  const selHs   = card.querySelector(".cam-hs-sel");
  const stat    = card.querySelector(".settings-status");

  stat.textContent   = "…";
  stat.dataset.state = "busy";
  try {
    const r    = await api("GET", `/api/cameras/${id}/settings`);
    const caps = r.data || {};

    function populateSel(sel, options, currentVal) {
      if (!sel) return;
      sel.innerHTML = "";
      const blank = document.createElement("option");
      blank.value = ""; blank.textContent = "—";
      sel.appendChild(blank);
      for (const opt of (options || [])) {
        const o = document.createElement("option");
        o.value = opt; o.textContent = opt;
        if (opt === currentVal) o.selected = true;
        sel.appendChild(o);
      }
    }

    const s = lastStatus.get(id) || {};
    populateSel(selRes,  caps.supported_resolutions || [], caps.resolution   || s.resolution   || "");
    populateSel(selFps,  caps.supported_fps         || [], caps.fps          || s.fps          || "");
    populateSel(selLens, caps.supported_lenses       || [], caps.lens         || s.lens         || "");
    populateSel(selHs,   caps.supported_hypersmooth  || [], caps.hypersmooth  || s.hypersmooth  || "");

    // Re-apply disabled state
    const locked = !!(s?.encoding) || s?.connection !== "connected";
    [selRes, selFps, selLens, selHs].forEach(sel => { if (sel) sel.disabled = locked; });

    stat.textContent   = "✓";
    stat.dataset.state = "ok";
    setTimeout(() => {
      if (stat.dataset.state === "ok") {
        stat.textContent = ""; stat.dataset.state = "";
      }
    }, 2000);
  } catch (err) {
    stat.textContent   = "!";
    stat.dataset.state = "err";
    stat.title         = err.message;
    entry.settingsLoaded = false; // allow retry on next render cycle
  }
}

async function onModeChange(entry, modeId) {
  const id  = entry.card.dataset.id;
  const btn = entry.card.querySelector(`.btn-mode[data-mode="${modeId}"]`);
  const stat = entry.card.querySelector(".mode-status");
  const MODE_NAMES = { 1000: "video", 1001: "photo", 1002: "timelapse" };
  const mode = MODE_NAMES[modeId];
  if (!mode) return;

  entry.card.querySelectorAll(".btn-mode").forEach(b => { b.disabled = true; });
  stat.textContent   = "…";
  stat.dataset.state = "busy";
  try {
    await api("POST", `/api/cameras/${id}/mode`, { mode });
    stat.textContent   = "✓";
    stat.dataset.state = "ok";
    setTimeout(() => {
      if (stat.dataset.state === "ok") { stat.textContent = ""; stat.dataset.state = ""; }
    }, 2000);
    toast("success", `${lastStatus.get(id)?.name || id}: switched to ${mode}`);
  } catch (err) {
    stat.textContent   = "!";
    stat.dataset.state = "err";
    stat.title         = err.message;
    toast("error", `Mode change failed: ${err.message}`);
  } finally {
    const s = lastStatus.get(id);
    const locked = !!(s?.encoding) || s?.connection !== "connected";
    entry.card.querySelectorAll(".btn-mode").forEach(b => { b.disabled = locked; });
  }
}

async function onSettingChange(entry, field) {
  const id      = entry.card.dataset.id;
  const selRes  = entry.card.querySelector(".cam-res-sel");
  const selFps  = entry.card.querySelector(".cam-fps-sel");
  const selLens = entry.card.querySelector(".cam-lens-sel");
  const selHs   = entry.card.querySelector(".cam-hs-sel");
  const stat    = entry.card.querySelector(".settings-status");

  const payload = {};
  if (field === "resolution")  payload.resolution  = selRes?.value  || null;
  if (field === "fps")         payload.fps         = selFps?.value  || null;
  if (field === "lens")        payload.lens        = selLens?.value || null;
  if (field === "hypersmooth") payload.hypersmooth = selHs?.value   || null;
  if (!Object.values(payload).some(Boolean)) return;

  [selRes, selFps, selLens, selHs].forEach(sel => { if (sel) sel.disabled = true; });
  stat.textContent   = "…";
  stat.dataset.state = "busy";
  try {
    await api("POST", `/api/cameras/${id}/settings`, payload);
    stat.textContent   = "✓";
    stat.dataset.state = "ok";
    setTimeout(() => {
      if (stat.dataset.state === "ok") { stat.textContent = ""; stat.dataset.state = ""; }
    }, 2000);
    const changed = Object.entries(payload).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ");
    toast("success", `${lastStatus.get(id)?.name || id}: ${changed}`);
  } catch (err) {
    stat.textContent   = "!";
    stat.dataset.state = "err";
    stat.title         = err.message;
    toast("error", `Settings failed: ${err.message}`);
    // Re-fetch capabilities so the dropdown drops the invalid value
    entry.settingsLoaded = false;
    loadCameraSettings(id, entry.card, entry);
  } finally {
    const s = lastStatus.get(id);
    const locked = !!(s?.encoding) || s?.connection !== "connected";
    [selRes, selFps, selLens, selHs].forEach(sel => { if (sel) sel.disabled = locked; });
  }
}

// ─── Master bus ─────────────────────────────────────────────────────────
function refreshGlobalState() {
  const all = Array.from(cardsById.values()).map(e => lastStatus.get(e.card.dataset.id)).filter(Boolean);
  const total      = all.length;
  const connected  = all.filter(s => s.connection === "connected");
  const rolling    = connected.filter(s => s.encoding === true);
  const armed      = connected.length;

  countArmed.textContent   = String(armed);
  countTotal.textContent   = String(total);
  countRolling.textContent = String(rolling.length);

  // Roll All / Cut All availability
  const canRoll = connected.some(s => !s.encoding);
  const canCut  = rolling.length > 0;
  btnRollAll.disabled = !canRoll;
  btnCutAll.disabled  = !canCut;

  btnRollAll.title = canRoll
    ? `Start recording on ${connected.filter(s => !s.encoding).length} camera(s)`
    : (armed === 0 ? "No cameras connected" : "All connected cameras are already rolling");
  btnCutAll.title  = canCut
    ? `Stop recording on ${rolling.length} camera(s)`
    : "No cameras are recording";

  // Sync Clocks button — enabled when at least one COHN or BLE+COHN camera is connected
  const cohnConnected = connected.filter(s => s.mode === "cohn" || s.mode === "ble+cohn");
  if (btnSyncTime) {
    btnSyncTime.disabled = cohnConnected.length === 0;
    btnSyncTime.title = cohnConnected.length === 0
      ? "No COHN cameras connected"
      : `Sync clock on ${cohnConnected.length} COHN camera${cohnConnected.length > 1 ? "s" : ""} to server time`;
  }

  // bus hint when nothing armed but cameras exist
  if (total > 0 && armed === 0) {
    busHint.classList.remove("hidden");
  } else {
    busHint.classList.add("hidden");
  }

  // Empty state
  if (total === 0) {
    emptyHint.classList.remove("hidden");
    cardsEl.classList.add("hidden");
  } else {
    emptyHint.classList.add("hidden");
    cardsEl.classList.remove("hidden");
  }

  // Top rail LIVE indicator
  if (rolling.length > 0) {
    railLive.dataset.state = "live";
    railLiveLabel.textContent = `LIVE — ${rolling.length} ROLLING`;
  } else {
    railLive.dataset.state = "idle";
    railLiveLabel.textContent = "STANDBY";
  }
}

btnRollAll.addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/cameras/record/start");
    (r.data || []).forEach(renderStatus);
    const n = (r.data || []).filter(s => s.encoding).length;
    if (n > 0) toast("success", `Recording started on ${n} camera${n > 1 ? "s" : ""}`);
  } catch (err) {
    toast("error", err.message);
  }
});

btnCutAll.addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/cameras/record/stop");
    (r.data || []).forEach(renderStatus);
    toast("success", "All cameras cut");
  } catch (err) {
    toast("error", err.message);
  }
});

// ─── Sync Clocks ────────────────────────────────────────────────────────
btnSyncTime?.addEventListener("click", async () => {
  const icon = btnSyncTime.querySelector(".sync-time-icon");
  const origText = btnSyncTime.textContent;
  btnSyncTime.disabled = true;
  if (icon) icon.textContent = "⏳";
  try {
    const r = await api("POST", "/api/cameras/sync-time");
    const results = r.data || {};
    const ok    = Object.values(results).filter(v => v === "ok").length;
    const failed = Object.entries(results).filter(([, v]) => v !== "ok");
    if (failed.length === 0) {
      toast("success", `Clocks synced on ${ok} camera${ok !== 1 ? "s" : ""}`);
    } else {
      failed.forEach(([id, err]) => toast("error", `${id}: ${err}`));
      if (ok > 0) toast("success", `Clocks synced on ${ok} camera${ok !== 1 ? "s" : ""}`);
    }
  } catch (err) {
    toast("error", `Sync failed: ${err.message}`);
  } finally {
    if (icon) icon.textContent = "⏱";
    btnSyncTime.disabled = false;
  }
});

// ─── Add / Edit / Remove ────────────────────────────────────────────────
function resetDialog() {
  dialogForm.reset();
  dialogWarn.classList.add("hidden");
  dialogError.classList.add("hidden");
  scanSection.classList.add("hidden");
  scanList.innerHTML = "";
  scanEmpty.classList.add("hidden");
  if (cohnInfo) cohnInfo.classList.add("hidden");
  // BLE+COHN option hidden by default; shown only when camera is already provisioned
  const dualOpt = document.getElementById("mode-opt-blecohn");
  if (dualOpt) dualOpt.hidden = true;
  dialogForm.elements.id.readOnly = false;
}

function openAddDialog() {
  dialogMode = "add";
  resetDialog();
  dialogEyebrow.textContent = "PATCH NEW CHANNEL";
  dialogTitle.textContent = "Add Camera";
  dialogSave.textContent  = "Add";
  dialog.showModal();
  setTimeout(() => dialogForm.elements.id.focus(), 30);
}

function openEditDialog(id) {
  const entry = cardsById.get(id);
  if (!entry) return;
  const s = lastStatus.get(id);
  dialogMode = "edit";
  resetDialog();
  dialogEyebrow.textContent = `EDIT CHANNEL ${String(entry.idx).padStart(2, "0")}`;
  dialogTitle.textContent = entry.card.querySelector(".cam-name").textContent;
  dialogSave.textContent  = "Save";
  dialogForm.elements.id.value = id;
  dialogForm.elements.id.readOnly = true;
  dialogForm.elements.name.value   = s?.name || "";
  dialogForm.elements.target.value = s?.target || "";
  const mode = s?.mode || "ble";
  Array.from(dialogForm.elements.mode).forEach(r => { r.checked = (r.value === mode); });

  // Show BLE+COHN option only when camera has been provisioned
  const dualOpt = document.getElementById("mode-opt-blecohn");
  if (dualOpt) dualOpt.hidden = !s?.cohn_provisioned;

  if (s?.connection === "connected") {
    dialogWarn.textContent = "Changing target or mode will auto-disconnect the camera.";
    dialogWarn.classList.remove("hidden");
  } else if ((mode === "cohn" || mode === "ble+cohn") && !s?.cohn_provisioned) {
    dialogWarn.textContent = "This camera is not yet provisioned. Save first, then click the gear icon on the card to provision.";
    dialogWarn.classList.remove("hidden");
  }
  updateCohnInfoVisibility(mode, id);
  dialog.showModal();
  setTimeout(() => dialogForm.elements.name.focus(), 30);
}

async function confirmRemove(id) {
  const entry = cardsById.get(id);
  if (!entry) return;
  const name = entry.card.querySelector(".cam-name").textContent;
  const s = lastStatus.get(id);
  const isConnected = s?.connection === "connected";
  const msg = isConnected
    ? `Remove "${name}"? It is currently connected and will be disconnected first.`
    : `Remove "${name}"?`;
  if (!window.confirm(msg)) return;
  try {
    await api("DELETE", `/api/cameras/${id}`);
    removeCard(id);
    toast("info", `Channel "${name}" removed`);
  } catch (err) {
    toast("error", `Remove failed: ${err.message}`);
  }
}

dialogForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  dialogError.classList.add("hidden");
  const fd = new FormData(dialogForm);
  const body = Object.fromEntries(fd.entries());
  try {
    if (dialogMode === "add") {
      const r = await api("POST", "/api/cameras", body);
      if (r.data) renderStatus(r.data);
      toast("success", `Channel "${body.name}" added`);
      dialog.close();
      // Auto-trigger COHN provision wizard if COHN-capable but not yet provisioned
      const needsProvision = (body.mode === "cohn" || body.mode === "ble+cohn");
      if (needsProvision && r.data && !r.data.cohn_provisioned) {
        setTimeout(() => {
          const entry = cardsById.get(body.id);
          if (entry) onProvisionCohn(entry);
        }, 250);
      }
    } else {
      const id = body.id;
      delete body.id;
      const r = await api("PATCH", `/api/cameras/${id}`, body);
      if (r.data) renderStatus(r.data);
      dialog.close();
      // Auto-trigger COHN provision wizard if switched to COHN-capable and not provisioned
      const needsProvision = (body.mode === "cohn" || body.mode === "ble+cohn");
      if (needsProvision && r.data && !r.data.cohn_provisioned) {
        setTimeout(() => {
          const entry = cardsById.get(id);
          if (entry) onProvisionCohn(entry);
        }, 250);
      }
    }
  } catch (err) {
    dialogError.textContent = String(err.message || err);
    dialogError.classList.remove("hidden");
  }
});

// Show COHN wizard info when transport radio changes
dialogForm.addEventListener("change", (ev) => {
  if (ev.target.name === "mode") {
    const editId = dialogMode === "edit" ? dialogForm.elements.id?.value : null;
    updateCohnInfoVisibility(ev.target.value, editId);
  }
});

document.getElementById("dialog-cancel").addEventListener("click", () => dialog.close());
btnAdd.addEventListener("click", openAddDialog);
document.getElementById("empty-add").addEventListener("click", openAddDialog);
document.getElementById("empty-scan").addEventListener("click", () => runScan());

// ─── COHN provisioning + preview ─────────────────────────────────────────
const provisionDialog = document.getElementById("provision-dialog");
const provisionForm   = document.getElementById("provision-form");
const provisionError  = document.getElementById("provision-error");
const provisionName   = document.getElementById("provision-name");
let provisionTargetId = null;

async function onProvisionCohn(entry) {
  provisionTargetId = entry.card.dataset.id;
  provisionName.textContent = entry.card.querySelector(".cam-name").textContent;
  provisionForm.reset();
  provisionError.classList.add("hidden");
  provisionDialog.showModal();
  // Pre-fill SSID from host Wi-Fi (best-effort, silent on failure)
  try {
    const r = await api("GET", "/api/wifi-ssid");
    const ssid = r?.data?.ssid;
    if (ssid && provisionForm.elements.ssid) {
      provisionForm.elements.ssid.value = ssid;
      setTimeout(() => provisionForm.elements.password?.focus(), 30);
    } else {
      setTimeout(() => provisionForm.elements.ssid?.focus(), 30);
    }
  } catch {
    setTimeout(() => provisionForm.elements.ssid?.focus(), 30);
  }
}

document.getElementById("provision-cancel").addEventListener("click", () => provisionDialog.close());

provisionForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(provisionForm);
  const body = { ssid: fd.get("ssid"), password: fd.get("password") };
  const submit = document.getElementById("provision-submit");
  submit.disabled = true;
  submit.textContent = "Provisioning…";
  provisionError.classList.add("hidden");
  try {
    const r = await api("POST", `/api/cameras/${provisionTargetId}/provision-cohn`, body);
    if (r.data) renderStatus(r.data);
    toast("success", `${provisionName.textContent}: joined Wi-Fi ✓ — connecting via COHN…`);
    provisionDialog.close();
    // Step 2 of 2: auto-connect via COHN
    const connectId = provisionTargetId;
    if (connectId) {
      setTimeout(async () => {
        try {
          await api("POST", `/api/cameras/${connectId}/connect`);
          toast("success", `${provisionName.textContent}: connected via COHN`);
        } catch (err) {
          toast("warning", `${provisionName.textContent}: provisioned — click LINK to connect`);
        }
      }, 800);
    }
  } catch (err) {
    provisionError.textContent = err.message;
    provisionError.classList.remove("hidden");
  } finally {
    submit.disabled = false;
    submit.textContent = "Provision";
  }
});

function onPreviewToggle(entry) {
  const id = entry.card.dataset.id;
  const panel = entry.card.querySelector(".preview-panel");
  if (!panel) return;
  if (!panel.classList.contains("hidden")) {
    stopPreview(entry);
    return;
  }
  const img = panel.querySelector(".preview-img");
  // Browser fetches the MJPEG stream directly via <img src>.
  img.src = `/api/cameras/${id}/preview?_t=${Date.now()}`;
  panel.classList.remove("hidden");
  const btn = entry.card.querySelector(".btn-preview");
  if (btn) btn.textContent = "STOP PREVIEW";
}

function stopPreview(entry, silent = false) {
  const id = entry.card.dataset.id;
  const panel = entry.card.querySelector(".preview-panel");
  if (!panel) return;
  const img = panel.querySelector(".preview-img");
  // Was the preview actually running before this call?
  const wasOpen = !panel.classList.contains("hidden");
  if (img) img.src = "";       // cancels the streaming GET
  panel.classList.add("hidden");
  const btn = entry.card.querySelector(".btn-preview");
  if (btn) btn.textContent = "PREVIEW";
  if (!wasOpen) return;        // nothing to tear down server-side
  // Tell the server to stop ffmpeg + webcam.
  fetch(`/api/cameras/${id}/preview`, { method: "DELETE" }).catch(() => {});
  if (!silent) toast("info", `${lastStatus.get(id)?.name || id}: preview stopped`);
}

// ─── BLE scan ───────────────────────────────────────────────────────────
async function runScan() {
  if (btnScan.dataset.busy === "true") return;
  btnScan.dataset.busy = "true";
  dialogRescan.disabled = true;
  scanEmpty.classList.add("hidden");
  scanList.innerHTML = "";
  scanSection.classList.remove("hidden");
  if (!dialog.open) openAddDialog();
  scanSection.classList.remove("hidden");

  toast("info", "Scanning for nearby GoPros…");
  try {
    const r = await api("POST", "/api/scan");
    const found = r.data || [];
    if (!found.length) {
      scanEmpty.classList.remove("hidden");
      toast("warning", "Scan found no GoPros nearby");
    } else {
      for (const item of found) {
        const li = document.createElement("li");
        const bars = rssiBars(item.rssi);
        li.innerHTML = `
          <span class="scan-name"><b>${escapeHtml(item.name)}</b> <span class="scan-target">${escapeHtml(item.target)}</span></span>
          <span class="scan-rssi-bar">${bars}</span>
          <span class="readout-value">${item.rssi} dBm</span>
        `;
        li.addEventListener("click", () => {
          dialogForm.elements.target.value = item.target;
          if (!dialogForm.elements.name.value) dialogForm.elements.name.value = item.name;
          if (!dialogForm.elements.id.value) {
            dialogForm.elements.id.value = `cam-${item.target.toLowerCase()}`;
          }
        });
        scanList.appendChild(li);
      }
      toast("success", `Found ${found.length} GoPro${found.length > 1 ? "s" : ""}`);
    }
  } catch (err) {
    dialogError.textContent = `Scan failed: ${err.message || err}`;
    dialogError.classList.remove("hidden");
    toast("error", `Scan failed: ${err.message}`);
  } finally {
    btnScan.dataset.busy = "";
    dialogRescan.disabled = false;
  }
}

function rssiBars(rssi) {
  // -30 strong … -90 weak
  const strength = rssi == null || isNaN(rssi)
    ? 0
    : Math.max(0, Math.min(4, Math.round(((rssi + 90) / 60) * 4)));
  let html = "";
  for (let i = 0; i < 4; i++) {
    html += `<i class="${i < strength ? "on" : ""}"></i>`;
  }
  return html;
}

btnScan.addEventListener("click", runScan);
dialogRescan.addEventListener("click", runScan);

// ─── Toasts ─────────────────────────────────────────────────────────────
const TOAST_TTL = 3500;
function toast(kind, message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.kind = kind;
  el.textContent = message;
  toastStack.appendChild(el);
  // Cap at 5
  while (toastStack.children.length > 5) toastStack.firstChild.remove();
  setTimeout(() => {
    el.classList.add("fade");
    setTimeout(() => el.remove(), 220);
  }, TOAST_TTL);
}

function emitDiffToasts(curr, prev) {
  if (!prev) return;
  const name = curr.name;
  if (prev.connection !== "connected" && curr.connection === "connected") {
    toast("info", `${name}: connected`);
  } else if (prev.connection === "connected" && curr.connection === "disconnected") {
    if (curr.last_error && curr.last_error.includes("timed out")) {
      toast("warning", `${name}: auto-disconnected (BLE timeout)`);
    } else {
      toast("info", `${name}: disconnected`);
    }
  } else if (prev.connection !== "error" && curr.connection === "error") {
    toast("error", `${name}: ${curr.last_error || "error"}`);
  }
  // Recording transitions caught at command level via toast on Roll All / Cut All
}

// ─── Helpers ────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// ─── Clock ──────────────────────────────────────────────────────────────
function tickClock() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  railClock.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
tickClock();
setInterval(tickClock, 30 * 1000);

// ─── ATEM indicator ─────────────────────────────────────────────────────
function updateAtemIndicator(payload) {
  if (!payload || !payload.enabled) return;
  const auto = payload.auto_enabled !== false;
  atemAutoBtn.dataset.on = auto ? "true" : "false";

  let state, label;
  if (!payload.connected) {
    state = "searching";
    label = "ATEM";
  } else if (payload.recording && auto) {
    state = "recording";
    label = "ATEM · REC";
  } else if (payload.connected) {
    state = "connected";
    label = "ATEM";
  }
  atemIndicator.dataset.state = state;
  atemLblEl.textContent = label;
  atemConnEl.textContent = payload.connected
    ? (payload.recording ? "REC" : "ONLINE")
    : "SEARCH";
  atemConnEl.dataset.state = state;
  const nameStr = payload.name || "ATEM";
  const hostStr = payload.host ? ` · ${payload.host}` : "";
  atemIndicator.title = `${nameStr}${hostStr}`;
  atemHostEl.textContent = payload.host || "auto";
  if (payload.last_event) atemLastEl.textContent = payload.last_event.message || "—";
  if (Array.isArray(payload.events)) {
    atemEvents = payload.events.slice(-12);
    renderAtemEvents();
  }
}

function appendAtemEvent(event) {
  if (!event) return;
  atemEvents.push(event);
  atemEvents = atemEvents.slice(-12);
  atemLastEl.textContent = event.message || "—";
  renderAtemEvents();
}

function renderAtemEvents() {
  if (!atemEventsEl) return;
  atemEventsEl.innerHTML = "";
  const events = [...atemEvents].reverse();
  if (!events.length) {
    const li = document.createElement("li");
    li.dataset.level = "info";
    li.innerHTML = `<span>--:--:--</span><b>WAIT</b><em>—</em>`;
    atemEventsEl.appendChild(li);
    return;
  }
  for (const event of events) {
    const d = event.ts ? new Date(event.ts * 1000) : new Date();
    const pad = n => String(n).padStart(2, "0");
    const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    const li = document.createElement("li");
    li.dataset.level = event.level || "info";
    li.innerHTML = `<span>${t}</span><b>${escapeHtml(event.kind || "event")}</b><em>${escapeHtml(event.message || "")}</em>`;
    atemEventsEl.appendChild(li);
  }
}

atemAutoBtn.addEventListener("click", async () => {
  const next = atemAutoBtn.dataset.on !== "true";
  atemAutoBtn.dataset.on = next ? "true" : "false";
  try {
    const r = await api("POST", "/api/atem/auto", { enabled: next });
    if (r.data) updateAtemIndicator(r.data);
  } catch (err) {
    atemAutoBtn.dataset.on = next ? "false" : "true";
    toast("error", `ATEM auto: ${err.message}`);
  }
});

// ─── WebSocket ──────────────────────────────────────────────────────────
function setWsState(state) {
  wsIndicator.dataset.state = state;
  wsLabelEl.textContent = ({open: "live", closed: "offline", connecting: "linking…"}[state]) || state;
}

// Reconnect backoff state. Reset to baseline on a successful open so a brief
// network glitch doesn't push us to the 30 s cap; grows exponentially while
// we keep failing so the server isn't hammered after a long outage.
const WS_BACKOFF_MIN_MS = 1000;
const WS_BACKOFF_MAX_MS = 30000;
let wsBackoffMs = WS_BACKOFF_MIN_MS;

function nextWsBackoff() {
  // ±25 % jitter prevents stampede when many clients reconnect simultaneously
  // (e.g. server restart). Math.random returns [0, 1).
  const jitter = wsBackoffMs * (0.75 + Math.random() * 0.5);
  const delay = Math.min(jitter, WS_BACKOFF_MAX_MS);
  wsBackoffMs = Math.min(wsBackoffMs * 2, WS_BACKOFF_MAX_MS);
  return delay;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  setWsState("connecting");
  ws.addEventListener("open", () => {
    setWsState("open");
    wsBackoffMs = WS_BACKOFF_MIN_MS;
  });
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "hello":
        if (Array.isArray(msg.payload)) msg.payload.forEach(renderStatus);
        break;
      case "status":
      case "camera_added":
      case "camera_updated":
      case "cohn_provisioned":
        if (msg.payload) renderStatus(msg.payload);
        break;
      case "camera_removed":
        if (msg.payload?.id) removeCard(msg.payload.id);
        break;
      case "atem_status":
        updateAtemIndicator(msg.payload);
        break;
      case "atem_event":
        appendAtemEvent(msg.payload);
        break;
    }
  });
  ws.addEventListener("close", () => {
    setWsState("closed");
    setTimeout(connectWS, nextWsBackoff());
  });
  ws.addEventListener("error", () => ws.close());
}

// ─── Bootstrap ──────────────────────────────────────────────────────────
async function loadInitial() {
  try {
    const r = await api("GET", "/api/cameras");
    (r.data || []).forEach(renderStatus);
  } catch (err) {
    toast("error", `Failed to load cameras: ${err.message}`);
  }
  try {
    const r = await api("GET", "/api/atem/status");
    if (r.data) updateAtemIndicator(r.data);
  } catch { }
  refreshGlobalState();
}

loadInitial();
connectWS();
