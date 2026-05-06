"""Read-only access to the open_gopro TinyDB credential store.

open_gopro persists per-camera COHN (HTTP-over-home-Wi-Fi) credentials in
``cohn_db.json`` at the project root. The SDK is the only writer; this module
just decodes the on-disk shape so the manager can hydrate
``CameraStatus.cohn_provisioned`` / ``cohn_ip`` without dragging TinyDB into
hot paths.

Layout::

    {
      "_default": {
        "1": {"serial": "ABCD", "credentials": {"ip_address": "...", ...}},
        "2": {...}
      }
    }

The ``serial`` field is the full camera serial; matching uses ``endswith`` of
the four-char target identifier (case-insensitive).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .driver import COHN_DB_PATH

log = logging.getLogger(__name__)


def read_cohn_db_for(target: str, db_path: Path | None = None) -> dict | None:
    """Return credentials for ``target`` if the on-disk DB has them, else None.

    ``db_path`` is for tests; production callers leave it at the default. Any
    parse / IO error is swallowed and reported at debug level — a missing or
    corrupt DB is normal during fresh installs and must never raise.
    """
    path = db_path if db_path is not None else COHN_DB_PATH
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        data = json.loads(raw)
        records = (data.get("_default") or {}) if isinstance(data, dict) else {}
        target_low = str(target).lower()
        for rec in records.values():
            if not isinstance(rec, dict):
                continue
            serial = str(rec.get("serial", "")).lower()
            if serial.endswith(target_low):
                creds = rec.get("credentials")
                if isinstance(creds, dict):
                    return creds
                return rec
    except Exception as exc:
        log.debug("could not read %s for target %s: %s", path, target, exc)
    return None
