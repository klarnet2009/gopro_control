"""WebSocket integration tests.

The /ws endpoint is the only push channel — frontend cards rely on it for
status, camera_added/removed/updated, scan_result, atem_status, atem_event.
These tests cover the contract the frontend depends on.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gopro_mgmt.api.app import create_app
from gopro_mgmt.api.ws import WSBroadcaster


@pytest.fixture
def ws_client(app_config, driver_factory, fake_scan):
    app = create_app(app_config, driver_factory=driver_factory, scan_fn=fake_scan)
    with TestClient(app) as c:
        yield c


def test_ws_hello_includes_all_configured_cameras(ws_client):
    with ws_client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "hello"
        ids = {c["id"] for c in msg["payload"]}
        assert ids == {"cam-a", "cam-b"}


def test_ws_hello_reports_initial_disconnected_state(ws_client):
    with ws_client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        for cam in msg["payload"]:
            assert cam["connection"] == "disconnected"


def test_ws_receives_camera_added_broadcast(ws_client):
    """Adding a camera via REST while a WS client is connected must push to it."""
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # drain hello

        body = {"id": "cam-x", "name": "Cam X", "target": "9999", "mode": "ble"}
        r = ws_client.post("/api/cameras", json=body)
        assert r.status_code == 201

        msg = ws.receive_json()
        assert msg["type"] == "camera_added"
        assert msg["payload"]["id"] == "cam-x"


def test_ws_receives_camera_removed_broadcast(ws_client):
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # drain hello

        r = ws_client.delete("/api/cameras/cam-a")
        assert r.status_code == 200

        msg = ws.receive_json()
        assert msg["type"] == "camera_removed"
        assert msg["payload"]["id"] == "cam-a"


def test_ws_receives_camera_updated_broadcast(ws_client):
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # drain hello

        r = ws_client.patch("/api/cameras/cam-a", json={"name": "Renamed"})
        assert r.status_code == 200

        msg = ws.receive_json()
        assert msg["type"] == "camera_updated"
        assert msg["payload"]["name"] == "Renamed"


def test_ws_disconnect_removes_client_from_broadcaster(ws_client):
    """After the client closes the socket, broadcaster.size must drop to 0."""
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # drain hello
        # The starlette TestClient does not run the broadcaster's set
        # mutation synchronously, so we confirm the registration happened
        # while still inside the with-block...
        assert len(ws_client.app.state.broadcaster._clients) == 1
    # ...and dropped after exit.
    assert len(ws_client.app.state.broadcaster._clients) == 0


# ── WSBroadcaster unit tests (no FastAPI required) ────────────────────────


class _FakeWS:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise ConnectionError("client gone")
        self.sent.append(message)


async def test_broadcaster_sends_to_all_clients():
    bus = WSBroadcaster()
    a, b = _FakeWS(), _FakeWS()
    await bus.add(a)
    await bus.add(b)
    await bus.broadcast({"type": "status", "payload": {"id": "x"}})
    assert a.sent == b.sent == [{"type": "status", "payload": {"id": "x"}}]


async def test_broadcaster_drops_clients_that_raise():
    bus = WSBroadcaster()
    good = _FakeWS()
    bad = _FakeWS(fail=True)
    await bus.add(good)
    await bus.add(bad)
    await bus.broadcast({"type": "status", "payload": {}})
    # The bad client is removed; good one still present and got the message.
    assert good.sent
    assert bad not in bus._clients
    assert good in bus._clients


async def test_broadcaster_remove_is_idempotent():
    bus = WSBroadcaster()
    ws = _FakeWS()
    await bus.add(ws)
    await bus.remove(ws)
    await bus.remove(ws)  # second remove must not raise
    assert ws not in bus._clients
