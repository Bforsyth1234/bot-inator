"""#14: temp/partial download names and dotfiles are dropped at the
source, and ``modified`` bursts are debounced to one per path per 2 s."""

from __future__ import annotations

import pytest
from watchdog.events import (
    DirModifiedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from events.event_bus import ContextEvent
from events.file_watcher import _FileChangeHandler


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


@pytest.fixture
def setup():
    bus, clock = FakeBus(), FakeClock()
    return bus, clock, _FileChangeHandler(bus, clock=clock)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    ["Zoom.dmg.crdownload", "report.pdf.download", "movie.mkv.PART", "x.tmp"],
)
def test_temp_suffixes_dropped(setup, tmp_path, name) -> None:
    bus, _, handler = setup
    path = str(tmp_path / name)
    handler.on_created(FileCreatedEvent(path))
    handler.on_modified(FileModifiedEvent(path))
    assert bus.pushed == []


def test_dotfiles_dropped(setup, tmp_path) -> None:
    bus, _, handler = setup
    for name in (".DS_Store", ".com.google.Chrome.abc123"):
        handler.on_created(FileCreatedEvent(str(tmp_path / name)))
        handler.on_modified(FileModifiedEvent(str(tmp_path / name)))
    assert bus.pushed == []


def test_rename_to_temp_name_dropped(setup, tmp_path) -> None:
    bus, _, handler = setup
    handler.on_moved(FileMovedEvent(str(tmp_path / "a.tmp"), str(tmp_path / "a.crdownload")))
    handler.on_moved(FileMovedEvent(str(tmp_path / "a"), str(tmp_path / ".a")))
    assert bus.pushed == []


def test_finished_download_rename_emitted_with_destination(setup, tmp_path) -> None:
    bus, _, handler = setup
    src, dest = str(tmp_path / "Zoom.dmg.crdownload"), str(tmp_path / "Zoom.dmg")
    handler.on_created(FileCreatedEvent(src))
    handler.on_modified(FileModifiedEvent(src))
    handler.on_moved(FileMovedEvent(src, dest))
    assert [e.metadata for e in bus.pushed] == [
        {"path": dest, "action": "renamed", "from": src},
    ]


def test_modified_burst_yields_one_event(setup, tmp_path) -> None:
    bus, clock, handler = setup
    path = str(tmp_path / "notes.txt")
    for _ in range(20):
        handler.on_modified(FileModifiedEvent(path))
        clock.now += 0.05
    assert [e.metadata["action"] for e in bus.pushed] == ["modified"]


def test_modification_after_window_emits_again(setup, tmp_path) -> None:
    bus, clock, handler = setup
    path = str(tmp_path / "notes.txt")
    handler.on_modified(FileModifiedEvent(path))
    clock.now += 1.9
    handler.on_modified(FileModifiedEvent(path))
    clock.now += 0.1  # 2.0 s after the first emitted event
    handler.on_modified(FileModifiedEvent(path))
    assert [e.metadata["action"] for e in bus.pushed] == ["modified", "modified"]


def test_debounce_is_per_path(setup, tmp_path) -> None:
    bus, _, handler = setup
    handler.on_modified(FileModifiedEvent(str(tmp_path / "a.txt")))
    handler.on_modified(FileModifiedEvent(str(tmp_path / "b.txt")))
    assert len(bus.pushed) == 2


def test_created_then_quick_modified_keeps_created(setup, tmp_path) -> None:
    bus, clock, handler = setup
    path = str(tmp_path / "new.txt")
    handler.on_created(FileCreatedEvent(path))
    clock.now += 0.1
    handler.on_modified(FileModifiedEvent(path))
    handler.on_created(FileCreatedEvent(path))  # created is never debounced
    assert [e.metadata["action"] for e in bus.pushed] == ["created", "created"]


def test_directories_ignored(setup, tmp_path) -> None:
    bus, _, handler = setup
    handler.on_modified(DirModifiedEvent(str(tmp_path)))
    assert bus.pushed == []
