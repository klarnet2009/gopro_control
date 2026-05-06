"""Tests for config.load_config + schema-level validation invariants.

These guard the boundary where YAML meets the type system: a malformed config
should fail loudly at startup, not silently produce a half-initialised manager.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gopro_mgmt.cohn_db import read_cohn_db_for
from gopro_mgmt.config import load_config
from gopro_mgmt.schemas import (
    AppConfig,
    CameraConfig,
    CameraCreate,
    CohnProvisionPayload,
    TimingConfig,
)

# ── load_config ──────────────────────────────────────────────────────────────


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_load_config_empty_file_yields_defaults(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.cameras == []
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8000


def test_load_config_round_trips_camera_list(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "cameras": [
                    {"id": "cam-a", "name": "Cam A", "target": "1111", "mode": "ble"},
                    {"id": "cam-b", "name": "Cam B", "target": "2222", "mode": "cohn"},
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert {c.id for c in cfg.cameras} == {"cam-a", "cam-b"}
    assert next(c for c in cfg.cameras if c.id == "cam-b").mode == "cohn"


def test_load_config_rejects_invalid_target(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump({"cameras": [{"id": "cam-a", "name": "Cam A", "target": "TOO_LONG"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_load_config_rejects_invalid_mode(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {"cameras": [{"id": "cam-a", "name": "Cam A", "target": "1111", "mode": "bluetooth"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_load_config_picks_up_timing_overrides(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump({"timing": {"stop_grace_sec": 12.5, "ble_cmd_timeout_sec": 4.0}}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.timing.stop_grace_sec == 12.5
    assert cfg.timing.ble_cmd_timeout_sec == 4.0
    # Non-overridden fields keep defaults.
    assert cfg.timing.post_stop_recovery_sec == TimingConfig().post_stop_recovery_sec


# ── TimingConfig defaults ────────────────────────────────────────────────────


def test_timing_config_defaults_match_legacy_constants():
    """If these change, the corresponding STOP_GRACE/POST_STOP_RECOVERY etc.
    re-exports in manager.py must change too — they are used by tests as
    canonical reference values."""
    t = TimingConfig()
    assert t.stop_grace_sec == 8.0
    assert t.post_stop_recovery_sec == 4.0
    assert t.rssi_poll_interval_sec == 15.0
    assert t.ble_cmd_timeout_sec == 8.0
    assert t.ble_detail_timeout_sec == 1.5
    assert t.http_cmd_timeout_sec == 12.0
    assert t.http_shutter_timeout_sec == 15.0
    assert t.cohn_keepalive_sec == 25.0


# ── ID / TARGET pattern validation ───────────────────────────────────────────


@pytest.mark.parametrize("bad_id", ["UPPER", "with space", "-leading", "way-too-long-" + "x" * 30])
def test_camera_config_rejects_invalid_id(bad_id: str):
    with pytest.raises(ValidationError):
        CameraConfig(id=bad_id, name="x", target="1111")


@pytest.mark.parametrize("good_id", ["a", "cam-1", "cam-with-dashes-09"])
def test_camera_config_accepts_lowercase_dashed_ids(good_id: str):
    cfg = CameraConfig(id=good_id, name="x", target="1111")
    assert cfg.id == good_id


@pytest.mark.parametrize("bad_target", ["abc", "ABCDE", "12-3", "***x"])
def test_camera_config_rejects_invalid_target(bad_target: str):
    with pytest.raises(ValidationError):
        CameraConfig(id="cam", name="x", target=bad_target)


@pytest.mark.parametrize("good_target", ["1234", "ABCD", "aB1c"])
def test_camera_config_accepts_4char_alnum_target(good_target: str):
    cfg = CameraConfig(id="cam", name="x", target=good_target)
    assert cfg.target == good_target


def test_camera_create_rejects_empty_name():
    with pytest.raises(ValidationError):
        CameraCreate(id="cam", name="", target="1111")


def test_cohn_provision_rejects_short_password():
    with pytest.raises(ValidationError):
        CohnProvisionPayload(ssid="MyWifi", password="short")


def test_cohn_provision_rejects_empty_ssid():
    with pytest.raises(ValidationError):
        CohnProvisionPayload(ssid="", password="longerthan8")


# ── AppConfig defaults ───────────────────────────────────────────────────────


def test_appconfig_default_has_empty_cameras():
    cfg = AppConfig()
    assert cfg.cameras == []
    assert cfg.atem_host is None
    assert cfg.timing == TimingConfig()


# ── cohn_db.read_cohn_db_for ────────────────────────────────────────────────


def test_cohn_db_returns_none_when_file_missing(tmp_path: Path):
    assert read_cohn_db_for("ABCD", db_path=tmp_path / "no_such.json") is None


def test_cohn_db_returns_none_for_empty_file(tmp_path: Path):
    p = tmp_path / "cohn_db.json"
    p.write_text("", encoding="utf-8")
    assert read_cohn_db_for("ABCD", db_path=p) is None


def test_cohn_db_returns_credentials_when_serial_endswith_target(tmp_path: Path):
    p = tmp_path / "cohn_db.json"
    p.write_text(
        '{"_default": {"1": {"serial": "C123ABCD", "credentials": {"ip_address": "10.0.0.1"}}}}',
        encoding="utf-8",
    )
    creds = read_cohn_db_for("ABCD", db_path=p)
    assert creds == {"ip_address": "10.0.0.1"}


def test_cohn_db_match_is_case_insensitive(tmp_path: Path):
    p = tmp_path / "cohn_db.json"
    p.write_text(
        '{"_default": {"1": {"serial": "C123abcd", "credentials": {"ip_address": "10.0.0.1"}}}}',
        encoding="utf-8",
    )
    assert read_cohn_db_for("ABCD", db_path=p) == {"ip_address": "10.0.0.1"}


def test_cohn_db_returns_none_when_no_serial_matches(tmp_path: Path):
    p = tmp_path / "cohn_db.json"
    p.write_text(
        '{"_default": {"1": {"serial": "C123XXXX", "credentials": {"ip_address": "10.0.0.1"}}}}',
        encoding="utf-8",
    )
    assert read_cohn_db_for("ABCD", db_path=p) is None


def test_cohn_db_returns_none_on_corrupt_json(tmp_path: Path):
    p = tmp_path / "cohn_db.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert read_cohn_db_for("ABCD", db_path=p) is None
