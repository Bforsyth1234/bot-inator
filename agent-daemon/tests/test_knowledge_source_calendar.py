"""#138: the Calendar reader for the knowledge index (ai/knowledge_sources/calendar.py).

Every test uses a fake calendar backend (or a fresh, unconnected
``AppCalendarBridge``) and tmp paths: never the user's real calendar,
the menu bar app or ~/.bot-inator.

``ai/knowledge_sources`` has no ``__init__.py`` yet (the wave runner adds
it), so the module is imported as a namespace-package submodule with
``importlib``; that works with or without the ``__init__.py``.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from ai import knowledge_index as ki

cal_src = importlib.import_module("ai.knowledge_sources.calendar")

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=TZ)


def clock():
    return NOW


def ev(id_, title, start, *, hours=1, notes="", location="", attendees=None, all_day=False,
       calendar="Work"):
    event = {"id": id_, "title": title, "start": start.isoformat(),
             "end": (start + timedelta(hours=hours)).isoformat(), "calendar": calendar,
             "location": location, "notes": notes, "url": "", "all_day": all_day}
    if attendees is not None:
        event["attendees"] = attendees
    return event


class FakeBackend:
    """``calendar_access`` backend shape: ready() / list(start, end) / max_range_days.
    ``list`` returns the events OVERLAPPING [start, end), like EventKit."""

    name = "fake"

    def __init__(self, events=(), ready=True, max_range_days=31, fail_after=None):
        self.events = list(events)
        self._ready = ready
        self.max_range_days = max_range_days
        self.calls: list[tuple[datetime, datetime]] = []
        self.fail_after = fail_after
        self.writes = 0

    def ready(self):
        return self._ready

    def list(self, start, end, calendars=None):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("The menu bar app didn't answer the calendar request within 15 s.")
        self.calls.append((start, end))
        out = []
        for e in self.events:
            s = datetime.fromisoformat(e["start"])
            f = datetime.fromisoformat(e["end"])
            if s < end and f > start:
                out.append(dict(e))
        return sorted(out, key=lambda e: e["start"])

    def create(self, *a, **k):  # pragma: no cover - must never be called
        self.writes += 1
        raise AssertionError("the reader must never create events")

    def delete(self, *a, **k):  # pragma: no cover - must never be called
        self.writes += 1
        raise AssertionError("the reader must never delete events")


SAM = [{"name": "Sam Okafor", "email": "sam@example.com", "is_self": False},
       {"name": "Me", "email": "me@example.com", "is_self": True}]


def standard_events():
    return [
        ev("E1", "Project sync", NOW - timedelta(days=40), attendees=SAM,
           location="Room 4"),
        ev("E2", "Dentist", NOW + timedelta(days=9),
           notes="Bring the insurance card; ask about the crown on tooth 14."),
        ev("E3", "Lunch", NOW - timedelta(days=3), attendees=[]),
    ]


def by_id(listing):
    return {i.item_id.split("@")[0]: i for i in listing.items}


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


def test_module_interface():
    assert cal_src.NAME == "calendar"
    assert cal_src.TITLE and cal_src.DESCRIPTION and cal_src.TAINT_LABEL
    assert cal_src.DEFAULT_OPTIONS == {"days_back": 730, "days_ahead": 365}
    assert cal_src.validate_options({}) == cal_src.DEFAULT_OPTIONS
    import json
    json.dumps(cal_src.DEFAULT_OPTIONS)


def test_not_connected_wording_matches_calendar_tools():
    from tools import calendar_access as ca

    assert cal_src.APP_NOT_CONNECTED == ca.APP_NOT_CONNECTED


# ---------------------------------------------------------------------------
# Items: text, ids, versions
# ---------------------------------------------------------------------------


def test_event_found_by_attendee_name_and_notes_word(tmp_path):
    backend = FakeBackend(standard_events())
    # End to end through the index: the reader stands in for a source slot
    # (``calendar`` is not in SOURCES until the wave runner registers it).
    index = ki.KnowledgeIndex(tmp_path / "k.db", tmp_path / "s.json", embedder=None,
                              readers={"meeting_notes": lambda: cal_src.listing(
                                  {}, backend=backend, clock=clock)})
    index.set_source_enabled("meeting_notes", True)
    res = index.index_source("meeting_notes")
    assert res["indexed"] == 3 and res["error"] == ""
    hits = index.search("when did I last meet Okafor?")
    assert hits and hits[0]["item_id"].startswith("E1@")
    assert hits[0]["title"] == "Project sync"
    hits = index.search("crown")
    assert hits and hits[0]["item_id"].startswith("E2@")
    assert hits[0]["date"].startswith((NOW + timedelta(days=9)).date().isoformat())
    assert backend.writes == 0


def test_item_fields():
    items = by_id(cal_src.listing({}, backend=FakeBackend(standard_events()), clock=clock))
    sync = items["E1"]
    start = NOW - timedelta(days=40)
    assert sync.item_id == f"E1@{start.astimezone().isoformat(timespec='seconds')}"
    assert sync.date == start.astimezone().isoformat(timespec="seconds")
    assert sync.title == "Project sync" and sync.link == ""
    assert "Attendees: Sam Okafor" in sync.text
    assert "Me" not in sync.text.split("Attendees:")[1]  # the user's own entry left out
    assert "Location: Room 4" in sync.text and "Calendar: Work" in sync.text
    assert str(start.year) in sync.text  # the year is in the text ("when did I last ...")
    assert "Attendees" not in items["E3"].text


def test_recurring_occurrences_are_separate_items():
    weekly = [ev("R1", "Standup", NOW - timedelta(days=7 * k)) for k in range(5)]
    listing = cal_src.listing({}, backend=FakeBackend(weekly), clock=clock)
    ids = [i.item_id for i in listing.items]
    assert len(ids) == 5 and len(set(ids)) == 5
    assert all(i.startswith("R1@") for i in ids)


def test_changed_event_gets_new_version_unchanged_keeps_it():
    events = standard_events()
    first = by_id(cal_src.listing({}, backend=FakeBackend(events), clock=clock))
    again = by_id(cal_src.listing({}, backend=FakeBackend(standard_events()), clock=clock))
    assert {k: v.version for k, v in first.items()} == {k: v.version for k, v in again.items()}
    changed = standard_events()
    changed[1]["notes"] = "Moved: bring the new insurance card."
    changed[0]["attendees"] = SAM + [{"name": "Priya", "email": "", "is_self": False}]
    after = by_id(cal_src.listing({}, backend=FakeBackend(changed), clock=clock))
    assert after["E2"].version != first["E2"].version
    assert after["E1"].version != first["E1"].version
    assert after["E3"].version == first["E3"].version


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def test_window_bounds_respected():
    events = [
        ev("OLD", "Too old", NOW - timedelta(days=731)),
        ev("EDGE_OLD", "Just inside back", NOW - timedelta(days=729)),
        ev("EDGE_NEW", "Just inside ahead", NOW + timedelta(days=364)),
        ev("FAR", "Too far ahead", NOW + timedelta(days=366)),
        # Starts before the window but ends inside it: not this window's occurrence.
        ev("SPAN", "Long trip", NOW - timedelta(days=731), hours=72),
    ]
    backend = FakeBackend(events)
    got = set(by_id(cal_src.listing({}, backend=backend, clock=clock)))
    assert got == {"EDGE_OLD", "EDGE_NEW"}
    # Read in slices no longer than the backend's cap, covering the window exactly.
    assert all(b - a <= timedelta(days=31) for a, b in backend.calls)
    assert backend.calls[0][0] == NOW - timedelta(days=730)
    assert backend.calls[-1][1] == NOW + timedelta(days=365)
    small = set(by_id(cal_src.listing({"days_back": 30, "days_ahead": 0},
                                      backend=FakeBackend(standard_events()), clock=clock)))
    assert small == {"E3"}


def test_event_on_a_slice_boundary_is_listed_once():
    backend = FakeBackend([], max_range_days=14)
    lo = NOW - timedelta(days=730)
    boundary = lo + timedelta(days=14) - timedelta(hours=1)  # overlaps two slices
    backend.events = [ev("B", "Overnight", boundary, hours=3)]
    listing = cal_src.listing({}, backend=backend, clock=clock)
    assert [i.item_id.split("@")[0] for i in listing.items] == ["B"]
    assert all(b - a <= timedelta(days=14) for a, b in backend.calls)


# ---------------------------------------------------------------------------
# Completeness and errors
# ---------------------------------------------------------------------------


def test_full_read_is_complete_and_prunes_deleted_event(tmp_path):
    backend = FakeBackend(standard_events())
    index = ki.KnowledgeIndex(tmp_path / "k.db", tmp_path / "s.json", embedder=None,
                              readers={"meeting_notes": lambda: cal_src.listing(
                                  {}, backend=backend, clock=clock)})
    index.set_source_enabled("meeting_notes", True)
    first = cal_src.listing({}, backend=backend, clock=clock)
    assert first.complete is True and first.error == ""
    index.index_source("meeting_notes")
    backend.events = [e for e in backend.events if e["id"] != "E2"]
    res = index.index_source("meeting_notes")
    assert res["removed"] == 1
    assert index.search("crown") == []


def test_not_ready_is_error_without_reading_or_pruning(tmp_path):
    backend = FakeBackend(standard_events())
    holder = {"b": backend}
    index = ki.KnowledgeIndex(tmp_path / "k.db", tmp_path / "s.json", embedder=None,
                              readers={"meeting_notes": lambda: cal_src.listing(
                                  {}, backend=holder["b"], clock=clock)})
    index.set_source_enabled("meeting_notes", True)
    index.index_source("meeting_notes")
    off = FakeBackend(standard_events(), ready=False)
    holder["b"] = off
    listing = cal_src.listing({}, backend=off, clock=clock)
    assert listing.complete is False and listing.error and list(listing.items) == []
    assert off.calls == []  # never lists (an app list while not_determined would prompt)
    res = index.index_source("meeting_notes")
    assert res["removed"] == 0 and res["error"]
    assert index.search("crown")  # still indexed


def test_app_not_connected_gives_the_calendar_tools_message():
    from tools import calendar_access as ca

    backend = ca.AppCalendarBackend(ca.AppCalendarBridge())  # fresh bridge: no app
    listing = cal_src.listing({}, backend=backend, clock=clock)
    assert listing.complete is False
    assert listing.error == ca.APP_NOT_CONNECTED
    assert list(listing.items) == []


@pytest.mark.parametrize("auth,phrase", [
    ("denied", "denied for the bot-inator menu bar app"),
    ("restricted", "restricted on this Mac"),
    ("write_only", "can only add events"),
    ("not_determined", "hasn't been asked yet"),
])
def test_app_without_access_gives_the_app_wording_and_never_lists(auth, phrase):
    from tools import calendar_access as ca

    bridge = ca.AppCalendarBridge()
    sent = []

    async def send(*args):  # pragma: no cover - must never be reached
        sent.append(args)

    bridge.connect("c1", send, loop=None)  # type: ignore[arg-type]
    bridge.set_status("c1", auth)
    listing = cal_src.listing({}, backend=ca.AppCalendarBackend(bridge), clock=clock)
    assert listing.complete is False and phrase in listing.error
    assert sent == []


def test_failure_mid_read_is_incomplete_with_error_no_exception():
    backend = FakeBackend(standard_events(), fail_after=3)
    listing = cal_src.listing({}, backend=backend, clock=clock)
    assert listing.complete is False
    assert "didn't answer" in listing.error


def test_backend_selection_error_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_BACKEND", "bogus")
    from tools import calendar_access as ca

    monkeypatch.setattr(ca, "_override", None)
    listing = cal_src.listing({}, clock=clock)
    assert listing.complete is False and "AGENT_CALENDAR_BACKEND" in listing.error


def test_cap_makes_listing_incomplete():
    many = [ev(f"M{k}", "Thing", NOW - timedelta(days=k)) for k in range(20)]
    listing = cal_src.listing({}, backend=FakeBackend(many), clock=clock, max_items=5)
    assert listing.complete is False and len(list(listing.items)) == 5


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "730", [1], {"days_back": "730"}, {"days_back": True}, {"days_back": -1},
    {"days_ahead": 1.5}, {"days_back": 99999}, {"days_ahead": 99999},
    {"days_back": 0, "days_ahead": 0}, {"calendars": ["Work"]},
])
def test_validate_options_rejects_junk(bad):
    with pytest.raises(ValueError) as err:
        cal_src.validate_options(bad)
    assert str(err.value)


def test_validate_options_returns_cleaned_copy():
    given = {"days_back": 30}
    out = cal_src.validate_options(given)
    assert out == {"days_back": 30, "days_ahead": 365}
    assert given == {"days_back": 30}
    assert cal_src.validate_options(None) == cal_src.DEFAULT_OPTIONS


def test_listing_with_bad_options_is_error_not_exception():
    backend = FakeBackend(standard_events())
    listing = cal_src.listing({"days_back": "lots"}, backend=backend, clock=clock)
    assert listing.complete is False and listing.error and backend.calls == []
