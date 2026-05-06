"""Atomic YAML persistence for the camera list.

Reads happen once at startup. Every mutation goes through `save()`, which
serializes the whole `AppConfig` and replaces the file atomically (temp
file + os.replace).
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import yaml

from .schemas import AppConfig

log = logging.getLogger(__name__)


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def save(self, config: AppConfig) -> None:
        async with self._lock:
            payload = config.model_dump(mode="python")
            await asyncio.to_thread(self._write_atomic, payload)
            log.info("config persisted to %s (%d cameras)", self._path, len(config.cameras))

    def _write_atomic(self, payload: dict) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(directory),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
