"""#131: unpacking an app into a watched folder is one ``file`` event for
the bundle, not one per file inside it; ``__pycache__`` / ``site-packages``
and hidden folders below the watched root never emit."""

from __future__ import annotations

import time
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from events.event_bus import ContextEvent
from events.file_watcher import FileWatcher, _FileChangeHandler, classify_path


class FakeBus:
    def __init__(self) -> None:
        self.pushed: list[ContextEvent] = []

    def push_threadsafe(self, ev: ContextEvent) -> None:
        self.pushed.append(ev)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _handler(root: Path):
    bus, clock = FakeBus(), FakeClock()
    return bus, clock, _FileChangeHandler(bus, clock=clock, roots=[root])  # type: ignore[arg-type]


TORCH = "bot-inator.app/Contents/Resources/python/lib/python3.14/site-packages/torch"


def test_files_inside_an_app_bundle_are_one_event_for_the_bundle(tmp_path) -> None:
    bus, clock, h = _handler(tmp_path)
    app = tmp_path / "bot-inator.app"
    h.on_created(DirCreatedEvent(str(app)))
    for i in range(40):
        h.on_created(FileCreatedEvent(str(tmp_path / TORCH / f"m{i}.py")))
        h.on_created(FileCreatedEvent(str(tmp_path / TORCH / "__pycache__" / f"m{i}.pyc")))
        h.on_modified(FileModifiedEvent(str(tmp_path / TORCH / f"m{i}.py")))
        clock.now += 0.5  # a 20 s unpack is still one burst
    assert [e.metadata for e in bus.pushed] == [{"path": str(app), "action": "created"}]


def test_bundle_contents_without_a_dir_event_still_report_the_bundle_once(tmp_path) -> None:
    bus, _, h = _handler(tmp_path)
    for i in range(10):
        h.on_created(FileCreatedEvent(str(tmp_path / "X.app" / "Contents" / f"f{i}")))
    assert [e.metadata["path"] for e in bus.pushed] == [str(tmp_path / "X.app")]


def test_bundle_reported_again_only_after_a_quiet_window(tmp_path) -> None:
    bus, clock, h = _handler(tmp_path)
    h.on_created(DirCreatedEvent(str(tmp_path / "X.app")))
    clock.now += 31
    h.on_created(DirCreatedEvent(str(tmp_path / "X.app")))
    assert len(bus.pushed) == 2


def test_noise_dirs_below_root_dropped_but_plain_subfolders_kept(tmp_path) -> None:
    bus, _, h = _handler(tmp_path)
    for rel in ("proj/__pycache__/a.pyc", "venv/lib/site-packages/x.py",
                "proj/node_modules/y.js", "proj/.git/objects/ab", ".BAH.123/z.txt"):
        h.on_created(FileCreatedEvent(str(tmp_path / rel)))
    h.on_created(DirCreatedEvent(str(tmp_path / "Invoices")))  # plain folder: no event
    h.on_created(FileCreatedEvent(str(tmp_path / "Invoices" / "acme.pdf")))
    assert [e.metadata["path"] for e in bus.pushed] == [str(tmp_path / "Invoices" / "acme.pdf")]


def test_bundle_moved_into_place_is_one_renamed_event(tmp_path) -> None:
    bus, _, h = _handler(tmp_path)
    src, dest = str(tmp_path / ".BAH.1" / "X.app"), str(tmp_path / "X.app")
    h.on_moved(DirMovedEvent(src, dest))
    h.on_moved(FileMovedEvent(src + "/Contents/a", dest + "/Contents/a"))
    h.on_modified(DirModifiedEvent(dest))
    assert [e.metadata for e in bus.pushed] == [{"path": dest, "action": "renamed", "from": src}]


def test_classify_path_without_roots() -> None:
    assert classify_path("/U/d/X.app/Contents/a") == ("bundle", "/U/d/X.app")
    assert classify_path("/U/d/x/__pycache__/a.pyc") == ("drop", "")
    assert classify_path("/U/d/report.pdf") == ("file", "/U/d/report.pdf")


def test_real_watcher_app_unpack_emits_exactly_one_event(tmp_path) -> None:
    """Verify block: a real FSEvents watcher on a tmp dir; an ``.app`` with
    nested files appears -> exactly one ``file`` event."""
    watched = tmp_path / "Downloads"
    watched.mkdir()
    bus = FakeBus()
    watcher = FileWatcher(bus, [watched])  # type: ignore[arg-type]
    watcher.start()
    try:
        time.sleep(0.5)
        inner = watched / TORCH
        (inner / "__pycache__").mkdir(parents=True)
        for i in range(30):
            (inner / f"m{i}.py").write_text("x")
            (inner / "__pycache__" / f"m{i}.pyc").write_text("x")
        deadline = time.monotonic() + 5
        while not bus.pushed and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1.5)  # let any stragglers arrive
    finally:
        watcher.stop()
    paths = [e.metadata["path"] for e in bus.pushed]
    assert len(paths) == 1, paths
    assert Path(paths[0]).name == "bot-inator.app"
