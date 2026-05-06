from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gopro_mgmt.config import load_config
from gopro_mgmt.config_store import ConfigStore
from gopro_mgmt.schemas import AppConfig, CameraConfig


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "server:\n  host: 127.0.0.1\n  port: 8000\npoll_interval_sec: 2\ncameras:\n"
        "  - id: cam-a\n    name: Cam A\n    target: '1111'\n    mode: ble\n",
        encoding="utf-8",
    )
    return p


async def test_save_round_trip(cfg: Path):
    original = load_config(cfg)
    store = ConfigStore(cfg)

    updated = original.model_copy(update={
        "cameras": original.cameras + [CameraConfig(id="cam-b", name="Cam B", target="ABCD", mode="ble+wifi")]
    })
    await store.save(updated)

    reloaded = load_config(cfg)
    assert {c.id for c in reloaded.cameras} == {"cam-a", "cam-b"}
    cam_b = next(c for c in reloaded.cameras if c.id == "cam-b")
    assert cam_b.target == "ABCD"
    assert cam_b.mode == "ble+wifi"


async def test_save_atomic_no_temp_left_behind(cfg: Path):
    store = ConfigStore(cfg)
    await store.save(AppConfig())
    leftovers = [p for p in cfg.parent.iterdir() if p.name.startswith(".config.yaml.")]
    assert leftovers == [], f"temp file leaked: {leftovers}"


async def test_save_creates_parent(tmp_path: Path):
    target = tmp_path / "nested" / "dir" / "config.yaml"
    store = ConfigStore(target)
    await store.save(AppConfig())
    assert target.exists()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["cameras"] == []


async def test_concurrent_saves_serialize(cfg: Path):
    import asyncio

    store = ConfigStore(cfg)
    base = load_config(cfg)
    targets = [
        base.model_copy(update={"cameras": [CameraConfig(id=f"cam-{i}", name=f"#{i}", target=f"{i:04d}")]})
        for i in range(5)
    ]
    await asyncio.gather(*[store.save(t) for t in targets])

    final = load_config(cfg)
    # one of the writes won — file must contain a valid camera, not corruption
    assert len(final.cameras) == 1
    assert final.cameras[0].id.startswith("cam-")
