"""Event-driven listener for macOS active-application changes.

The parent daemon runs ``uvicorn`` on its main thread, which leaves no
``CFRunLoop`` available to service ``NSWorkspace``'s distributed-notification
mach-port source. To keep the listener event-driven (no polling), we spawn
a small subprocess helper whose *main* thread runs ``NSRunLoop`` and
observes ``NSWorkspaceDidActivateApplicationNotification``. The helper
emits a JSON line per activation on stdout; this listener reads those
lines on a background thread and pushes a :class:`ContextEvent` to the bus.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

_HELPER_SCRIPT = Path(__file__).with_name("_mac_app_helper.py")


class MacAppListener:
    """Spawns the NSWorkspace helper subprocess and forwards activations."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._process is not None:
            return
        if not _HELPER_SCRIPT.exists():
            logger.error("mac_app_helper script not found at %s", _HELPER_SCRIPT)
            return
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self._process = subprocess.Popen(
                [sys.executable, str(_HELPER_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,
            )
        except Exception:
            logger.exception("Failed to spawn mac_app_helper subprocess")
            self._process = None
            return
        logger.info("mac_app_helper spawned (pid=%s)", self._process.pid)
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="MacAppListener-stdout", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="MacAppListener-stderr", daemon=True
        )
        self._stderr_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        proc = self._process
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:  # pragma: no cover
                logger.exception("Failed to terminate mac_app_helper")
        self._process = None

    def _read_stdout(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        for line in self._iter_lines(proc.stdout):
            if self._stop_event.is_set():
                return
            self._handle_line(line)
        logger.info("mac_app_helper stdout closed")

    def _read_stderr(self) -> None:
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        for line in self._iter_lines(proc.stderr):
            if self._stop_event.is_set():
                return
            logger.info("mac_app_helper: %s", line.strip())

    @staticmethod
    def _iter_lines(stream: IO[str]):
        while True:
            line = stream.readline()
            if not line:
                return
            yield line

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("mac_app_helper emitted non-JSON line: %r", line)
            return
        app_name = payload.get("app_name")
        bundle_id = payload.get("bundle_id")
        pid = payload.get("pid")
        logger.info(
            "NSWorkspace activation: app=%s bundle=%s pid=%s",
            app_name, bundle_id, pid,
        )
        event = ContextEvent(
            event_type="app_activated",
            app_name=app_name,
            window_title=None,
            metadata={"bundle_id": bundle_id, "pid": pid},
        )
        try:
            self._bus.push_threadsafe(event)
        except Exception:  # pragma: no cover - loop not bound or shutting down
            logger.exception("Failed to push app_activated event")
