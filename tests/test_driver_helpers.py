"""Pure-function tests for driver.py helpers (no SDK / BLE / network).

These cover the settings-map machinery and small response helpers that the
manager relies on. They are deliberately isolated so a future split into
``settings_map.py`` / ``capabilities.py`` doesn't lose coverage.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gopro_mgmt import driver as drv

# ── Settings maps ────────────────────────────────────────────────────────────


def test_resolution_reverse_map_inverts_forward_map():
    for friendly, enum_name in drv._RESOLUTION_MAP.items():
        assert drv._RESOLUTION_REVERSE[enum_name] == friendly


def test_fps_reverse_map_inverts_forward_map():
    for friendly, enum_name in drv._FPS_MAP.items():
        assert drv._FPS_REVERSE[enum_name] == friendly


def test_lens_reverse_map_inverts_forward_map():
    for enum_name, label in drv._LENS_LABELS.items():
        assert drv._LENS_REVERSE[label] == enum_name


def test_hypersmooth_reverse_map_inverts_forward_map():
    for enum_name, label in drv._HYPERSMOOTH_LABELS.items():
        assert drv._HYPERSMOOTH_REVERSE[label] == enum_name


# ── Enum-to-friendly converters ─────────────────────────────────────────────


def _enum(name: str):
    """Mimic an enum value: it has .name and falls back to str()."""
    return SimpleNamespace(name=name)


def test_enum_to_resolution_known_value():
    assert drv._enum_to_resolution(_enum("NUM_4K")) == "4K"


def test_enum_to_resolution_unknown_returns_none():
    assert drv._enum_to_resolution(_enum("NUM_8K_FUTURE")) is None


def test_enum_to_resolution_handles_bare_string_via_str_fallback():
    # When a non-enum is passed, the converter falls back to str(val); a string
    # that already matches an enum name resolves the same as the enum form.
    assert drv._enum_to_resolution("NUM_4K") == "4K"


def test_enum_to_resolution_handles_unknown_string_returns_none():
    assert drv._enum_to_resolution("NUM_8K_FUTURE") is None


def test_enum_to_fps_known_value():
    assert drv._enum_to_fps(_enum("NUM_60_0")) == "60"


def test_enum_to_lens_known_label():
    assert drv._enum_to_lens(_enum("WIDE")) == "Wide"


def test_enum_to_lens_unknown_titlecases_fallback():
    # Unknown enum names get a best-effort title-case so the UI never sees
    # raw SDK identifiers.
    assert drv._enum_to_lens(_enum("CUSTOM_FOO")) == "Custom Foo"


def test_enum_to_hypersmooth_known_label():
    assert drv._enum_to_hypersmooth(_enum("BOOST")) == "Boost"


def test_enum_to_hypersmooth_unknown_titlecases_fallback():
    assert drv._enum_to_hypersmooth(_enum("EXPERIMENTAL_X")) == "Experimental X"


# ── Status validation ───────────────────────────────────────────────────────


def test_valid_status_value_battery_in_range():
    assert drv._valid_status_value("battery_percent", 75) == 75


@pytest.mark.parametrize("bad", [-1, 101, 200])
def test_valid_status_value_battery_out_of_range_returns_none(bad):
    assert drv._valid_status_value("battery_percent", bad) is None


def test_valid_status_value_sd_remaining_accepts_int_string():
    assert drv._valid_status_value("sd_remaining_sec", "3600") == 3600


def test_valid_status_value_sd_remaining_above_week_rejected():
    # 8 days exceeds the 7-day cap → sentinel that the camera sometimes spits
    # out before the SD card is mounted.
    eight_days = 8 * 24 * 3600
    assert drv._valid_status_value("sd_remaining_sec", eight_days) is None


def test_valid_status_value_preset_group_known():
    assert drv._valid_status_value("preset_group", 1000) == 1000


def test_valid_status_value_preset_group_unknown_rejected():
    assert drv._valid_status_value("preset_group", 9999) is None


def test_valid_status_value_unknown_key_passes_through():
    # Keys not specifically handled fall through unchanged. This documents the
    # contract: only the listed keys are validated.
    assert drv._valid_status_value("unrelated", "anything") == "anything"


def test_valid_status_value_non_numeric_battery_returns_none():
    assert drv._valid_status_value("battery_percent", "fifty") is None


# ── Model capability lookup ─────────────────────────────────────────────────


def test_model_caps_for_hero12_matches_lowercase_substring():
    caps = drv._model_caps_for("HERO12 Black")
    assert caps is not None
    assert "5.3K" in caps["resolutions"]
    assert "240" in caps["fps"]


def test_model_caps_for_hero9_distinct_from_hero11():
    h9 = drv._model_caps_for("HERO9 Black")
    h11 = drv._model_caps_for("HERO11 Black")
    assert h9 is not None and h11 is not None
    # Hero 11 supports 8:7 aspect; Hero 9 does not.
    assert "5.3K 8:7" in h11["resolutions"]
    assert "5.3K 8:7" not in h9["resolutions"]


def test_model_caps_for_unknown_returns_none():
    assert drv._model_caps_for("HERO20 Black") is None


def test_model_caps_for_none_returns_none():
    assert drv._model_caps_for(None) is None


def test_model_caps_for_empty_string_returns_none():
    assert drv._model_caps_for("") is None


# ── _unwrap / _check_resp ───────────────────────────────────────────────────


def test_unwrap_returns_data_attribute_when_present():
    resp = SimpleNamespace(data={"battery": 80}, ok=True)
    assert drv._unwrap(resp) == {"battery": 80}


def test_unwrap_returns_input_when_data_missing():
    plain = {"already": "unwrapped"}
    assert drv._unwrap(plain) is plain


def test_check_resp_passes_when_ok_true():
    drv._check_resp(SimpleNamespace(ok=True))


def test_check_resp_passes_when_ok_attr_missing():
    # Backwards-compat: SDK responses without an ok attribute are treated
    # as success rather than crashing the caller.
    drv._check_resp(SimpleNamespace(data="x"))


def test_check_resp_raises_on_failure_with_setting_id():
    resp = SimpleNamespace(ok=False, id="SettingId.VIDEO_RESOLUTION")
    with pytest.raises(RuntimeError, match="Video Resolution"):
        drv._check_resp(resp)


def test_check_resp_raises_generic_when_no_setting_id():
    resp = SimpleNamespace(ok=False)
    with pytest.raises(RuntimeError, match="Camera rejected command"):
        drv._check_resp(resp)


# ── Observer health ─────────────────────────────────────────────────────────


def test_observer_health_empty_for_uninitialised_driver():
    """Driver with no observers running yet reports 0/0 — used by COHN mode."""
    d = drv.WirelessGoProDriver("AAAA", mode="cohn")
    assert d.get_observer_health() == {"alive": 0, "total": 0}


def test_observer_health_counts_alive_and_retrying_states():
    """Observers in 'alive' or 'retrying' count as live; 'starting'/'dead' do not."""
    d = drv.WirelessGoProDriver("AAAA", mode="ble")
    d._observer_status.update(
        {
            "battery_percent": "alive",
            "fps": "alive",
            "lens": "retrying",
            "preset_group": "starting",
            "resolution": "dead",
        }
    )
    assert d.get_observer_health() == {"alive": 3, "total": 5}
