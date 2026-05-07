"""Friendly-key ↔ open_gopro enum mappings + status-value validation.

Pure data + pure functions. Lives outside driver.py so:
  • the manager can validate user input without touching the SDK adapter
  • test fixtures don't drag in 1000+ lines of transport plumbing
  • adding support for a new resolution/fps is a single-file change

Add a new entry to the forward map and the reverse map is regenerated
automatically. ``_LENS_LABELS`` / ``_HYPERSMOOTH_LABELS`` go forward (enum →
label); the reverse maps are mostly used by ``set_video_settings`` to translate
labels back into SDK enum names.
"""
from __future__ import annotations

from typing import Any

# Forward maps: friendly key sent by the UI → SDK enum attribute name.
_RESOLUTION_MAP: dict[str, str] = {
    # ── 5.3K (Hero 11 / Hero 12) ───────────────────────────────────────
    "5.3K":         "NUM_5_3K",
    "5.3K 4:3":     "NUM_5_3K_4_3",
    "5.3K 8:7":     "NUM_5_3K_8_7",
    # ── 4K ─────────────────────────────────────────────────────────────
    "4K":           "NUM_4K",
    "4K 4:3":       "NUM_4K_4_3",
    "4K 8:7":       "NUM_4K_8_7",
    # ── 2.7K ───────────────────────────────────────────────────────────
    "2.7K":         "NUM_2_7K",
    "2.7K 4:3":     "NUM_2_7K_4_3",
    # ── 1080p / 1440p / 720p ───────────────────────────────────────────
    "1440p":        "NUM_1440",
    "1080p":        "NUM_1080",
    "720p":         "NUM_720",
}
_FPS_MAP: dict[str, str] = {
    "240": "NUM_240_0",
    "120": "NUM_120_0",
    "100": "NUM_100_0",
    "60":  "NUM_60_0",
    "50":  "NUM_50_0",
    "30":  "NUM_30_0",
    "25":  "NUM_25_0",
    "24":  "NUM_24_0",
}

# Reverse maps: enum name → friendly key (decoding camera responses)
_RESOLUTION_REVERSE: dict[str, str] = {v: k for k, v in _RESOLUTION_MAP.items()}
_FPS_REVERSE: dict[str, str] = {v: k for k, v in _FPS_MAP.items()}

# Lens / stabilization display labels
_LENS_LABELS: dict[str, str] = {
    "WIDE":                    "Wide",
    "NARROW":                  "Narrow",
    "SUPERVIEW":               "SuperView",
    "LINEAR":                  "Linear",
    "MAX_SUPERVIEW":           "Max SuperView",
    "LINEAR_HORIZON_LEVELING": "Linear+Level",
    "HYPERVIEW":               "HyperView",
    "LINEAR_HORIZON_LOCK":     "Linear+Lock",
    "MAX_HYPERVIEW":           "Max HyperView",
    "ULTRA_SUPERVIEW":         "Ultra SuperView",
    "ULTRA_WIDE":              "Ultra Wide",
    "ULTRA_LINEAR":            "Ultra Linear",
    "ULTRA_HYPERVIEW":         "Ultra HyperView",
}
_HYPERSMOOTH_LABELS: dict[str, str] = {
    "OFF":        "Off",
    "LOW":        "Low",
    "STANDARD":   "Standard",
    "HIGH":       "High",
    "BOOST":      "Boost",
    "AUTO_BOOST": "AutoBoost",
}

_LENS_REVERSE = {v: k for k, v in _LENS_LABELS.items()}
_HYPERSMOOTH_REVERSE = {v: k for k, v in _HYPERSMOOTH_LABELS.items()}
_PRESET_GROUPS = {1000, 1001, 1002}


# ── Enum → friendly converters ───────────────────────────────────────────────


def _enum_to_resolution(val: Any) -> str | None:
    """Convert a VideoResolution enum value to our friendly key, or None."""
    name = val.name if hasattr(val, "name") else str(val)
    return _RESOLUTION_REVERSE.get(name)


def _enum_to_fps(val: Any) -> str | None:
    """Convert a VideoFPS enum value to our friendly key, or None."""
    name = val.name if hasattr(val, "name") else str(val)
    return _FPS_REVERSE.get(name)


def _enum_to_lens(val: Any) -> str | None:
    """Convert a VideoLens enum to a human-readable label."""
    name = val.name if hasattr(val, "name") else str(val)
    return _LENS_LABELS.get(name, name.replace("_", " ").title() if name else None)


def _enum_to_hypersmooth(val: Any) -> str | None:
    """Convert a Hypersmooth enum to a human-readable label."""
    name = val.name if hasattr(val, "name") else str(val)
    return _HYPERSMOOTH_LABELS.get(name, name.replace("_", " ").title() if name else None)


# ── Status field validation ──────────────────────────────────────────────────


def _valid_status_value(key: str, value: Any) -> Any | None:
    """Sanity-check telemetry from the camera before exposing it to the UI.

    Returns the coerced value when valid, ``None`` otherwise. Unknown keys
    pass through unchanged so callers can layer additional validation.
    """
    try:
        if key == "battery_percent":
            value = int(value)
            return value if 0 <= value <= 100 else None
        if key == "sd_remaining_sec":
            value = int(value)
            return value if 0 <= value <= 7 * 24 * 3600 else None
        if key == "preset_group":
            value = int(value)
            return value if value in _PRESET_GROUPS else None
    except (TypeError, ValueError):
        return None
    return value
