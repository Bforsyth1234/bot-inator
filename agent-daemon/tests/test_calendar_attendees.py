"""Attendees in the calendar layer (todo #80, for #71 meeting prep).

Attendees flow: backend (app DTO / EventKit / fake) -> ``make_event`` ->
``CalendarWatcher`` ``event_starting_soon`` -> ``meeting_prep.gather_prep``.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import threading
from datetime import datetime, timedelta
from unittest import mock

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from events.calendar_watcher import CalendarWatcher
from events.meeting_prep import gather_prep
from tools import calendar_access as ca

list_mod = importlib.import_module("tools.list_events")

TZ = datetime.now().astimezone().tzinfo
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
ORIGINAL_KEYS = ["title", "start", "location", "url", "calendar", "video_url"]

PEOPLE = [
    {"name": "Me", "email": "mailto:me@example.com", "is_self": True},
    {"name": "Alice Example", "email": "mailto:Alice%40Example.com"},
    {"name": "Bob", "url": "mailto:bob@example.com?subject=x"},
    {"name": "Bob again", "email": "mailto:BOB@example.com"},  # duplicate address
    {"name": "", "email": ""},  # nothing to keep
    {"name": "Carol (no address)", "email": "urn:uuid:1234"},
]


class _Backend:
    name = "fake"

    def __init__(self, events):
        self.events = events

    def ready(self):
        return True

    def list(self, start, end, calendars=None):
        return list(self.events)


class _Bus:
    def __init__(self):
        self.events = []

    def push_threadsafe(self, event):
        self.events.append(event)


def _ev(title, minutes_out=5, **kw):
    start = NOW + timedelta(minutes=minutes_out)
    return ca.make_event(id=kw.pop("id", "ID-" + title), title=title, start=start,
                         end=start + timedelta(minutes=30), **kw)


def _announce(*events):
    bus = _Bus()
    CalendarWatcher(bus, backend=lambda: _Backend(events), clock=lambda: NOW).poll_once()
    return bus.events


# ---- make_event ----------------------------------------------------------------------


def test_make_event_normalizes_attendees():
    ev = _ev("Sync", attendees=PEOPLE)
    assert ev["attendees"] == [
        {"name": "Me", "email": "me@example.com", "is_self": True},
        {"name": "Alice Example", "email": "alice@example.com", "is_self": False},
        {"name": "Bob", "email": "bob@example.com", "is_self": False},
        {"name": "Carol (no address)", "email": "", "is_self": False},
    ]


def test_make_event_without_attendees_is_unchanged():
    ev = _ev("Focus")
    assert "attendees" not in ev
    assert list(ev) == ["id", "title", "start", "end", "calendar", "location", "notes", "url", "all_day"]
    assert _ev("Solo", attendees=[])["attendees"] == []  # known: nobody


def test_make_event_caps_attendees():
    many = [{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(ca.ATTENDEES_LIMIT + 20)]
    assert len(_ev("All hands", attendees=many)["attendees"]) == ca.ATTENDEES_LIMIT


# ---- backends ------------------------------------------------------------------------


@pytest.fixture()
def bridge(monkeypatch):
    lp = asyncio.new_event_loop()
    thread = threading.Thread(target=lp.run_forever, daemon=True)
    thread.start()
    b = ca.AppCalendarBridge()
    monkeypatch.setattr(ca, "app_bridge", b)
    yield b, lp
    lp.call_soon_threadsafe(lp.stop)
    thread.join(timeout=5)
    lp.close()


def _app_list(bridge, events):
    b, lp = bridge

    async def send(request_id, op, args, timeout_seconds):
        asyncio.get_running_loop().call_later(
            0.01, b.submit_response, "ws", {"request_id": request_id, "ok": True, "events": events})

    b.connect("ws", send, lp)
    b.set_status("ws", "full_access")
    return ca.AppCalendarBackend().list(NOW, NOW + timedelta(days=1))


_SWIFT_EVENT = {"id": "E1", "title": "Sync", "start": "2026-11-10T09:05:00+00:00",
                "end": "2026-11-10T09:35:00+00:00", "calendar": "Work", "location": "",
                "notes": "", "url": "", "all_day": False}


def test_app_backend_passes_attendees_through(bridge):
    swift = dict(_SWIFT_EVENT, attendees=[
        {"name": "Me", "email": "me@example.com", "is_self": True},
        {"name": "Alice", "email": "alice@example.com", "is_self": False},
    ])
    [ev] = _app_list(bridge, [swift])
    assert ev["attendees"] == swift["attendees"]


def test_app_older_than_80_means_attendees_unknown(bridge):
    [ev] = _app_list(bridge, [_SWIFT_EVENT])
    assert "attendees" not in ev


class _NSURL:
    def __init__(self, s):
        self.s = s

    def absoluteString(self):
        return self.s


class _Participant:
    def __init__(self, name, url, me=False, kind=1):
        self._name, self._url, self._me, self._kind = name, url, me, kind

    def name(self):
        return self._name

    def URL(self):
        return _NSURL(self._url) if self._url else None

    def isCurrentUser(self):
        return self._me

    def participantType(self):
        return self._kind


class _EKEvent:
    def __init__(self, attendees, organizer=None):
        self._attendees, self._organizer = attendees, organizer

    def attendees(self):
        return self._attendees

    def organizer(self):
        return self._organizer


def test_eventkit_attendees():
    ev = _EKEvent([
        _Participant("Me", "mailto:me@example.com", me=True),
        _Participant("Alice", "mailto:alice@example.com"),
        _Participant("Room 4", "mailto:room4@example.com", kind=3),  # EKParticipantTypeRoom
        _Participant("Projector", "mailto:proj@example.com", kind=2),  # resource
    ], organizer=_Participant("Olga Organizer", "mailto:olga@example.com"))
    people = ca.clean_attendees(ca.EventKitBackend._attendees(ev))
    assert [(p["name"], p["email"], p["is_self"]) for p in people] == [
        ("Me", "me@example.com", True),
        ("Alice", "alice@example.com", False),
        ("Olga Organizer", "olga@example.com", False),
    ]
    # A solo event: EventKit answers nil -> known empty, organizer not added.
    assert ca.EventKitBackend._attendees(_EKEvent(None, organizer=_Participant("Me", "mailto:me@x.com", me=True))) == []


def test_applescript_events_have_no_attendees_key():
    rec = "\x1f".join(["U1", "Sync", "2026-11-10T09:00:00", "2026-11-10T09:15:00", "Work",
                       "", "", "", "false"])
    run = mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=rec + "\n", stderr=""))
    with mock.patch.object(ca.subprocess, "run", run):
        [ev] = ca.AppleScriptBackend().list(NOW, NOW + timedelta(days=1))
    assert "attendees" not in ev


# ---- watcher -------------------------------------------------------------------------


def test_watcher_adds_attendees_end_and_id():
    [event] = _announce(_ev("Sync", attendees=PEOPLE, id="EV-9"))
    meta = event.metadata
    assert meta["id"] == "EV-9"
    assert meta["end"] == ca.iso(NOW + timedelta(minutes=35))
    assert [p["email"] for p in meta["attendees"]] == [
        "me@example.com", "alice@example.com", "bob@example.com", ""]
    # Personal data after the original keys (previews show the first few).
    assert list(meta)[:6] == ORIGINAL_KEYS


def test_watcher_event_without_attendees_unchanged():
    [event] = _announce(_ev("Focus", id="EV-1"))
    assert list(event.metadata) == ORIGINAL_KEYS + ["end", "id"]
    assert "attendees" not in event.metadata


# ---- into meeting prep ---------------------------------------------------------------


def _fakes(mail_calls):
    def mail_with(addresses, since):
        mail_calls.append(list(addresses))
        return {"status": "ok", "messages": [
            {"message_id": 1, "sender": "Alice Example <alice@example.com>", "subject": "Agenda", "date": ""}]}

    return {"mail_with": mail_with,
            "find_files": lambda q, limit=5: {"status": "ok", "results": []},
            "find_note": lambda title: {"status": "ok", "notes": []},
            "redact_subject": lambda s: s}


def test_attendees_flow_from_backend_into_meeting_prep():
    [event] = _announce(_ev("Budget review", attendees=PEOPLE))  # no call link
    mail_calls = []
    payload = gather_prep(event.metadata, NOW, **_fakes(mail_calls))
    assert payload is not None  # known guests: prepped even without a call link
    assert mail_calls == [["alice@example.com", "bob@example.com"]]  # not the user's own address
    assert payload["attendees"]["status"] == "ok"
    assert payload["attendees"]["items"][:2] == ["Alice Example <alice@example.com>", "Bob <bob@example.com>"]
    assert payload["mail"]["status"] == "ok" and payload["mail"]["items"][0]["subject"] == "Agenda"


def test_solo_and_unknown_attendee_events_in_meeting_prep():
    mail_calls = []
    [solo] = _announce(_ev("Focus", attendees=[{"name": "Me", "email": "me@x.com", "is_self": True}]))
    assert gather_prep(solo.metadata, NOW, **_fakes(mail_calls)) is None
    [unknown] = _announce(_ev("Block"))  # old app / AppleScript, no call link: skipped as before
    assert gather_prep(unknown.metadata, NOW, **_fakes(mail_calls)) is None
    assert mail_calls == []


# ---- list_events ---------------------------------------------------------------------


def test_list_events_shows_names_not_addresses():
    out = list_mod._with_when(_ev("Sync", attendees=PEOPLE))
    assert out["attendees"] == ["Alice Example", "Bob", "Carol (no address)"]
    assert "@" not in repr(out)
    assert "attendees" not in list_mod._with_when(_ev("Focus"))
