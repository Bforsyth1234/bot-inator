"""Filesystem listener backed by ``watchdog``.

Uses the platform-native observer (FSEvents on macOS) so events are
delivered as kernel callbacks — there is no polling loop in this module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)


class _FileChangeHandler(FileSystemEventHandler):
    """Pushes a :class:`ContextEvent` for every create/modify callback."""

    def __init__(self, bus: EventBus) -> None:
        super().__init__()
        self._bus = bus

    def _emit(self, event: FileSystemEvent, action: str) -> None:
        if event.is_directory:
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
