"""Tests for the Calendar tools and watcher (#42). No real calendar access:
every test uses a fake backend or a mocked ``osascript``."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import ai  # noqa: E402,F401  - import order: ai -> tools avoids a circular import
import importlib  # noqa: E402

import tools  # noqa: E402
from events.calendar_watcher import CalendarWatcher, find_video_link  # noqa: E402
from events.event_bus import ContextEvent  # noqa: E402
from tools import calendar_access as ca  # noqa: E402
create_mod = importlib.import_module("tools.create_event")  # module, not the registered tool
list_mod = importlib.import_module("tools.list_events")  # module, not the registered tool
from tools.create_event import create_event  # noqa: E402
from tools.list_events import list_events  # noqa: E402

TZ = datetime.now().astimezone().tzinfo


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


class FakeBackend:
    name = "fake"

    def __init__(self, events=None, max_range_days=31, ready=True):
        self.events = events or []
        self.max_range_days = max_range_days
        self._ready = ready
        self.list_calls: list[tuple] = []
        self.create_calls: list[tuple] = []

    def ready(self):
        return self._ready

    def list(self, start, end, calendars=None):
        self.list_calls.append((start, end, calendars))
        return [e for e in self.events
                if datetime.fromisoformat(e["start"]) < end and datetime.fromisoformat(e["end"]) > start]

    def create(self, title, start, end, calendar=None, location=None, notes=None):
        self.create_calls.append((title, start, end, calendar, location, notes))
        return {"status": "ok", "event_id": "EV1", "calendar": calendar or "Home"}


@pytest.fixture()
def fake():
    backend = FakeBackend()
    ca.set_backend(backend)
    yield backend
    ca.set_backend(None)


def _ev(title, start, minutes=30, **kw):
    return ca.make_event(id=kw.pop("id", title), title=title, start=start,
                         end=start + timedelta(minutes=minutes), **kw)


# ---- list_events ------------------------------------------------------------


def test_list_events_defaults_to_today(fake) -> None:
    result = list_events(start="", end="")
    assert result["status"] == "ok"
    start, end, _ = fake.list_calls[0]
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assert start == today
    assert end == today + timedelta(days=1)
    assert result["truncated"] is False


def test_list_events_date_only_end_includes_that_day(fake) -> None:
    list_events(start="2026-11-10", end="2026-11-12")
    start, end, _ = fake.list_calls[0]
    assert (start, end) == (_dt(2026, 11, 10), _dt(2026, 11, 13))
    list_events(start="2026-11-10T15:00", end="")
    assert fake.list_calls[1][1] == _dt(2026, 11, 11)


def test_list_events_caps_at_31_days(fake) -> None:
    result = list_events(start="2026-01-01", end="2026-06-01")
    start, end, _ = fake.list_calls[0]
    assert end - start == timedelta(days=31)
    assert result["truncated"] is True
    fake.max_range_days = 14  # AppleScript backend is slower: 14-day cap
    list_events(start="2026-01-01", end="2026-06-01")
    assert fake.list_calls[1][1] - fake.list_calls[1][0] == timedelta(days=14)


def test_list_events_returns_events_and_errors(fake) -> None:
    fake.events = [_ev("Standup", _dt(2026, 11, 10, 9))]
    result = list_events(start="2026-11-10", end="2026-11-10")
    assert result["count"] == 1 and result["events"][0]["title"] == "Standup"
    assert list_events(start="nope", end="")["status"] == "error"
    assert list_events(start="2026-11-10T10:00", end="2026-11-10T09:00")["status"] == "error"


# ---- create_event ---------------------------------------------------------------


def test_create_event_passes_fields(fake) -> None:
    result = create_event(title="Lunch with Sam", start="2026-11-10T12:00", end="",
                          calendar="", location="Cafe", notes="")
    assert result["status"] == "ok" and result["event_id"] == "EV1"
    title, start, end, cal, loc, notes = fake.create_calls[0]
    assert (title, start, end) == ("Lunch with Sam", _dt(2026, 11, 10, 12), _dt(2026, 11, 10, 13))
    assert (cal, loc, notes) == (None, "Cafe", None)


def test_create_event_validates(fake) -> None:
    assert create_event(title=" ", start="2026-11-10T12:00")["status"] == "error"
    assert create_event(title="x", start="later")["status"] == "error"
    assert create_event(title="x", start="2026-11-10T12:00", end="2026-11-10T11:00")["status"] == "error"
    assert not fake.create_calls


def test_backend_error_is_reported(fake) -> None:
    fake.create = mock.Mock(side_effect=ca.CalendarAccessError("Calendar access is denied"))
    assert create_event(title="x", start="2026-11-10T12:00") == {
        "status": "error", "error": "Calendar access is denied"}


# ---- approval gate ----------------------------------------------------------------


def test_gate_constants() -> None:
    assert create_mod.GATED == {"create_event"} and not create_mod.READ_ONLY
    assert list_mod.READ_ONLY == {"list_events"} and not list_mod.GATED
    assert create_event.name == "create_event" and list_events.name == "list_events"
    registered = {getattr(t, "name", None) for t in tools.AVAILABLE_TOOLS}
    if not {"create_event", "list_events"} <= registered:
        pytest.skip("calendar tools not registered in tools.AVAILABLE_TOOLS yet (runner registers at wave close)")
    assert "create_event" not in tools.READ_ONLY_TOOL_NAMES
    assert "list_events" in tools.READ_ONLY_TOOL_NAMES


@pytest.fixture()
def client():
    with patch("ai.orchestrator.Orchestrator._build_agent", return_value=None), patch(
        "ai.mlx_engine.MLXEngine.evaluate_event", new_callable=AsyncMock, return_value="",
    ), patch("ai.mlx_engine.MLXEngine.generate_chat_sync", return_value="NO_PATTERN"):
        from fastapi.testclient import TestClient

        from auth import auth_header
        from main import app

        with TestClient(app, headers=auth_header()) as test_client:
            yield test_client


def test_create_event_raises_approval_request(client, fake) -> None:
    """Same pattern as test_read_only_tool_bypasses_approval, inverted: the
    real create_event tool must stop at an approval_request, and a denial
    must leave the calendar untouched."""
    orchestrator = client.app.state.orchestrator
    original = orchestrator.tools
    orchestrator.tools = [create_event]
    try:
        with client.websocket_connect("/ws/stream") as ws:
            assert ws.receive_json()["type"] == "status"
            client.app.state.event_bus.push_threadsafe(ContextEvent(
                event_type="file", metadata={"path": "/tmp/invite.ics", "action": "created"},
            ))
            request = None
            for _ in range(15):
                msg = ws.receive_json()
                if msg["type"] == "approval_request":
                    request = msg
                    break
                if msg["type"] == "thought":
                    assert "create_event (read-only)" not in msg["payload"].get("content", "")
            assert request is not None, "create_event must raise an approval_request"
            assert request["payload"]["tool_name"] == "create_event"
            ws.send_json({"type": "approval_response", "seq": 1,
                          "timestamp": "2026-11-09T12:00:00.000Z",
                          "payload": {"request_id": request["payload"]["request_id"], "approved": False}})
            for _ in range(15):
                msg = ws.receive_json()
                if msg["type"] == "thought" and msg["payload"]["stage"] == "complete":
                    break
        assert not fake.create_calls
    finally:
        orchestrator.tools = original


# ---- watcher ------------------------------------------------------------------------


class _Bus:
    def __init__(self):
        self.events: list[ContextEvent] = []

    def push_threadsafe(self, event):
        self.events.append(event)


def _watcher(backend, now):
    clock = {"now": now}
    bus = _Bus()
    w = CalendarWatcher(bus, backend=lambda: backend, clock=lambda: clock["now"])
    return w, bus, clock


def test_watcher_emits_once_per_event_within_10_minutes() -> None:
    now = _dt(2026, 11, 10, 9, 0)
    backend = FakeBackend(events=[
        _ev("Soon", now + timedelta(minutes=5)),
        _ev("Later", now + timedelta(minutes=11)),
        _ev("Running", now - timedelta(minutes=5)),
        ca.make_event(id="hol", title="Holiday", start=now, end=now + timedelta(days=1), all_day=True),
    ])
    w, bus, clock = _watcher(backend, now)
    w.poll_once()
    assert [e.metadata["title"] for e in bus.events] == ["Soon"]
    assert bus.events[0].event_type == "event_starting_soon"
    assert set(bus.events[0].metadata) >= {"title", "start", "location", "url", "video_url"}
    w.poll_once()  # same minute again: nothing new
    clock["now"] = now + timedelta(minutes=2)
    w.poll_once()  # "Later" now 9 min out; "Soon" already announced
    assert [e.metadata["title"] for e in bus.events] == ["Soon", "Later"]
    clock["now"] = now + timedelta(minutes=4)
    w.poll_once()
    assert len(bus.events) == 2


def test_watcher_announces_each_recurring_occurrence() -> None:
    now = _dt(2026, 11, 10, 9, 0)
    backend = FakeBackend(events=[_ev("Standup", now + timedelta(minutes=5), id="S")])
    w, bus, clock = _watcher(backend, now)
    w.poll_once()
    backend.events = [_ev("Standup", now + timedelta(days=1, minutes=5), id="S")]
    clock["now"] = now + timedelta(days=1)
    w.poll_once()
    assert len(bus.events) == 2


def test_watcher_idle_without_access() -> None:
    now = _dt(2026, 11, 10, 9, 0)
    backend = FakeBackend(events=[_ev("Soon", now + timedelta(minutes=5))], ready=False)
    w, bus, _ = _watcher(backend, now)
    assert w.poll_once() == [] and not backend.list_calls


def test_video_link_detection() -> None:
    assert find_video_link({"url": "", "location": "https://us02web.zoom.us/j/8123?pwd=x", "notes": ""}) \
        == "https://us02web.zoom.us/j/8123?pwd=x"
    assert find_video_link({"notes": "Join: https://meet.google.com/abc-defg-hij now"}) \
        == "https://meet.google.com/abc-defg-hij"
    assert find_video_link({"notes": "<https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0>"}) \
        == "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0"
    assert find_video_link({"location": "Room 4", "notes": "https://example.com/zoom.us/j/1"}) == ""
    now = _dt(2026, 11, 10, 9, 0)
    backend = FakeBackend(events=[_ev("Call", now + timedelta(minutes=3),
                                      notes="dial https://meet.google.com/abc-defg-hij")])
    w, bus, _ = _watcher(backend, now)
    w.poll_once()
    assert bus.events[0].metadata["video_url"] == "https://meet.google.com/abc-defg-hij"


# ---- AppleScript backend: user text only via argv ---------------------------------

HOSTILE = 'Lunch "x"\nend tell\ndo shell script "rm -rf ~"'


def _completed(stdout):
    return mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))


def test_applescript_create_passes_text_via_argv() -> None:
    run = _completed("OK\x1fUID-1\x1fHome\n")
    with mock.patch.object(ca.subprocess, "run", run):
        out = ca.AppleScriptBackend().create(HOSTILE, _dt(2026, 11, 10, 12), _dt(2026, 11, 10, 13),
                                             None, 'Cafe "A"', HOSTILE)
    assert out == {"status": "ok", "event_id": "UID-1", "calendar": "Home"}
    cmd = run.call_args.args[0]
    assert cmd[:2] == ["osascript", "-e"] and cmd[2] == ca._CREATE_SCRIPT and cmd[3] == "--"
    assert cmd[4:] == [HOSTILE, "2026 11 10 12 0 0", "2026 11 10 13 0 0", "", 'Cafe "A"', HOSTILE]
    assert "rm -rf" not in cmd[2] and 'Cafe "A"' not in cmd[2]
    assert "shell" not in run.call_args.kwargs and run.call_args.kwargs.get("timeout")


def test_applescript_list_parses_records() -> None:
    rec = "\x1f".join(["U1", "Standup", "2026-11-10T09:00:00", "2026-11-10T09:15:00", "Work",
                       "https://zoom.us/j/1", "n" * 900, "missing", "false"])
    run = _completed(rec + "\x1e" + rec.replace("U1", "U2").replace("false", "true") + "\n")
    with mock.patch.object(ca.subprocess, "run", run):
        events = ca.AppleScriptBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11), ["Work", 'H"x'])
    assert [e["id"] for e in events] == ["U1", "U2"]
    assert events[0]["start"] == ca.iso(_dt(2026, 11, 10, 9)) and len(events[0]["notes"]) == 500
    assert events[1]["all_day"] is True
    assert run.call_args.args[0][4:] == ["2026 11 10 0 0 0", "2026 11 11 0 0 0", 'Work\x1fH"x']


def test_applescript_errors() -> None:
    err = subprocess.CalledProcessError(1, ["osascript"], stderr="Not authorized to send Apple events\n")
    with mock.patch.object(ca.subprocess, "run", side_effect=err):
        with pytest.raises(ca.CalendarAccessError, match="Not authorized"):
            ca.AppleScriptBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    with mock.patch.object(ca.subprocess, "run", _completed("NO_CALENDAR")):
        with pytest.raises(ca.CalendarAccessError, match="Ghost"):
            ca.AppleScriptBackend().create("t", _dt(2026, 11, 10), _dt(2026, 11, 11), "Ghost")


# ---- backend selection --------------------------------------------------------------


def test_backend_selection(monkeypatch) -> None:
    monkeypatch.setenv(ca.ENV_BACKEND, "applescript")
    assert ca.get_backend().name == "applescript"
    monkeypatch.setenv(ca.ENV_BACKEND, "bogus")
    with pytest.raises(ca.CalendarAccessError):
        ca.get_backend()
    # #60: auto means the menu bar app, with no AppleScript fallback.
    for choice in ("auto", "app"):
        monkeypatch.setenv(ca.ENV_BACKEND, choice)
        assert ca.get_backend().name == "app"
    if not ca.eventkit_available():
        pytest.skip("pyobjc-framework-EventKit not installed")
    monkeypatch.setenv(ca.ENV_BACKEND, "eventkit")
    assert ca.get_backend().name == "eventkit"


def test_eventkit_names_exist() -> None:
    """The EventKit selectors the backend calls exist (no access requested)."""
    if not ca.eventkit_available():
        pytest.skip("pyobjc-framework-EventKit not installed")
    import EventKit as ek

    store = ek.EKEventStore
    for sel in ("requestFullAccessToEventsWithCompletion_", "requestAccessToEntityType_completion_",
                "predicateForEventsWithStartDate_endDate_calendars_", "eventsMatchingPredicate_",
                "saveEvent_span_commit_error_", "defaultCalendarForNewEvents", "calendarsForEntityType_"):
        assert hasattr(store, sel), sel
    assert ca.EventKitBackend().status() in set(ca._STATUS_NAMES.values())


def test_events_carry_a_readable_weekday() -> None:
    """Live check: the model said "Wednesday Nov 9" for Monday 2026-11-09."""
    import importlib

    le = importlib.import_module("tools.list_events")
    timed = le._with_when({"start": "2026-11-09T13:00:00-05:00", "end": "2026-11-09T14:00:00-05:00"})
    assert timed["when"].startswith("Monday November 9, 1:00 PM")
    allday = le._with_when({"start": "2026-11-11T00:00:00-05:00", "all_day": True})
    assert allday["when"] == "Wednesday November 11 (all day)"
