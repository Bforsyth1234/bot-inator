"""Tests for the ``recall_activity`` timeline tool (#126).

Both sources are the real stores on fixtures: an ``ActivityStore`` in
a short temp directory and an in-memory ``ActionJournal`` whose moves are real files.
"""

from __future__ import annotations

import importlib
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import privacy
from ai.action_journal import ActionJournal
from ai.activity_store import STORED_FIELDS, ActivityStore
from events.event_bus import ContextEvent

# ``tools.recall_activity`` as an attribute becomes the registered tool
# once the runner registers it, so fetch the module itself.
ra = importlib.import_module("tools.recall_activity")

TZ = timezone(timedelta(hours=-5))
TODAY = date(2026, 11, 18)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def box():
    """A short directory: the journal clips a label at 160 characters, and
    pytest's ``tmp_path`` is long enough to reach that."""
    path = Path(tempfile.mkdtemp(prefix="ra", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def store(box):
    s = ActivityStore(box / "activity.db", enabled=True, tz=TZ, retention_days=10_000)
    yield s
    s.close()


@pytest.fixture
def clock():
    c = Clock()
    c.now = at(18, 12).timestamp()
    return c


@pytest.fixture
def journal(clock):
    j = ActionJournal(None, clock=clock)
    yield j
    j.close()


def file_event(when: datetime, path: str, action: str = "created") -> ContextEvent:
    return ContextEvent("file", timestamp=when, metadata={"path": path, "action": action})


def app_event(when: datetime, app: str, pid: int) -> ContextEvent:
    return ContextEvent("app_activated", app_name=app, timestamp=when, metadata={"pid": pid})


def move(journal, clock, box, when: datetime, name: str, folder: str = "Invoices"):
    """A real move_file result journaled at ``when``; returns the entry."""
    dst = box / folder / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"x")
    clock.now = when.timestamp()
    entry = journal.record("move_file", {}, {"status": "ok", "new_path": str(dst),
                                            "old_path": str(box / "Downloads" / name)},
                           why="filing rule")
    assert entry is not None
    return entry


def timeline(store, journal, day, until="", kind=""):
    return ra.build_timeline(day, until, kind, activity=store, journal=journal, today=TODAY)


def test_merges_and_orders_both_stores(store, journal, clock, box):
    store.record(app_event(at(10, 9, 0), "Safari", 1))
    store.record(file_event(at(10, 9, 30), "/tmp/Downloads/acme-invoice.pdf"))
    store.record(ContextEvent("mail_received", timestamp=at(10, 11, 0), metadata={"bulk": False}))
    store.record(app_event(at(11, 8, 0), "Xcode", 2))        # the next day
    store.record(app_event(at(9, 23, 59), "Notes", 3))       # the day before
    move(journal, clock, box, at(10, 9, 45), "acme-invoice.pdf")
    move(journal, clock, box, at(11, 0, 0), "other.pdf")  # midnight after: outside
    out = timeline(store, journal, "2026-11-10")
    assert out["status"] == "ok" and out["history"] == "on"
    assert (out["from"], out["to"], out["count"]) == ("2026-11-10", "2026-11-10", 4)
    assert (out["events"], out["actions"], out["truncated"], out["notes"]) == (3, 1, False, [])
    assert [(l["time"], l["source"], l["text"]) for l in out["lines"]] == [
        ("2026-11-10 09:00", "activity", "opened Safari"),
        ("2026-11-10 09:30", "activity", "new .pdf file in /tmp/Downloads"),
        ("2026-11-10 09:45", "agent", f"Moved acme-invoice.pdf to {box / 'Invoices'}"),
        ("2026-11-10 11:00", "activity", "mail arrived (from a person)"),
    ]
    agent = out["lines"][2]
    assert agent["tool"] == "move_file" and agent["undone"] is False and agent["why"] == "filing rule"
    # The activity half never names the file (#75).
    assert all("acme" not in l["text"] for l in out["lines"] if l["source"] == "activity")
    # A range takes in the neighbours; ``kind`` narrows.
    out = timeline(store, journal, "2026-11-09", until="2026-11-11")
    assert out["count"] == 7 and out["lines"][0]["text"] == "opened Notes"
    assert [l["time"] for l in out["lines"]] == sorted(l["time"] for l in out["lines"])
    out = timeline(store, journal, "2026-11-10", kind="apps")
    assert [l["text"] for l in out["lines"]] == ["opened Safari"]
    out = timeline(store, journal, "2026-11-10", kind="files")
    assert [l["source"] for l in out["lines"]] == ["activity", "agent"]
    out = timeline(store, journal, "2026-11-10", kind="actions")
    assert [l["source"] for l in out["lines"]] == ["agent"] and out["events"] == 0


def test_marks_an_undone_action(store, journal, clock, box):
    kept = move(journal, clock, box, at(10, 9, 0), "kept.pdf")
    undone = move(journal, clock, box, at(10, 10, 0), "back.pdf")
    (box / "Downloads").mkdir()
    clock.now = at(10, 10, 5).timestamp()
    assert journal.undo(undone["id"])["status"] == "undone"
    out = timeline(store, journal, "2026-11-10")
    assert [(l["text"].split(" to ")[0], l["undone"]) for l in out["lines"]] == [
        ("Moved kept.pdf", False), ("Moved back.pdf", True)]
    assert out["lines"][1]["text"].endswith(" (undone)")
    assert not out["lines"][0]["text"].endswith(" (undone)")
    assert kept["id"] != undone["id"]


def test_sums_a_busy_hour(store, journal, clock, box):
    for i in range(ra.HOUR_DETAIL):                       # 10:00: at the limit, kept
        store.record(app_event(at(10, 10, i), "Safari", 100 + i))
    for i in range(9):                                    # 14:00: busy, summed
        store.record(file_event(at(10, 14, i), f"/tmp/Downloads/f{i}.pdf"))
    for i in range(4):
        store.record(app_event(at(10, 14, 20 + i), "Xcode", 200 + i))
    move(journal, clock, box, at(10, 14, 30), "f1.pdf")
    out = timeline(store, journal, "2026-11-10")
    assert out["events"] == ra.HOUR_DETAIL + 13
    ten = [l for l in out["lines"] if l["time"].startswith("2026-11-10 10:")]
    assert len(ten) == ra.HOUR_DETAIL and all("summed" not in l for l in ten)
    two = [l for l in out["lines"] if l["source"] == "activity" and "14:" in l["time"]]
    assert two == [{"time": "2026-11-10 14:00-14:59", "source": "activity", "summed": 13,
                    "text": "13 things: 9x new .pdf file in /tmp/Downloads; 4x opened Xcode"}]
    # What the agent did in that hour is still its own line.
    assert [l["time"] for l in out["lines"] if l["source"] == "agent"] == ["2026-11-10 14:30"]
    assert out["count"] == ra.HOUR_DETAIL + 2


def test_at_most_100_lines(store, journal, clock, box):
    # 5 rows in each of 24 hours on two days: 240 detail lines, none "busy".
    for day in (10, 11):
        for hour in range(24):
            for i in range(5):
                store.record(app_event(at(day, hour, i), "Safari", day * 10_000 + hour * 100 + i))
    move(journal, clock, box, at(11, 12, 30), "a.pdf")
    out = timeline(store, journal, "2026-11-10", until="2026-11-11")
    assert out["count"] == 49 and out["count"] <= ra.MAX_LINES      # 48 hours + 1 action
    assert sum(l.get("summed", 0) for l in out["lines"]) == 240 == out["events"]
    # More hours than fit: summed per day.
    for day in (12, 13, 14):
        for hour in range(24):
            store.record(app_event(at(day, hour), "Safari", day * 10_000 + hour))
    out = timeline(store, journal, "2026-11-10", until="2026-11-14")
    assert [l["time"] for l in out["lines"] if l["source"] == "activity"] == [
        "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14"]
    assert out["actions"] == 1 and out["truncated"] is False
    # More actions than fit: the newest 100 lines, and it says so.
    for i in range(130):
        move(journal, clock, box, at(12, 8) + timedelta(minutes=i), f"m{i}.pdf")
    out = timeline(store, journal, "2026-11-10", until="2026-11-14")
    assert out["count"] == ra.MAX_LINES and out["truncated"] is True
    assert out["lines"][-1]["text"].startswith("Moved m129.pdf")
    assert any("left out" in n for n in out["notes"])


def test_history_off_returns_the_journal_only(store, journal, clock, box, monkeypatch):
    store.record(app_event(at(10, 9), "Safari", 1))          # recorded while it was on
    move(journal, clock, box, at(10, 9, 45), "acme-invoice.pdf")
    store.enabled = None                                     # follow the live switch
    monkeypatch.setenv("AGENT_ACTIVITY_HISTORY", "0")
    out = timeline(store, journal, "2026-11-10")
    assert out["status"] == "ok" and out["history"] == "off"
    assert ra.HISTORY_OFF_NOTE in out["notes"]
    assert [l["source"] for l in out["lines"]] == ["agent"] and out["events"] == 0
    monkeypatch.setenv("AGENT_ACTIVITY_HISTORY", "1")
    out = timeline(store, journal, "2026-11-10")
    assert out["history"] == "on" and out["notes"] == [] and out["count"] == 2


@pytest.mark.parametrize("day,until,kind", [
    ("last tuesday", "", ""), ("", "", ""), ("2026-11-10", "tomorrow-ish", ""),
    ("2026-02-30", "", ""), ("2026-11-10", "2026-11-09", ""),
    ("2026-11-01", "2026-11-15", ""),                        # 15 days
    ("2026-11-10", "", "secrets"),
])
def test_bad_input_is_an_error_result(day, until, kind):
    class Boom:
        enabled = True

        def rows(self, *a, **k):
            raise AssertionError("a store was read for bad input")

    out = ra.build_timeline(day, until, kind, activity=Boom(), journal=Boom(), today=TODAY)
    assert out["status"] == "error" and out["error"] and out["lines"] == []


def test_fourteen_days_is_allowed_and_a_failing_store_is_a_note(journal):
    class Broken:
        enabled = True

        def rows(self, *a, **k):
            raise RuntimeError("disk on fire")

    out = ra.build_timeline("2026-11-01", "2026-11-14", activity=Broken(), journal=None,
                            today=TODAY)
    assert out["status"] == "ok" and out["lines"] == []
    assert any("disk on fire" in n for n in out["notes"]) and ra.NO_JOURNAL_NOTE in out["notes"]


def test_the_tool_uses_the_injected_sources(store, journal, clock, box):
    store.record(app_event(at(10, 9), "Safari", 1))
    move(journal, clock, box, at(10, 9, 45), "a.pdf")
    fn = getattr(ra.recall_activity, "forward", ra.recall_activity)
    ra.set_sources(activity=store, journal=journal)
    try:
        out = fn("2026-11-10")
        assert [l["source"] for l in out["lines"]] == ["activity", "agent"]
        assert fn("nonsense")["status"] == "error"
        ra.set_sources(activity=store)                       # no journal handed over
        out = fn("2026-11-10")
        assert [l["source"] for l in out["lines"]] == ["activity"]
        assert ra.NO_JOURNAL_NOTE in out["notes"]
    finally:
        ra.set_sources()
    assert ra.READ_ONLY == {"recall_activity"}


def test_activity_rows_have_no_name_field():
    """The timeline can only describe what the store keeps (#75)."""
    assert "path" not in STORED_FIELDS and "name" not in STORED_FIELDS


# ---- decision 5: paths under a cloud engine ----------------------------------


def test_shorten_path_and_label(monkeypatch, box):
    monkeypatch.setattr(ra.Path, "home", classmethod(lambda cls: box))
    home = str(box)
    assert ra.shorten_path(f"{home}/Downloads/acme-invoice.PDF") == "~/Downloads/*.pdf"
    assert ra.shorten_path("/Volumes/USB/secret notes") == "/Volumes/USB/*"
    assert ra.shorten_label("move_file", f"Moved acme-invoice.pdf to {home}/Documents/Invoices") \
        == "Moved a .pdf file to ~/Documents/Invoices"
    # " to " inside the name or the folder: the first " to /" is the separator.
    assert ra.shorten_label("move_file", "Moved how to win.txt to /Volumes/a to /b") \
        == "Moved a .txt file to /Volumes/a to /b"
    assert ra.shorten_label("move_file", "Moved README to /srv") == "Moved a file to /srv"
    # A label that does not fit the pattern keeps nothing of the name.
    assert ra.shorten_label("move_file", "Moved " + "x" * 154) == "Moved a file"
    assert ra.shorten_label("move_file", "Moved a file") == "Moved a file"
    assert ra.shorten_label("create_event", "Added a calendar event in Work") \
        == "Added a calendar event in Work"


def _cloud(monkeypatch, cloud: bool, opt_in: bool):
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: cloud)
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: opt_in)


