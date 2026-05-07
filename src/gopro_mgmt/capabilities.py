"""Per-model capability tables for GoPro cameras.

Used as a fallback when the BLE ``get_capabilities()`` query fails or returns
an empty list (older firmware, transient SDK error). Keys are lowercase
substrings matched against the model name returned by ``get_hardware_info()``
(e.g. ``"HERO12 Black"`` → matches ``"hero12"``).

Sources:
  • GoPro official spec sheets
  • Open GoPro BLE spec tables

When adding a new model: add a new dict entry with the same shape, and verify
it lines up with the friendly keys defined in ``settings_map.py``.
"""
from __future__ import annotations

_MODEL_CAPS: dict[str, dict[str, list[str]]] = {
    "hero12": {
        "resolutions": ["5.3K", "5.3K 4:3", "4K", "4K 4:3", "4K 8:7", "2.7K 4:3", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level", "HyperView", "Linear+Lock"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost", "AutoBoost"],
    },
    "hero11": {
        "resolutions": ["5.3K", "5.3K 4:3", "5.3K 8:7", "4K", "4K 4:3", "4K 8:7", "2.7K 4:3", "1440p", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level", "HyperView", "Linear+Lock"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost", "AutoBoost"],
    },
    "hero10": {
        "resolutions": ["5.3K", "5.3K 4:3", "4K", "4K 4:3", "2.7K 4:3", "1440p", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost"],
    },
    "hero9": {
        "resolutions": ["5K", "5K 4:3", "4K", "4K 4:3", "2.7K", "2.7K 4:3", "1440p", "1080p", "720p"],
        "fps":         ["240", "120", "60", "50", "30", "25", "24"],
        "lenses":      ["Wide", "SuperView", "Linear", "Linear+Level"],
        "hypersmooth": ["Off", "Low", "Standard", "High", "Boost"],
    },
}


def _model_caps_for(model: str | None) -> dict[str, list[str]] | None:
    """Return the capability table for a known model, or None if not recognised."""
    if not model:
        return None
    lower = model.lower()
    for key, caps in _MODEL_CAPS.items():
        if key in lower:
            return caps
    return None
