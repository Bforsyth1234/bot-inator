"""Tests for the action journal and undo (#83).

Files are real (``tmp_path``); calendars and reminders use fake backends
behind the real ``tools.calendar_access`` functions; Notes' ``osascript``
is mocked; the app backend talks to a fake app on a real event loop.
"""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
import tools
from ai import action_journal as aj
from ai.action_journal import ActionJournal, is_undoable
from ai.idea_outcomes import OutcomeStore
from routes import actions as actions_routes
from schemas.ws_messages import ApprovalRequestPayload, CalendarRequestPayload
from tools import calendar_access as ca
from tools import notes
from tools.create_event import create_event
from tools.move_file import move_file
from tools.reminders import create_reminder

DAY = 24 * 3600.0
TZ = timezone(timedelta(hours=-5))


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def outcomes(clock) -> OutcomeStore:
    return OutcomeStore(None, clock=clock)


@pytest.fixture()
def journal(tmp_path, clock, outcomes) -> ActionJournal:
    j = ActionJournal(tmp_path / "actions.db", outcomes=outcomes, clock=clock)
    yield j
    j.close()


# ---- fake calendar / reminders backend -----------------------------------------


class FakeCalendar:
    """Events and reminders in memory, with the #83 get/delete ops."""

    name = "fake"
    max_range_days = 31

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.reminders: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self._n = 0

    def ready(self) -> bool:
        return True

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def create(self, title, start, end, calendar=None, location=None, notes=None):
        eid = self._id("EV")
        self.events[eid] = ca.make_event(
            id=eid, title=title, start=start, end=end, calendar=calendar or "Home",
            location=location or "", notes=notes or "")
        return {"status": "ok", "event_id": eid, "calendar": calendar or "Home"}

    def list(self, start, end, calendars=None):
        return list(self.events.values())

    def get(self, event_id):
        ev = self.events.get(event_id)
        return dict(ev) if ev else None

    def delete(self, event_id):
        if event_id not in self.events:
            raise ca.CalendarAccessError("no such event")
        self.deleted.append(event_id)
        del self.events[event_id]
        return {"status": "ok"}

    def reminder_lists(self):
        return ["Inbox"]

    def list_reminders(self, lists=None, include_completed=False):
        return list(self.reminders.values())

    def create_reminder(self, title, due=None, all_day=False, list_name=None, notes=None):
        rid = self._id("RM")
        self.reminders[rid] = ca.make_reminder(
            id=rid, title=title, due=due, all_day=all_day, list=list_name or "Inbox",
            notes=notes or "", completed=False)
        return {"status": "ok", "reminder_id": rid, "list": list_name or "Inbox"}

    def get_reminder(self, reminder_id):
        r = self.reminders.get(reminder_id)
        return dict(r) if r else None

    def delete_reminder(self, reminder_id):
        self.deleted.append(reminder_id)
        del self.reminders[reminder_id]
        return {"status": "ok"}


@pytest.fixture()
def cal():
    fake = FakeCalendar()
    ca.set_backend(fake)
    ca.set_reminder_backend(fake)
    yield fake
    ca.set_backend(None)
    ca.set_reminder_backend(None)


def _event(title="Lunch with Sam") -> dict[str, Any]:
    return create_event(title=title, start="2026-11-12T12:00", end="2026-11-12T13:00")


# ---- move_file -------------------------------------------------------------------


@pytest.fixture()
def moved(tmp_path, journal):
    src_dir, dst_dir = tmp_path / "Downloads", tmp_path / "Invoices"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "invoice.pdf"
    src.write_bytes(b"%PDF-1.4 invoice")
    result = move_file(str(src), str(dst_dir))
    assert result["status"] == "ok"
    entry = journal.record("move_file", {"src": str(src), "dst_dir": str(dst_dir)}, result,
                           event_id="evt_1", why="file_created")
    return SimpleNamespace(src=src, new=dst_dir / "invoice.pdf", entry=entry, result=result)


def test_approved_move_is_journaled_and_undo_moves_it_back(moved, journal) -> None:
    entry = moved.entry
    assert entry["tool"] == "move_file" and entry["undoable"] and entry["can_undo"]
    assert entry["why"] == "file_created" and entry["event_id"] == "evt_1"
    assert "invoice.pdf" in entry["label"]
    assert journal.rows()[0]["id"] == entry["id"]

    out = journal.undo(entry["id"])
    assert out["status"] == "undone", out
    assert moved.src.read_bytes() == b"%PDF-1.4 invoice"
    assert not moved.new.exists()
    assert out["entry"]["can_undo"] is False and out["entry"]["undone_at"] is not None
    # A second click does nothing.
    assert journal.undo(entry["id"])["status"] == "already_undone"
    assert [r["status"] for r in journal.undo_log(entry["id"])] == ["undone", "already_undone"]


