from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from gopro_mgmt.api.app import create_app
from gopro_mgmt.config_store import ConfigStore


@pytest.fixture
def client(app_config, driver_factory, fake_scan):
    app = create_app(app_config, driver_factory=driver_factory, scan_fn=fake_scan)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_store(tmp_path: Path, app_config, driver_factory, fake_scan):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(app_config.model_dump(mode="python"), sort_keys=False),
        encoding="utf-8",
    )
    store = ConfigStore(cfg_path)
    app = create_app(
        app_config,
        driver_factory=driver_factory,
        config_store=store,
        scan_fn=fake_scan,
    )
    with TestClient(app) as c:
        yield c, cfg_path


def test_list_cameras(client):
    r = client.get("/api/cameras")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert {c["id"] for c in body["data"]} == {"cam-a", "cam-b"}
    assert all(c["connection"] == "disconnected" for c in body["data"])


def test_connect_then_start_then_stop(client):
    r = client.post("/api/cameras/cam-a/connect")
    assert r.json()["data"]["connection"] == "connected"

    r = client.post("/api/cameras/cam-a/record/start")
    assert r.json()["ok"] is True
    assert r.json()["data"]["encoding"] is True

    r = client.post("/api/cameras/cam-a/record/stop")
    assert r.json()["data"]["encoding"] is False


def test_start_all_only_connected(client):
    client.post("/api/cameras/cam-a/connect")
    r = client.post("/api/cameras/record/start")
    body = r.json()
    assert body["ok"] is True
    assert [c["id"] for c in body["data"]] == ["cam-a"]
    assert body["data"][0]["encoding"] is True


def test_start_all_with_two_cameras(client):
    client.post("/api/cameras/cam-a/connect")
    client.post("/api/cameras/cam-b/connect")
    r = client.post("/api/cameras/record/start")
    body = r.json()
    ids = sorted(c["id"] for c in body["data"])
    assert ids == ["cam-a", "cam-b"]
    assert all(c["encoding"] is True for c in body["data"])

    r = client.post("/api/cameras/record/stop")
    assert all(c["encoding"] is False for c in r.json()["data"])


def test_unknown_camera_404(client):
    r = client.post("/api/cameras/ghost/connect")
    assert r.status_code == 404


def test_start_without_connect_returns_failure(client):
    r = client.post("/api/cameras/cam-a/record/start")
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "start_failed"


def test_status_endpoint(client):
    client.post("/api/cameras/cam-a/connect")
    r = client.get("/api/cameras/cam-a/status")
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["battery_percent"] == 87


# ---- CRUD endpoints ---------------------------------------------------------


def test_add_camera_201_and_appears_in_list(client):
    r = client.post("/api/cameras", json={
        "id": "cam-c", "name": "Cam C", "target": "ABCD", "mode": "ble+wifi",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["id"] == "cam-c"
    assert body["data"]["mode"] == "ble+wifi"

    listing = client.get("/api/cameras").json()
    ids = [c["id"] for c in listing["data"]]
    assert "cam-c" in ids


def test_add_duplicate_returns_409(client):
    r = client.post("/api/cameras", json={
        "id": "cam-a", "name": "Dup", "target": "0000",
    })
    assert r.status_code == 409


def test_add_invalid_id_returns_422(client):
    r = client.post("/api/cameras", json={
        "id": "Cam Bad!", "name": "x", "target": "1234",
    })
    assert r.status_code == 422


def test_add_invalid_target_returns_422(client):
    r = client.post("/api/cameras", json={
        "id": "cam-x", "name": "x", "target": "12",  # too short
    })
    assert r.status_code == 422


def test_patch_name_only(client):
    r = client.patch("/api/cameras/cam-a", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "Renamed"


def test_patch_target_auto_disconnects_via_route(client):
    client.post("/api/cameras/cam-a/connect")
    assert client.get("/api/cameras").json()["data"][0]["connection"] == "connected"
    r = client.patch("/api/cameras/cam-a", json={"target": "0000"})
    assert r.status_code == 200
    assert r.json()["data"]["connection"] == "disconnected"
    assert r.json()["data"]["target"] == "0000"


def test_patch_unknown_returns_404(client):
    r = client.patch("/api/cameras/ghost", json={"name": "x"})
    assert r.status_code == 404


def test_delete_removes_camera(client):
    r = client.delete("/api/cameras/cam-a")
    assert r.status_code == 200
    assert r.json()["data"]["id"] == "cam-a"
    listing = client.get("/api/cameras").json()
    assert all(c["id"] != "cam-a" for c in listing["data"])


def test_delete_unknown_returns_404(client):
    r = client.delete("/api/cameras/ghost")
    assert r.status_code == 404


# ---- Persistence ------------------------------------------------------------


def test_add_persists_to_config_yaml(client_with_store):
    client, cfg_path = client_with_store
    r = client.post("/api/cameras", json={
        "id": "cam-c", "name": "Cam C", "target": "AAAA", "mode": "ble",
    })
    assert r.status_code == 201

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in saved["cameras"]]
    assert "cam-c" in ids


def test_delete_persists_to_config_yaml(client_with_store):
    client, cfg_path = client_with_store
    r = client.delete("/api/cameras/cam-a")
    assert r.status_code == 200

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in saved["cameras"]]
    assert "cam-a" not in ids
    assert "cam-b" in ids


def test_patch_persists_to_config_yaml(client_with_store):
    client, cfg_path = client_with_store
    client.patch("/api/cameras/cam-a", json={"name": "Persisted"})

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cam_a = next(c for c in saved["cameras"] if c["id"] == "cam-a")
    assert cam_a["name"] == "Persisted"


# ---- Scan endpoint ----------------------------------------------------------


def test_scan_endpoint_returns_results(client):
    r = client.post("/api/scan")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [item["target"] for item in body["data"]] == ["AB12", "CD34"]
    assert body["data"][0]["rssi"] == -42


# ---- COHN provisioning routes -----------------------------------------------


@pytest.fixture
def _patch_provision_driver_routes(monkeypatch):
    from gopro_mgmt import manager as mgr_mod

    class _StubDriver:
        def __init__(self, target, *, mode="ble", **_kwargs):
            self.target = target
            self.mode = mode

        async def provision_cohn(self, ssid, password):
            return {
                "ip_address": "192.168.1.42",
                "username": "gopro",
                "has_certificate": True,
            }

    monkeypatch.setattr(mgr_mod, "WirelessGoProDriver", _StubDriver, raising=False)
    yield


def test_provision_cohn_route(client, _patch_provision_driver_routes):
    body = {"ssid": "MyWifi", "password": "longerthan8"}
    r = client.post("/api/cameras/cam-a/provision-cohn", json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["cohn_provisioned"] is True
    assert j["data"]["cohn_ip"] == "192.168.1.42"


def test_provision_cohn_password_too_short_returns_422(client):
    body = {"ssid": "MyWifi", "password": "short"}
    r = client.post("/api/cameras/cam-a/provision-cohn", json=body)
    assert r.status_code == 422


def test_provision_cohn_unknown_camera_returns_404(client, _patch_provision_driver_routes):
    body = {"ssid": "MyWifi", "password": "longerthan8"}
    r = client.post("/api/cameras/ghost/provision-cohn", json=body)
    assert r.status_code == 404
