"""Monitor an ATEM switcher for recording events and trigger GoPro recording.

Runs the synchronous pyatem loop in a background daemon thread. Bridges events
back into the asyncio loop via run_coroutine_threadsafe so the manager and
broadcaster can be used without thread-safety concerns.

Discovery:
- If atem_host is given: connect directly (no mDNS needed).
- If atem_host is None: auto-discover via mDNS (_blackmagic._tcp.local.).
  Requires the zeroconf package (already installed as a transitive dependency).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

_RECONNECT_DELAY = 5.0
_FALSE_STOP_CONFIRM_SEC = 2.0


class AtemWatcher:
    def __init__(
        self,
        manager,
        broadcaster,
        host: str | None = None,
    ) -> None:
        self._host = host
        self._manager = manager
        self._broadcaster = broadcaster
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._recording = False
        self._recording_initialized = False
        self._last_recording_update = 0.0
        self._auto_enabled = True
        self._effective_host: str | None = host
        self._device_name: str | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=30)
        self._events_lock = threading.Lock()

    # ── public ───────────────────────────────────────────────────────────────

    def set_auto(self, enabled: bool) -> None:
        self._auto_enabled = enabled
        log.info("ATEM auto-trigger %s", "enabled" if enabled else "disabled")
        self._record_event(
            "auto",
            "auto-trigger enabled" if enabled else "auto-trigger disabled",
            level="ok" if enabled else "warn",
        )
        self._schedule(self._broadcast_status())

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atem-watcher", daemon=True)
        self._thread.start()
        log.info("ATEM watcher started (host=%s)", self._host or "auto-discover")
        self._record_event("watcher", f"started ({self._host or 'auto-discover'})")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # loop() blocks up to 5 s (socket timeout); give it a little extra
            self._thread.join(timeout=12)
            self._thread = None
        log.info("ATEM watcher stopped")
        self._record_event("watcher", "stopped", level="warn")

    @property
    def status(self) -> dict[str, Any]:
        return {
            "host": self._effective_host,
            "name": self._device_name,
            "connected": self._connected,
            "recording": self._recording,
            "auto_enabled": self._auto_enabled,
            "state": self._state_label(),
            "last_event": self.events[-1] if self.events else None,
            "events": self.events,
        }

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return list(self._events)

    # ── internals ────────────────────────────────────────────────────────────

    def _schedule(self, coro) -> None:
        if self._loop is None or self._loop.is_closed():
            log.debug("dropping ATEM scheduled coroutine because loop is unavailable")
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_future_result)

    @staticmethod
    def _log_future_result(future) -> None:
        try:
            future.result()
        except Exception:
            log.exception("scheduled ATEM action failed")

    def _state_label(self) -> str:
        if not self._connected:
            return "searching"
        if self._recording:
            return "recording"
        return "connected"

    def _record_event(self, kind: str, message: str, *, level: str = "info") -> dict[str, Any]:
        event = {
            "ts": time.time(),
            "kind": kind,
            "level": level,
            "message": message,
            "host": self._effective_host,
            "connected": self._connected,
            "recording": self._recording,
            "auto_enabled": self._auto_enabled,
        }
        with self._events_lock:
            self._events.append(event)
        self._schedule(self._broadcast_event(event))
        return event

    def _run(self) -> None:
        if self._host:
            self._run_with_host(self._host)
        else:
            self._discover_and_run()

    def _discover_and_run(self) -> None:
        try:
            from pyatem.locate import listen, stop as locate_stop
        except ImportError:
            log.error(
                "zeroconf is not installed — cannot auto-discover ATEM. "
                "Set atem_host in config.yaml instead."
            )
            return

        log.info("looking for ATEM via mDNS (_blackmagic._tcp.local.)…")
        while not self._stop.is_set():
            found = threading.Event()
            found_host: list[str] = []

            def on_add(name, subtitle, protocol, address):
                if protocol == "udp" and not found.is_set():
                    self._device_name = name
                    self._effective_host = str(address[0])
                    found_host.append(self._effective_host)
                    log.info("ATEM discovered: %s at %s", name, self._effective_host)
                    self._record_event("discover", f"{name} at {self._effective_host}", level="ok")
                    found.set()

            listen(on_add)

            # Wait until discovered, checking stop every second
            while not self._stop.is_set():
                if found.wait(timeout=1.0):
                    break

            locate_stop()

            if found_host and not self._stop.is_set():
                self._run_with_host(found_host[0])
                # After disconnect, loop back and re-discover

    def _run_with_host(self, host: str) -> None:
        while not self._stop.is_set():
            try:
                self._connect_and_loop(host)
            except Exception:
                log.exception("ATEM connection error at %s", host)
                self._record_event("error", f"connection error at {host}", level="err")
            # Reset state and broadcast before sleeping
            self._connected = False
            self._recording = False
            self._recording_initialized = False
            self._record_event("connection", "offline", level="err")
            self._schedule(self._broadcast_status())
            self._stop.wait(_RECONNECT_DELAY)

    def _connect_and_loop(self, host: str) -> None:
        from pyatem.protocol import AtemProtocol

        log.info("connecting to ATEM at %s…", host)
        switcher = AtemProtocol(host)
        was_disconnected = threading.Event()

        def on_connected():
            self._connected = True
            log.info("ATEM connected (%s)", host)
            self._record_event("connection", f"connected {host}", level="ok")
            self._reconcile_recording_state(switcher, source="connect")
            self._schedule(self._broadcast_status())

        def on_disconnected():
            self._connected = False
            self._recording = False
            self._recording_initialized = False
            log.warning("ATEM disconnected from %s", host)
            self._record_event("connection", f"disconnected {host}", level="err")
            self._schedule(self._broadcast_status())
            was_disconnected.set()

        def on_recording_status(field):
            self._set_recording(bool(field.is_recording), source="recording-status")

        def on_recording_disk(field):
            # Disk status is a useful cross-check: one disk can be idle while
            # another is actively recording, so only promote True from here.
            if getattr(field, "is_recording", False):
                self._set_recording(True, source="recording-disk")

        switcher.on("connected", on_connected)
        switcher.on("disconnected", on_disconnected)
        switcher.on("change:recording-status", on_recording_status)
        switcher.on("change:recording-disk:*", on_recording_disk)
        switcher.connect()

        while not self._stop.is_set() and not was_disconnected.is_set():
            switcher.loop()
            self._reconcile_recording_state(switcher, source="mixerstate")

    def _reconcile_recording_state(self, switcher, *, source: str) -> None:
        status = switcher.mixerstate.get("recording-status")
        if status is not None and hasattr(status, "is_recording"):
            self._set_recording(bool(status.is_recording), source=source)
            return

        disks = switcher.mixerstate.get("recording-disk")
        if isinstance(disks, dict):
            if any(getattr(disk, "is_recording", False) for disk in disks.values()):
                self._set_recording(True, source=f"{source}:disk")

    def _set_recording(self, new_val: bool, *, source: str) -> None:
        previous = self._recording if self._recording_initialized else None
        self._recording = new_val
        self._recording_initialized = True
        self._last_recording_update = time.monotonic()

        if previous == new_val:
            return

        log.info("ATEM recording → %s (source=%s auto=%s)", new_val, source, self._auto_enabled)
        self._record_event(
            "recording",
            f"REC {'ON' if new_val else 'OFF'} via {source}",
            level="rec" if new_val else "warn",
        )
        self._schedule(self._broadcast_status())
        if not self._auto_enabled:
            self._record_event("command", "auto disabled; no camera command", level="warn")
            return
        if new_val:
            self._record_event("command", "ROLL ALL scheduled", level="rec")
            self._schedule(self._manager.start_all())
        elif previous is True:
            self._record_event("command", f"CUT ALL scheduled (confirm in {_FALSE_STOP_CONFIRM_SEC}s)", level="warn")
            self._schedule(self._confirmed_stop_all())

    async def _confirmed_stop_all(self) -> None:
        # Do not immediately stop GoPros on a single false sample. The ATEM UDP
        # feed can briefly replay stale RTMS=false while the recorder is still
        # running; requiring a stable false state avoids accidental cuts.
        started = self._last_recording_update
        await asyncio.sleep(_FALSE_STOP_CONFIRM_SEC)
        if self._recording or self._last_recording_update != started:
            log.info("ATEM stop ignored because recording state changed during confirmation")
            return
        await self._manager.stop_all()

    async def _broadcast_status(self) -> None:
        await self._broadcaster.broadcast({
            "type": "atem_status",
            "payload": {"enabled": True, **self.status},
        })

    async def _broadcast_event(self, event: dict[str, Any]) -> None:
        await self._broadcaster.broadcast({
            "type": "atem_event",
            "payload": event,
        })
