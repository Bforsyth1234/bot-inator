"""Filesystem listener backed by ``watchdog``.

Uses the platform-native observer (FSEvents on macOS) so events are
delivered as kernel callbacks — there is no polling loop in this module.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path, PurePath
from typing import Callable, Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

# In-progress download / scratch names. Kept in step with
# ``ai.idea_sources._TEMP_SUFFIXES`` (which filters again downstream).
_TEMP_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")

# At most one ``modified`` event per path per this many seconds.
MODIFIED_DEBOUNCE_S = 2.0


def _is_noise(path: str) -> bool:
    """True for dotfiles and temp/partial download names."""
    name = PurePath(path).name
    return not name or name.startswith(".") or name.lower().endswith(_TEMP_SUFFIXES)


class _FileChangeHandler(FileSystemEventHandler):
    """Pushes a :class:`ContextEvent` for create/modify/rename callbacks.

    Temp/partial download names and dotfiles are dropped at the source.
    ``modified`` is debounced to one event per path per
    :data:`MODIFIED_DEBOUNCE_S`; ``created`` and ``renamed`` always pass
    and start that window, so the write burst right after them is absorbed.
    """

    def __init__(
        self,
        bus: EventBus,
        clock: Callable[[], float] = time.monotonic,
        debounce_s: float = MODIFIED_DEBOUNCE_S,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._clock = clock
        self._debounce_s = debounce_s
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _stamp(self, path: str, now: float) -> None:
        self._last_seen[path] = now
        if len(self._last_seen) > 1024:  # bound memory: drop expired stamps
            cutoff = now - self._debounce_s
            self._last_seen = {p: t for p, t in self._last_seen.items() if t >= cutoff}

    def _admit(self, path: str, action: str) -> bool:
        """Record the event time; False when a ``modified`` is in-window."""
        now = self._clock()
        with self._lock:
            last = self._last_seen.get(path)
            if action == "modified" and last is not None and now - last < self._debounce_s:
                return False
            self._stamp(path, now)
            return True

    def _emit(self, event: FileSystemEvent, action: str) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        if _is_noise(path) or not self._admit(path, action):
            return
        try:
            ctx = ContextEvent(
                event_type="file",
                app_name=None,
                window_title=None,
                metadata={"path": str(event.src_path), "action": action},
            )
            self._bus.push_threadsafe(ctx)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to push file event for %s", event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._emit(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._emit(event, "modified")

    def on_moved(self, event: FileSystemEvent) -> None:
        # Browsers download to a temp name (``.crdownload``, ``.download``,
        # ``.part``) and rename on completion, so a finished download
        # arrives only as a move. Report the destination path.
        # A rename *to* a temp name or dotfile is churn; a rename *from*
        # one to a real name is the finished download and is kept.
        if event.is_directory:
            return
        dest = str(getattr(event, "dest_path", "") or event.src_path)
        if _is_noise(dest):
            return
        self._admit(dest, "renamed")
        try:
            ctx = ContextEvent(
                event_type="file",
                metadata={
                    "path": dest,
                    "action": "renamed",
                    "from": str(event.src_path),
                },
            )
            self._bus.push_threadsafe(ctx)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to push move event for %s", event.src_path)


class FileWatcher:
    """Watch one or more directories for file create/modify events."""

    def __init__(
        self,
        bus: EventBus,
        directories: Iterable[str | Path],
        recursive: bool = True,
    ) -> None:
        self._bus = bus
        self._directories = [Path(d).expanduser() for d in directories]
        self._recursive = recursive
        self._observer: Observer | None = None

    def start(self) -> None:
        if self._observer is not None:
            return
        handler = _FileChangeHandler(self._bus)
        observer = Observer()
        for directory in self._directories:
            if not directory.exists():
                logger.warning("Watch directory does not exist: %s", directory)
                continue
            observer.schedule(handler, str(directory), recursive=self._recursive)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=5.0)
        self._observer = None
