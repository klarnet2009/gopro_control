from __future__ import annotations

from pathlib import Path

import yaml

from .schemas import AppConfig


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)