def test_undo_refused_when_the_file_was_edited(moved, journal) -> None:
    moved.new.write_bytes(b"%PDF-1.4 invoice, annotated")
    out = journal.undo(moved.entry["id"])
    assert out["status"] == "changed" and "not undone" in out["message"]
    assert moved.new.exists() and not moved.src.exists()


def test_undo_refused_when_only_the_mtime_changed(moved, journal) -> None:
    st = moved.new.stat()
    os.utime(moved.new, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert journal.undo(moved.entry["id"])["status"] == "changed"
    assert moved.new.exists() and not moved.src.exists()


def test_undo_refused_when_old_path_is_taken(moved, journal) -> None:
    moved.src.write_bytes(b"a new download with the same name")
    out = journal.undo(moved.entry["id"])
    assert out["status"] == "changed" and "taken" in out["message"]
    assert moved.src.read_bytes() == b"a new download with the same name"
    assert moved.new.read_bytes() == b"%PDF-1.4 invoice"


def test_undo_refused_when_the_file_moved_on(moved, journal, tmp_path) -> None:
    moved.new.rename(tmp_path / "elsewhere.pdf")
    out = journal.undo(moved.entry["id"])
    assert out["status"] == "changed"
    assert not moved.src.exists()
    # Still offered: the user might put it back.
    assert journal.get(moved.entry["id"])["can_undo"] is True


# ---- events, reminders ------------------------------------------------------------


def test_created_event_is_deleted_by_its_journaled_id_only(cal, journal) -> None:
    mine = _event()
    other = cal.create("Dentist", datetime(2026, 11, 12, 9, tzinfo=TZ),
                       datetime(2026, 11, 12, 10, tzinfo=TZ))["event_id"]
    entry = journal.record("create_event", {"title": "Lunch with Sam"}, mine)
    assert entry["undoable"] and entry["label"] == "Added a calendar event in Home"
    out = journal.undo(entry["id"])
    assert out["status"] == "undone", out
    assert cal.deleted == [mine["event_id"]]
    assert other in cal.events and mine["event_id"] not in cal.events


def test_edited_event_is_not_deleted(cal, journal) -> None:
    mine = _event()
    entry = journal.record("create_event", {}, mine)
    cal.events[mine["event_id"]]["start"] = "2026-11-12T14:00:00-05:00"  # user moved it
    out = journal.undo(entry["id"])
    assert out["status"] == "changed" and cal.deleted == []
    assert mine["event_id"] in cal.events


def test_event_already_deleted_is_gone(cal, journal) -> None:
    mine = _event()
    entry = journal.record("create_event", {}, mine)
    del cal.events[mine["event_id"]]
    out = journal.undo(entry["id"])
    assert out["status"] == "gone" and cal.deleted == []
    assert out["entry"]["can_undo"] is False


def test_event_without_a_fingerprint_is_not_deleted(cal, journal, monkeypatch) -> None:
    mine = _event()
    monkeypatch.setattr(journal.ops, "event_fingerprint", mock.Mock(side_effect=ca.CalendarAccessError("app gone")))
    entry = journal.record("create_event", {}, mine)
    monkeypatch.undo()
    assert journal.undo(entry["id"])["status"] == "changed"
    assert cal.deleted == []


def test_created_reminder_is_undone_and_a_completed_one_is_not(cal, journal) -> None:
    first = create_reminder(title="Call the vet", due="2026-11-14")
    second = create_reminder(title="Buy milk")
    e1 = journal.record("create_reminder", {}, first)
    e2 = journal.record("create_reminder", {}, second)
    cal.reminders[second["reminder_id"]]["completed"] = True  # user checked it off
    assert journal.undo(e1["id"])["status"] == "undone"
    assert journal.undo(e2["id"])["status"] == "changed"
    assert cal.deleted == [first["reminder_id"]]


# ---- notes -----------------------------------------------------------------------


class FakeNotes:
    """Stands in for ``osascript`` running the fixed Notes scripts."""

    def __init__(self) -> None:
        self.bodies: dict[str, str] = {}
        self.deleted: list[str] = []

    def __call__(self, cmd, **kwargs):
        script, args = cmd[2], cmd[4:]
        if script == notes._CREATE_SCRIPT:
            nid = f"x-coredata://N/ICNote/p{len(self.bodies) + 1}"
            self.bodies[nid] = args[0]
            out = f"OK\x1f{nid}"
        elif script == notes._GET_SCRIPT:
            out = f"OK\n{self.bodies[args[0]]}" if args[0] in self.bodies else "NOT_FOUND"
        elif script == notes._DELETE_SCRIPT:
            out = "NOT_FOUND"
            if args[0] in self.bodies:
                del self.bodies[args[0]]
                self.deleted.append(args[0])
                out = "OK"
        else:
            raise AssertionError("unexpected script")
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")


def test_create_note_returns_its_id_and_undo_deletes_it(journal) -> None:
    fake = FakeNotes()
    with mock.patch.object(notes.subprocess, "run", fake):
        result = notes.create_note(title="Plan", body="one\ntwo")
        keep = notes.create_note(title="Other", body="x")
        assert result["note_id"] == "x-coredata://N/ICNote/p1"
        entry = journal.record("create_note", {}, result)
        out = journal.undo(entry["id"])
    assert out["status"] == "undone", out
    assert fake.deleted == [result["note_id"]]
    assert keep["note_id"] in fake.bodies


def test_edited_note_is_not_deleted(journal) -> None:
    fake = FakeNotes()
    with mock.patch.object(notes.subprocess, "run", fake):
        result = notes.create_note(title="Plan", body="one")
        entry = journal.record("create_note", {}, result)
        fake.bodies[result["note_id"]] += "<div>added by hand</div>"
        out = journal.undo(entry["id"])
    assert out["status"] == "changed" and fake.deleted == []


def test_note_from_an_old_script_reply_has_no_id_and_no_undo(journal) -> None:
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="OK", stderr=""))
    with mock.patch.object(notes.subprocess, "run", run):
        result = notes.create_note(title="Plan", body="one")
    assert result == {"status": "ok", "title": "Plan"}
    entry = journal.record("create_note", {}, result)
    assert entry["undoable"] is False and "without an id" in entry["not_undoable_reason"]


