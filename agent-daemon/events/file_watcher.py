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


# #131: directories macOS shows as one item. A file *inside* one is never
# its own event; the bundle is reported once instead.
BUNDLE_SUFFIXES = (
    ".app", ".appex", ".bundle", ".framework", ".plugin", ".kext", ".pkg",
    ".mpkg", ".xcarchive", ".xcodeproj", ".xcworkspace", ".photoslibrary",
    ".musiclibrary", ".prefpane", ".qlgenerator",
    ".saver", ".dsym", ".playground", ".rtfd", ".pages", ".numbers", ".key",
    ".lproj", ".nib", ".xpc", ".mdimporter",
)
# Folders whose contents are tooling churn, never something the user made.
NOISE_DIRS = frozenset({
    "__pycache__", "site-packages", "dist-packages", "node_modules",
    ".git", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
})
# A bundle is reported again only after this long with no event inside it,
# so a long unzip (minutes) still yields one event.
BUNDLE_QUIET_S = 30.0


def _is_noise(path: str) -> bool:
    """True for dotfiles and temp/partial download names."""
    name = PurePath(path).name
    return not name or name.startswith(".") or name.lower().endswith(_TEMP_SUFFIXES)


def _is_bundle_name(name: str) -> bool:
    return name.lower().endswith(BUNDLE_SUFFIXES)


def _inner_parts(path: str, roots: Iterable[PurePath]) -> tuple[PurePath, tuple[str, ...]]:
    """(base, parts below it): ``base`` is the watched root holding
    ``path`` (or ``/`` when none does), ``parts`` the components after it."""
    p = PurePath(path)
    best: PurePath | None = None
    for root in roots:
        try:
            p.relative_to(root)
        except ValueError:
            continue
        if best is None or len(root.parts) > len(best.parts):
            best = root
    if best is None:
        return PurePath(p.anchor or "/"), p.parts[1:] if p.anchor else p.parts
    return best, p.relative_to(best).parts


def classify_path(path: str, roots: Iterable[PurePath] = ()) -> tuple[str, str]:
    """``("drop", "")``, ``("bundle", <bundle path>)`` or ``("file", path)``.

    Below the watched root: a path under a hidden folder or one of
    :data:`NOISE_DIRS` is dropped; a path inside a bundle (an ``.app`` and
    the like) maps to the outermost bundle, which is reported once.
    """
    base, parts = _inner_parts(path, roots)
    parents = parts[:-1]
    for i, part in enumerate(parents):
        if _is_bundle_name(part):
            return "bundle", str(base.joinpath(*parts[: i + 1]))
        if part.startswith(".") or part.lower() in NOISE_DIRS:
            return "drop", ""
    return "file", path


class _FileChangeHandler(FileSystemEventHandler):
    """Pushes a :class:`ContextEvent` for create/modify/rename callbacks.

    Temp/partial download names and dotfiles are dropped at the source.
    ``modified`` is debounced to one event per path per
    :data:`MODIFIED_DEBOUNCE_S`; ``created`` and ``renamed`` always pass
    and start that window, so the write burst right after them is absorbed.

    #131: a file inside a bundle (``.app`` and the like, see
    :data:`BUNDLE_SUFFIXES`) is reported as one ``created`` (or ``renamed``)
    event for the outermost bundle per :data:`BUNDLE_QUIET_S` burst; a path
    under a hidden folder or :data:`NOISE_DIRS` below the watched root is
    dropped. Plain subfolders are unchanged (watches and routines use them).
    """

    def __init__(
        self,
        bus: EventBus,
        clock: Callable[[], float] = time.monotonic,
        debounce_s: float = MODIFIED_DEBOUNCE_S,
        roots: Iterable[str | Path] = (),
        bundle_quiet_s: float = BUNDLE_QUIET_S,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._clock = clock
        self._debounce_s = debounce_s
        self._roots: list[PurePath] = []
        for r in roots:
            expanded = Path(r).expanduser()
            for form in (expanded, expanded.resolve()):
                if PurePath(form) not in self._roots:
                    self._roots.append(PurePath(form))
        self._bundle_quiet_s = bundle_quiet_s
        self._bundles: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _bundle_admit(self, bundle: str) -> bool:
        """True the first time a bundle is seen in a quiet window. Every
        event inside it extends the window, so one burst is one event."""
        now = self._clock()
        with self._lock:
            last = self._bundles.get(bundle)
            self._bundles[bundle] = now
            if len(self._bundles) > 256:
                cutoff = now - self._bundle_quiet_s
                self._bundles = {b: t for b, t in self._bundles.items() if t >= cutoff}
            return last is None or now - last >= self._bundle_quiet_s

    def _push(self, metadata: dict[str, str]) -> None:
        try:
            ctx = ContextEvent(event_type="file", app_name=None, window_title=None,
                               metadata=metadata)
            self._bus.push_threadsafe(ctx)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to push file event for %s", metadata.get("path"))

    def _route(self, path: str, is_directory: bool) -> tuple[str, str]:
        """Where an event at ``path`` goes: drop, a bundle, or the file.

        A directory is reported only when it is itself a bundle that sits
        directly in a watched place (not inside another bundle or noise).
        """
        if _is_noise(path):
            return "drop", ""
        kind, target = classify_path(path, self._roots)
        if kind != "file":
            return kind, target
        if is_directory:
            return ("bundle", path) if _is_bundle_name(PurePath(path).name) else ("drop", "")
        return "file", path

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
        path = str(event.src_path)
        kind, target = self._route(path, event.is_directory)
        if kind == "drop":
            return
        if kind == "bundle":
            # A bundle "appears" (created) once; a change inside it later is
            # not news. Modified events inside only extend the quiet window.
            if self._bundle_admit(target) and action == "created":
                self._push({"path": target, "action": "created"})
            return
        if not self._admit(path, action):
            return
        self._push({"path": path, "action": action})

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
        # #131: an app bundle moved into place (Archive Utility unpacks to a
        # hidden folder, then renames) is one event for the bundle; moves of
        # files inside a bundle are not.
        dest = str(getattr(event, "dest_path", "") or event.src_path)
        kind, target = self._route(dest, event.is_directory)
        if kind == "drop":
            return
        if kind == "bundle":
            if target == dest and self._bundle_admit(target):
                self._push({"path": dest, "action": "renamed", "from": str(event.src_path)})
            else:
                self._bundle_admit(target)
            return
        self._admit(dest, "renamed")
        self._push({"path": dest, "action": "renamed", "from": str(event.src_path)})


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
        handler = _FileChangeHandler(self._bus, roots=self._directories)
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