def test_redact_result_for_a_cloud_engine(store, journal, clock, box, monkeypatch):
    store.record(file_event(at(10, 9, 30), "/tmp/Downloads/acme-invoice.pdf"))
    move(journal, clock, box, at(10, 9, 45), "acme-invoice.pdf")
    undone = move(journal, clock, box, at(10, 10, 0), "pay.pdf")
    (box / "Downloads").mkdir()
    journal.undo(undone["id"])
    out = timeline(store, journal, "2026-11-10")
    assert "acme-invoice" in str(out) and "pay.pdf" in str(out)
    before = repr(out)

    _cloud(monkeypatch, cloud=True, opt_in=False)
    red = ra.redact_result(out, "main")
    assert "acme" not in str(red) and "pay.pdf" not in str(red) and "filing rule" not in str(red)
    assert [l["text"] for l in red["lines"]] == [
        "new .pdf file in /tmp/Downloads",
        f"Moved a .pdf file to {box / 'Invoices'}",
        f"Moved a .pdf file to {box / 'Invoices'} (undone)",
    ]
    assert red["lines"][2]["undone"] is True and ra.REDACTED_NOTE in red["notes"]
    assert repr(out) == before                               # the input is not changed

    for cloud, opt_in, role in [(False, False, "main"), (True, True, "main"), (True, False, None)]:
        _cloud(monkeypatch, cloud=cloud, opt_in=opt_in)
        assert ra.redact_result(out, role) is out
    _cloud(monkeypatch, cloud=True, opt_in=False)
    err = {"status": "error", "error": "x", "count": 0, "lines": []}
    assert ra.redact_result(err, "main") == err and ra.redact_result("text", "main") == "text"