# ---- not undoable, reversible, no delete tool ----------------------------------------


@pytest.mark.parametrize("tool", ["run_shortcut", "open_url", "open_app", "show_notification",
                                  "my_generated_tool", "mcp__server__thing", "append_note"])
def test_not_undoable_tools_have_no_spec_and_are_not_reversible(journal, tool) -> None:
    assert is_undoable(tool) is False
    entry = journal.record(tool, {}, {"status": "ok"})
    assert entry["undoable"] is False and entry["can_undo"] is False
    assert entry["not_undoable_reason"]
    out = journal.undo(entry["id"])
    assert out["status"] == "not_undoable"


@pytest.mark.parametrize("tool", sorted(aj.UNDOABLE_TOOLS))
def test_undoable_tools_are_reversible(tool) -> None:
    assert is_undoable(tool) is True


def test_reversible_on_the_approval_request() -> None:
    base = dict(request_id="r", event_id="e", tool_name="move_file", reasoning="x")
    assert ApprovalRequestPayload(**base).reversible is False  # older daemons / default
    p = ApprovalRequestPayload(**base, reversible=True)
    assert ApprovalRequestPayload.model_validate_json(p.model_dump_json()).reversible is True


def test_calendar_request_accepts_the_undo_ops() -> None:
    for op in ("get", "delete"):
        assert CalendarRequestPayload(request_id="c", op=op, args={"event_id": "E"}).op == op


def test_the_agent_has_no_delete_tool() -> None:
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.AVAILABLE_TOOLS}
    assert names, "no tools registered?"
    assert not any("delete" in n or "remove" in n or "undo" in n for n in names), names
    for fn in (ca.delete_event, ca.delete_reminder, notes.delete_note, notes.note_fingerprint):
        assert not getattr(fn, "is_tool", False)
        assert fn.__name__ not in notes.GATED | tools.READ_ONLY_TOOL_NAMES


@pytest.mark.parametrize("result", [
    {"status": "error", "error": "src is not a file"},
    {"skipped": True, "reason": "user_denied"},
    None,
])
def test_failed_or_skipped_calls_are_not_journaled(journal, result) -> None:
    assert journal.record("move_file", {}, result) is None
    assert journal.rows() == []


# ---- outcomes + #78 hooks ----------------------------------------------------------


def test_undo_calls_record_undone(cal, journal, outcomes) -> None:
    offer_id = outcomes.record_offer("next_step", "create_event", "approved", one_shot=True)
    entry = journal.record("create_event", {}, _event(), offer_id=offer_id)
    assert journal.undo(entry["id"])["status"] == "undone"
    assert outcomes._find(offer_id).get("undone_soon") is True


def test_refused_undo_does_not_call_record_undone(cal, journal) -> None:
    journal.outcomes = mock.Mock()
    mine = _event()
    entry = journal.record("create_event", {}, mine, offer_id="off_1")
    cal.events[mine["event_id"]]["title"] = "Lunch with Sam and Alex"
    assert journal.undo(entry["id"])["status"] == "changed"
    journal.outcomes.record_undone.assert_not_called()


