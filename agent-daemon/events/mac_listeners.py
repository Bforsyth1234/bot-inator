"""Event-driven listener for macOS active-application changes.

The parent daemon runs ``uvicorn`` on its main thread, which leaves no
``CFRunLoop`` available to service ``NSWorkspace``'s distributed-notification
mach-port source. To keep the listener event-driven (no polling), we spawn
a small subprocess helper whose *main* thread runs ``NSRunLoop`` and
observes ``NSWorkspaceDidActivateApplicationNotification``. The helper
emits a JSON line per activation on stdout; this listener reads those
lines on a background thread and pushes a :class:`ContextEvent` to the bus.

Supervision (#16): a supervisor thread owns the helper's lifetime. When the
helper exits for any reason while the listener is not stopping, it is
respawned after a backoff of 1, 2, 4 ... seconds, capped at 60 s. The
backoff resets once a helper has stayed up for ``stable_after`` seconds
(default 60 s: one full backoff cap, so a helper that crashes every ~minute
still backs off instead of hammering at 1 s). Liveness is exposed through
:attr:`is_alive`, :attr:`restart_count`, :attr:`last_error` and
:meth:`status`.

Debounce (#17): an activation is only pushed as ``app_activated`` after the
app has held focus for ``debounce_s`` (500 ms). A newer activation inside
that window replaces the pending one. The debounce lives here in the parent,
not in the helper: the helper is an AppKit-only shim that cannot be imported
in tests, its state is lost on every crash/restart, and keeping it a dumb
line emitter means everything with logic is covered by plain-Python tests.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Sequence

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

_HELPER_SCRIPT = Path(__file__).with_name("_mac_app_helper.py")

DEFAULT_BACKOFF_INITIAL_S = 1.0
DEFAULT_BACKOFF_MAX_S = 60.0
DEFAULT_STABLE_AFTER_S = 60.0
DEFAULT_DEBOUNCE_S = 0.5


class MacAppListener:
    """Supervises the NSWorkspace helper subprocess and forwards activations."""

    def __init__(
        self,
        bus: EventBus,
        *,
        helper_cmd: Sequence[str] | None = None,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL_S,
        backoff_max: float = DEFAULT_BACKOFF_MAX_S,
        stable_after: float = DEFAULT_STABLE_AFTER_S,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._helper_cmd = (
            list(helper_cmd)
            if helper_cmd is not None
            else [sys.executable, str(_HELPER_SCRIPT)]
        )
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._stable_after = stable_after
        self._debounce_s = debounce_s
        self._clock = clock

        self._process: subprocess.Popen[str] | None = None
        self._supervisor: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.restart_count = 0
        self.last_error: str | None = None
        self.last_exit_code: int | None = None
        self.current_backoff: float = backoff_initial
        self._started_at: float | None = None

        # Debounce state, guarded by ``_lock``.
        self._pending_timer: threading.Timer | None = None
        self._pending_gen = 0
        self._last_emitted_key: tuple[Any, Any] | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        if helper_missing(self._helper_cmd):
            logger.error("mac_app_helper script not found at %s", _HELPER_SCRIPT)
            self.last_error = f"helper script not found: {_HELPER_SCRIPT}"
            return
        self._stop_event.clear()
        self.current_backoff = self._backoff_initial
        self._supervisor = threading.Thread(
            target=self._supervise, name="MacAppListener-supervisor", daemon=True
        )
        self._supervisor.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_pending()
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:  # pragma: no cover
                logger.exception("Failed to terminate mac_app_helper")
        sup = self._supervisor
        if sup is not None and sup is not threading.current_thread():
            sup.join(timeout=3.0)
        self._supervisor = None
        self._process = None

    # ------------------------------------------------------------- liveness

    @property
    def is_alive(self) -> bool:
        proc = self._process
        return proc is not None and proc.poll() is None

    def status(self) -> dict[str, Any]:
        proc = self._process
        started = self._started_at
        return {
            "alive": self.is_alive,
            "pid": proc.pid if proc is not None else None,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "last_exit_code": self.last_exit_code,
            "uptime_s": (
                round(self._clock() - started, 1)
                if started is not None and self.is_alive
                else None
            ),
        }

    # ----------------------------------------------------------- supervisor

    def _supervise(self) -> None:
        while not self._stop_event.is_set():
            proc = self._spawn()
            if proc is not None:
                started = self._clock()
                self._started_at = started
                self._pump(proc)
                code = proc.wait()
                if self._stop_event.is_set():
                    # stop() terminated it: not a crash, so keep the last
                    # real exit code and error for status() (todo #50).
                    break
                self.last_exit_code = code
                uptime = self._clock() - started
                self.last_error = f"helper exited with code {code} after {uptime:.1f}s"
                if uptime >= self._stable_after:
                    self.current_backoff = self._backoff_initial
            if self._stop_event.is_set():
                break
            delay = self.current_backoff
            logger.warning(
                "mac_app_helper down (%s); restarting in %.1fs", self.last_error, delay
            )
            if self._stop_event.wait(delay):
                break
            self.restart_count += 1
            self.current_backoff = min(delay * 2, self._backoff_max)

    def _spawn(self) -> subprocess.Popen[str] | None:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                self._helper_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            logger.exception("Failed to spawn mac_app_helper subprocess")
            self.last_error = f"spawn failed: {exc!r}"
            return None
        self._process = proc
        if self._stop_event.is_set():  # stop() raced the spawn
            proc.terminate()
        logger.info("mac_app_helper spawned (pid=%s)", proc.pid)
        return proc

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        """Forward stderr on a side thread; read stdout here until EOF."""
        if proc.stderr is not None:
            threading.Thread(
                target=self._read_stderr,
                args=(proc.stderr,),
                name="MacAppListener-stderr",
                daemon=True,
            ).start()
        if proc.stdout is None:
            return
        for line in _iter_lines(proc.stdout):
            if self._stop_event.is_set():
                return
            self._handle_line(line)
        logger.info("mac_app_helper stdout closed")

    def _read_stderr(self, stream: IO[str]) -> None:
        for line in _iter_lines(stream):
            if self._stop_event.is_set():
                return
            logger.info("mac_app_helper: %s", line.strip())

    # ------------------------------------------------------------- debounce

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("mac_app_helper emitted non-JSON line: %r", line)
            return
        if not isinstance(payload, dict):
            logger.warning("mac_app_helper emitted non-object line: %r", line)
            return
        app_name = payload.get("app_name")
        bundle_id = payload.get("bundle_id")
        pid = payload.get("pid")
        logger.debug(
            "NSWorkspace activation: app=%s bundle=%s pid=%s", app_name, bundle_id, pid
        )
        event = ContextEvent(
            event_type="app_activated",
            app_name=app_name,
            window_title=None,
            timestamp=datetime.now(timezone.utc),
            metadata={"bundle_id": bundle_id, "pid": pid},
        )
        self._schedule(event, (bundle_id, pid))

    def _schedule(self, event: ContextEvent, key: tuple[Any, Any]) -> None:
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
            self._pending_gen += 1
            gen = self._pending_gen
            timer = threading.Timer(self._debounce_s, self._fire, args=(gen, event, key))
            timer.daemon = True
            self._pending_timer = timer
            timer.start()

    def _cancel_pending(self) -> None:
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
            self._pending_timer = None
            self._pending_gen += 1

    def _fire(self, gen: int, event: ContextEvent, key: tuple[Any, Any]) -> None:
        with self._lock:
            if gen != self._pending_gen or self._stop_event.is_set():
                return  # superseded by a newer switch, or stopping
            self._pending_timer = None
            if key == self._last_emitted_key:
                # A -> B -> A inside the window: focus never left A for long.
                return
            self._last_emitted_key = key
        logger.info(
            "app_activated (held %.0f ms): app=%s bundle=%s pid=%s",
            self._debounce_s * 1000,
            event.app_name,
            event.metadata.get("bundle_id"),
            event.metadata.get("pid"),
        )
        try:
            self._bus.push_threadsafe(event)
        except Exception:  # pragma: no cover - loop not bound or shutting down
            logger.exception("Failed to push app_activated event")


def helper_missing(cmd: Sequence[str]) -> bool:
    """True when the default helper script is the target and it is absent."""
    return str(_HELPER_SCRIPT) in cmd and not _HELPER_SCRIPT.exists()


def _iter_lines(stream: IO[str]):
    while True:
        try:
            line = stream.readline()
        except (ValueError, OSError):  # stream closed under us
            return
        if not line:
            return
        yield line
