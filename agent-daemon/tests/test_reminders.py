"""Tests for the Reminders tools (#45). No real Reminders access: every test
uses a fake backend, a fake EventKit store, or a mocked ``osascript``."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import ai  # noqa: E402,F401  - import order: ai -> tools avoids a circular import
import importlib  # noqa: E402

import tools  # noqa: E402
from events.event_bus import ContextEvent  # noqa: E402
from tools import calendar_access as ca  # noqa: E402

rem_mod = importlib.import_module("tools.reminders")  # module, not a registered tool
list_reminders = rem_mod.list_reminders
create_reminder = rem_mod.create_reminder

TZ = datetime.now().astimezone().tzinfo


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


class FakeReminders:
    name = "fake"

    def __init__(self, items=None):
        self.items = items or []
        self.list_calls: list[tuple] = []
        self.create_calls: list[tuple] = []

    def reminder_lists(self):
        return sorted({r["list"] for r in self.items})

    def list_reminders(self, lists=None, include_completed=False):
        self.list_calls.append((lists, include_completed))
        out = [r for r in self.items
               if (not lists or r["list"] in lists) and (include_completed or not r["completed"])]
        return ca.sort_reminders(out)

    def create_reminder(self, title, due=None, all_day=False, list_name=None, notes=None):
        self.create_calls.append((title, due, all_day, list_name, notes))
        return {"status": "ok", "reminder_id": "R1", "list": list_name or "Reminders"}


@pytest.fixture()
def fake():
    backend = FakeReminders([
        ca.make_reminder(id="1", title="Buy milk", list="Groceries"),
        ca.make_reminder(id="2", title="Send deck", due=_dt(2026, 11, 14), all_day=True, list="Work"),
        ca.make_reminder(id="3", title="Call vet", due=_dt(2026, 11, 10, 9), list="Reminders"),
        ca.make_reminder(id="4", title="Old task", list="Work", completed=True),
    ])
    ca.set_reminder_backend(backend)
    yield backend
    ca.set_reminder_backend(None)


# ---- list_reminders --------------------------------------------------------------


def test_list_reminders_open_only_sorted(fake) -> None:
    result = list_reminders()
    assert result["status"] == "ok" and result["truncated"] is False
    assert [r["title"] for r in result["reminders"]] == ["Call vet", "Send deck", "Buy milk"]
    assert fake.list_calls[0] == (None, False)
    send = result["reminders"][1]
    assert send["due"] == "2026-11-14" and send["all_day"] is True
    assert result["reminders"][0]["due"] == ca.iso(_dt(2026, 11, 10, 9))


def test_list_reminders_filters(fake) -> None:
    work = list_reminders(list_name="Work")
    assert [r["id"] for r in work["reminders"]] == ["2"] and fake.list_calls[0] == (["Work"], False)
    every = list_reminders(list_name=" Work ", include_completed=True)
    assert {r["id"] for r in every["reminders"]} == {"2", "4"}
    assert fake.list_calls[1] == (["Work"], True)


def test_list_reminders_caps_and_errors(fake) -> None:
    fake.items = [ca.make_reminder(id=str(i), title=f"t{i}") for i in range(rem_mod.MAX_RESULTS + 5)]
    result = list_reminders()
    assert result["count"] == rem_mod.MAX_RESULTS and result["truncated"] is True
    fake.list_reminders = mock.Mock(side_effect=ca.CalendarAccessError("Reminders access is denied"))
    assert list_reminders() == {"status": "error", "error": "Reminders access is denied"}


# ---- create_reminder / due parsing ------------------------------------------------


def test_parse_due() -> None:
    assert rem_mod.parse_due("") == (None, False)
    assert rem_mod.parse_due("2026-11-10T09:00") == (_dt(2026, 11, 10, 9), False)
    assert rem_mod.parse_due("2026-11-10") == (_dt(2026, 11, 10), True)
    for bad in ("tomorrow at 9", "2026-13-01", "11/10/2026"):
        with pytest.raises(ValueError):
            rem_mod.parse_due(bad)


def test_create_reminder_with_time(fake) -> None:
    result = create_reminder(title="Call the vet", due="2026-11-10T09:00", list_name="", notes="")
    assert result == {"status": "ok", "reminder_id": "R1", "list": "Reminders",
                      "title": "Call the vet", "due": ca.iso(_dt(2026, 11, 10, 9)), "all_day": False}
    assert fake.create_calls[0] == ("Call the vet", _dt(2026, 11, 10, 9), False, None, None)


def test_create_reminder_date_only_is_all_day(fake) -> None:
    result = create_reminder(title="Send the deck", due="2026-11-14", list_name="Work", notes="for Sam")
    assert result["due"] == "2026-11-14" and result["all_day"] is True and result["list"] == "Work"
    assert fake.create_calls[0] == ("Send the deck", _dt(2026, 11, 14), True, "Work", "for Sam")


def test_create_reminder_no_due(fake) -> None:
    result = create_reminder(title="Buy stamps")
    assert result["due"] == "" and result["all_day"] is False
    assert fake.create_calls[0] == ("Buy stamps", None, False, None, None)


def test_create_reminder_validates(fake) -> None:
    assert create_reminder(title=" ")["status"] == "error"
    bad = create_reminder(title="x", due="next Friday")
    assert bad["status"] == "error" and "Bad due date" in bad["error"]
    assert not fake.create_calls
    fake.create_reminder = mock.Mock(side_effect=ca.CalendarAccessError("No reminders list named 'X'."))
    assert create_reminder(title="x", list_name="X") == {
        "status": "error", "error": "No reminders list named 'X'."}


# ---- approval gate ------------------------------------------------------------------


def test_gate_constants() -> None:
    assert rem_mod.READ_ONLY == {"list_reminders"} and rem_mod.GATED == {"create_reminder"}
    assert list_reminders.name == "list_reminders" and create_reminder.name == "create_reminder"
    registered = {getattr(t, "name", None) for t in tools.AVAILABLE_TOOLS}
    if not {"list_reminders", "create_reminder"} <= registered:
        pytest.skip("reminder tools not registered in tools.AVAILABLE_TOOLS yet (runner registers at wave close)")
    assert "create_reminder" not in tools.READ_ONLY_TOOL_NAMES
    assert "list_reminders" in tools.READ_ONLY_TOOL_NAMES


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


def test_create_reminder_raises_approval_request(client, fake) -> None:
    """The real create_reminder tool must stop at an approval_request, and a
    denial must leave Reminders untouched (pattern of test_calendar)."""
    orchestrator = client.app.state.orchestrator
    original = orchestrator.tools
    orchestrator.tools = [create_reminder]
    try:
        with client.websocket_connect("/ws/stream") as ws:
            assert ws.receive_json()["type"] == "status"
            client.app.state.event_bus.push_threadsafe(ContextEvent(
                event_type="file", metadata={"path": "/tmp/todo.txt", "action": "created"},
            ))
            request = None
            for _ in range(15):
                msg = ws.receive_json()
                if msg["type"] == "approval_request":
                    request = msg
                    break
            assert request is not None, "create_reminder must raise an approval_request"
            assert request["payload"]["tool_name"] == "create_reminder"
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


# ---- EventKit: bounded wait on the asynchronous fetch ---------------------------------


class _Store:
    def __init__(self, reply=True):
        self.reply = reply
        self.cancelled = None

    def fetchRemindersMatchingPredicate_completion_(self, pred, completion):
        if self.reply:
            threading.Timer(0.05, completion, args=(["a", "b"],)).start()
        return "FETCH-1"

    def cancelFetchRequest_(self, ident):
        self.cancelled = ident


def test_eventkit_fetch_waits_for_completion() -> None:
    assert ca.EventKitBackend._fetch_reminders(_Store(), "pred", timeout=5) == ["a", "b"]


def test_eventkit_fetch_times_out_and_cancels() -> None:
    store = _Store(reply=False)
    with pytest.raises(ca.CalendarAccessError, match="timed out"):
        ca.EventKitBackend._fetch_reminders(store, "pred", timeout=0.1)
    assert store.cancelled == "FETCH-1"


def test_eventkit_due_components_round_trip() -> None:
    if not ca.eventkit_available():
        pytest.skip("pyobjc-framework-EventKit not installed")
    backend = ca.EventKitBackend()
    for due, all_day in ((_dt(2026, 11, 10, 9, 30), False), (_dt(2026, 11, 14), True)):
        comps = backend._due_components(due, all_day)
        rem = mock.Mock(dueDateComponents=mock.Mock(return_value=comps))
        assert backend._due_of(rem) == (due, all_day)
    assert backend._due_of(mock.Mock(dueDateComponents=mock.Mock(return_value=None))) == (None, False)


def test_eventkit_reminder_names_exist() -> None:
    """The EventKit selectors the reminder code calls exist (no access requested)."""
    if not ca.eventkit_available():
        pytest.skip("pyobjc-framework-EventKit not installed")
    import EventKit as ek

    store = ek.EKEventStore
    for sel in ("requestFullAccessToRemindersWithCompletion_", "fetchRemindersMatchingPredicate_completion_",
                "cancelFetchRequest_", "predicateForRemindersInCalendars_",
                "predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_",
                "saveReminder_commit_error_", "defaultCalendarForNewReminders"):
        assert hasattr(store, sel), sel
    assert hasattr(ek.EKReminder, "reminderWithEventStore_")
    assert hasattr(ek.EKAlarm, "alarmWithAbsoluteDate_")
    assert ca.EventKitBackend().status("reminder") in set(ca._STATUS_NAMES.values())


# ---- AppleScript backend: user text only via argv ---------------------------------------

HOSTILE = 'Call "vet"\nend tell\ndo shell script "rm -rf ~"'


def _completed(stdout):
    return mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))


def test_applescript_create_reminder_via_argv() -> None:
    run = _completed("OK\x1fx-apple-reminder://R9\x1fWork\n")
    with mock.patch.object(ca.subprocess, "run", run):
        out = ca.AppleScriptBackend().create_reminder(HOSTILE, _dt(2026, 11, 10, 9), False, "Work", HOSTILE)
    assert out == {"status": "ok", "reminder_id": "x-apple-reminder://R9", "list": "Work"}
    cmd = run.call_args.args[0]
    assert cmd[:2] == ["osascript", "-e"] and cmd[2] == ca._REMINDERS_CREATE_SCRIPT and cmd[3] == "--"
    assert cmd[4:] == [HOSTILE, "Work", HOSTILE, "2026 11 10 9 0 0", "false"]
    assert "rm -rf" not in cmd[2] and run.call_args.kwargs.get("timeout")
    with mock.patch.object(ca.subprocess, "run", run):
        ca.AppleScriptBackend().create_reminder("t", _dt(2026, 11, 14), True, None, None)
        assert run.call_args.args[0][4:] == ["t", "", "", "2026 11 14 0 0 0", "true"]
        ca.AppleScriptBackend().create_reminder("t")
        assert run.call_args.args[0][4:] == ["t", "", "", "", "false"]


def test_applescript_list_reminders_parses_records() -> None:
    timed = "\x1f".join(["R1", "Call vet", "2026-11-10T09:00:00", "", "Reminders", "", "false"])
    allday = "\x1f".join(["R2", "Send deck", "2026-11-14T00:00:00", "2026-11-14T00:00:00", "Work",
                          "n" * 900, "false"])
    nodue = "\x1f".join(["R3", "Buy milk", "", "", "Groceries", "", "true"])
    run = _completed("\x1e".join([nodue, allday, timed]) + "\n")
    with mock.patch.object(ca.subprocess, "run", run):
        items = ca.AppleScriptBackend().list_reminders(['W"x'], True)
    assert [r["id"] for r in items] == ["R1", "R2", "R3"]
    assert items[0]["due"] == ca.iso(_dt(2026, 11, 10, 9)) and items[0]["all_day"] is False
    assert items[1]["due"] == "2026-11-14" and items[1]["all_day"] is True
    assert len(items[1]["notes"]) == 500 and items[2]["completed"] is True
    assert run.call_args.args[0][4:] == ['W"x', "true"]
    with mock.patch.object(ca.subprocess, "run", _completed("NO_LIST")):
        with pytest.raises(ca.CalendarAccessError, match="Ghost"):
            ca.AppleScriptBackend().list_reminders(["Ghost"])


# ---- backend selection -----------------------------------------------------------------


def test_reminder_backend_selection(monkeypatch) -> None:
    monkeypatch.delenv(ca.ENV_REMINDERS_BACKEND, raising=False)
    monkeypatch.setenv(ca.ENV_BACKEND, "applescript")  # falls back to the calendar pin
    assert ca.get_reminder_backend().name == "applescript"
    monkeypatch.setenv(ca.ENV_REMINDERS_BACKEND, "bogus")
    with pytest.raises(ca.CalendarAccessError):
        ca.get_reminder_backend()
    if not ca.eventkit_available():
        pytest.skip("pyobjc-framework-EventKit not installed")
    monkeypatch.setenv(ca.ENV_REMINDERS_BACKEND, "auto")
    statuses = {"event": "full_access", "reminder": "denied"}
    monkeypatch.setattr(ca.EventKitBackend, "status", lambda self, entity="event": statuses[entity])
    assert ca.get_reminder_backend().name == "applescript"  # reminders denied, calendar fine
    statuses["reminder"] = "not_determined"
    assert ca.get_reminder_backend().name == "eventkit"