def test_rule_runs_count_undos_and_notify_listeners(cal, journal) -> None:
    seen: list[tuple[str, str]] = []
    journal.add_undo_listener(lambda entry, out: seen.append((entry["rule_id"], out["status"])))
    ids = [journal.record("create_event", {}, _event(), source="rule", rule_id="rule_7")["id"]
           for _ in range(2)]
    assert journal.undo_count("rule_7") == 0
    for entry_id in ids:
        journal.undo(entry_id)
    assert journal.undo_count("rule_7") == 2
    assert seen == [("rule_7", "undone"), ("rule_7", "undone")]
    assert journal.get(ids[0])["source"] == "rule"


# ---- storage -------------------------------------------------------------------------


def test_file_is_0600_and_persists(tmp_path, clock, moved) -> None:
    path = tmp_path / "actions.db"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reopened = ActionJournal(path, clock=clock)
    try:
        assert reopened.get(moved.entry["id"])["can_undo"] is True
        assert reopened.undo(moved.entry["id"])["status"] == "undone"
    finally:
        reopened.close()


def test_no_titles_or_bodies_are_stored(tmp_path, cal, journal) -> None:
    journal.record("create_event", {"title": "Secret surgery", "notes": "private"},
                   create_event(title="Secret surgery", start="2026-11-12T12:00", notes="private"))
    raw = (tmp_path / "actions.db").read_bytes()
    assert b"Secret" not in raw and b"private" not in raw


def test_entries_older_than_30_days_are_dropped(moved, journal, clock) -> None:
    clock.advance(29 * DAY)
    journal.prune()
    assert len(journal.rows()) == 1
    clock.advance(2 * DAY)
    journal.prune()
    assert journal.rows() == []
    assert journal.undo(moved.entry["id"])["status"] == "not_found"


# ---- app calendar backend: get / delete ops ----------------------------------------------


@pytest.fixture()
def loop():
    lp = asyncio.new_event_loop()
    thread = threading.Thread(target=lp.run_forever, daemon=True)
    thread.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    thread.join(timeout=5)
    lp.close()


def test_app_backend_get_and_delete(loop) -> None:
    bridge = ca.AppCalendarBridge()
    sent: list[tuple[str, dict]] = []
    event = {"id": "E9", "title": "Lunch", "start": "2026-11-10T12:00:00+00:00",
             "end": "2026-11-10T13:00:00+00:00", "calendar": "Home", "location": "",
             "notes": "", "url": "", "all_day": False}

    async def send(request_id, op, args, timeout_seconds):
        sent.append((op, args))
        found = [event] if args.get("event_id") == "E9" else []
        reply = {"request_id": request_id, "ok": True, "events": found}
        asyncio.get_running_loop().call_later(0.01, bridge.submit_response, "ws", reply)

    bridge.connect("ws", send, loop)
    bridge.set_status("ws", "full_access")
    backend = ca.AppCalendarBackend(bridge)
    assert backend.get("E9")["title"] == "Lunch"
    assert backend.get("nope") is None
    assert backend.delete("E9") == {"status": "ok"}
    assert sent == [("get", {"event_id": "E9"}), ("get", {"event_id": "nope"}),
                    ("delete", {"event_id": "E9"})]


def test_fingerprint_ignores_fields_outside_the_item() -> None:
    a = {"title": "T", "start": "s", "end": "e", "attendees": [1]}
    b = {"title": "T", "start": "s", "end": "e", "attendees": [2]}
    assert ca.fingerprint(a) == ca.fingerprint(b) != ca.fingerprint({**a, "title": "U"})
    assert ca.fingerprint(None) is None


# ---- routes ------------------------------------------------------------------------------


@pytest.fixture()
def api(journal, moved):
    app = FastAPI()
    app.include_router(actions_routes.router)
    app.add_middleware(auth.AuthMiddleware)
    app.state.action_journal = journal
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, moved=moved)


def test_list_and_undo_routes(api) -> None:
    h = auth.auth_header()
    rows = api.client.get("/api/actions", headers=h).json()
    assert [r["id"] for r in rows] == [api.moved.entry["id"]] and rows[0]["can_undo"]
    r = api.client.post(f"/api/actions/{api.moved.entry['id']}/undo", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "undone"
    assert api.moved.src.exists()
    again = api.client.post(f"/api/actions/{api.moved.entry['id']}/undo", headers=h)
    assert again.status_code == 409 and again.json()["status"] == "already_undone"
    missing = api.client.post("/api/actions/act_nope/undo", headers=h)
    assert missing.status_code == 404


@pytest.mark.parametrize("method,path", [("get", "/api/actions"),
                                         ("post", "/api/actions/x/undo")])
def test_routes_need_the_token(api, method, path) -> None:
    assert getattr(api.client, method)(path).status_code == 401
    assert api.moved.new.exists()


def test_routes_503_when_not_wired() -> None:
    app = FastAPI()
    app.include_router(actions_routes.router)
    with TestClient(app) as client:
        assert client.get("/api/actions").status_code == 503
