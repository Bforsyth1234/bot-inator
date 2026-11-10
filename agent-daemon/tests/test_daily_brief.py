"""Tests for events.daily_brief and routes.daily_brief (todo #65)."""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
import tools
import user_settings
from ai import privacy
from events import daily_brief as db
from events.daily_brief import DailyBriefScheduler, gather_brief
from events.event_bus import EventBus
from routes import daily_brief as routes_db
from tools import calendar_access

TZ = timezone(timedelta(hours=-7))


def at(hour: int, minute: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class FakeBus:
    def __init__(self) -> None:
        self.events = []
        self.fail = False

    def push_threadsafe(self, event) -> None:
        if self.fail:
            raise RuntimeError("no loop")
        self.events.append(event)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_scheduler(tmp_path, clock, bus=None, *, enabled=True, when=dtime(8, 0), gather=None):
    bus = bus or FakeBus()
    scheduler = DailyBriefScheduler(
        bus,
        state_path=tmp_path / "state" / "daily_brief.json",
        clock=clock,
        gather=gather or (lambda now: {"date": now.date().isoformat(), "text": "brief"}),
        enabled=(enabled if callable(enabled) else (lambda: enabled)),
        time_of_day=(when if callable(when) else (lambda: when)),
    )
    return scheduler, bus


# ---- scheduler -----------------------------------------------------------------

def test_fires_once_per_day(tmp_path):
    clock = Clock(at(7, 59))
    scheduler, bus = make_scheduler(tmp_path, clock)
    assert scheduler.check_once() is None  # before the set time
    clock.now = at(8, 0)
    event = scheduler.check_once()
    assert event is not None and event.event_type == "daily_brief"
    assert event.metadata["trigger"] == "schedule"
    assert event.metadata["date"] == "2026-11-10"
    for minute in (0, 1, 30):
        clock.now = at(8, minute)
        assert scheduler.check_once() is None
    clock.now = at(23, 59)
    assert scheduler.check_once() is None
    assert len(bus.events) == 1
    # The next day it fires again, once.
    clock.now = at(7, 30, day=11)
    assert scheduler.check_once() is None
    clock.now = at(8, 0, day=11)
    assert scheduler.check_once() is not None
    assert scheduler.check_once() is None
    assert len(bus.events) == 2


def test_does_not_fire_again_after_restart_same_day(tmp_path):
    clock = Clock(at(8, 5))
    first, bus = make_scheduler(tmp_path, clock)
    assert first.check_once() is not None
    saved = json.loads((tmp_path / "state" / "daily_brief.json").read_text())
    assert saved["last_run"].startswith("2026-11-10T08:05")
    # "Restart": a new scheduler on the same state file.
    clock.now = at(9, 0)
    second, _ = make_scheduler(tmp_path, clock, bus)
    assert second.check_once() is None
    assert len(bus.events) == 1


def test_catches_up_after_missed_time_same_day_only(tmp_path):
    # Ran on the 8th; asleep through all of the 9th and past 08:00 on the 10th.
    clock = Clock(at(8, 0, day=8))
    scheduler, bus = make_scheduler(tmp_path, clock)
    assert scheduler.check_once() is not None
    clock.now = at(13, 20, day=10)
    woke, _ = make_scheduler(tmp_path, clock, bus)
    event = woke.check_once()
    assert event is not None
    assert event.metadata["date"] == "2026-11-10"  # today's, not the 9th's
    assert woke.check_once() is None  # one catch-up, not one per missed day
    assert len(bus.events) == 2


def test_missed_time_does_not_fire_before_todays_time(tmp_path):
    clock = Clock(at(8, 0, day=8))
    scheduler, bus = make_scheduler(tmp_path, clock)
    scheduler.check_once()
    clock.now = at(6, 0, day=10)  # the 9th was missed, today's time not reached
    assert scheduler.check_once() is None
    assert len(bus.events) == 1


def test_disabled_setting_means_no_event(tmp_path):
    clock = Clock(at(9, 0))
    gathered = []
    scheduler, bus = make_scheduler(
        tmp_path, clock, enabled=False,
        gather=lambda now: gathered.append(now) or {"text": ""},
    )
    assert scheduler.check_once() is None
    assert bus.events == [] and gathered == []
    assert not (tmp_path / "state" / "daily_brief.json").exists()


def test_settings_file_drives_enabled_and_time(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    monkeypatch.delenv(db.ENABLED_KEY, raising=False)
    monkeypatch.delenv(db.TIME_KEY, raising=False)
    assert db.brief_enabled() is True and db.brief_time() == dtime(8, 0)  # defaults

    clock = Clock(at(9, 0))
    bus = FakeBus()
    scheduler = DailyBriefScheduler(
        bus, state_path=tmp_path / "daily_brief.json", clock=clock,
        gather=lambda now: {"text": "brief"},
    )
    user_settings.write({db.ENABLED_KEY: "0"})
    assert scheduler.check_once() is None
    user_settings.write({db.ENABLED_KEY: "1", db.TIME_KEY: "09:30"})
    assert db.brief_time() == dtime(9, 30)
    assert scheduler.check_once() is None  # 09:00 < 09:30
    clock.now = at(9, 30)
    assert scheduler.check_once() is not None
    user_settings.write({db.TIME_KEY: "nonsense"})
    assert db.brief_time() == dtime(8, 0)  # bad value falls back to the default


def test_moving_the_time_later_the_same_day_arms_it_again(tmp_path):
    clock = Clock(at(8, 0))
    when = {"t": dtime(8, 0)}
    scheduler, bus = make_scheduler(tmp_path, clock, when=lambda: when["t"])
    assert scheduler.check_once() is not None
    when["t"] = dtime(10, 2)  # the live check: "set the time 2 minutes ahead"
    clock.now = at(10, 0)
    assert scheduler.check_once() is None
    clock.now = at(10, 2)
    assert scheduler.check_once() is not None
    when["t"] = dtime(7, 0)  # moving it earlier does not replay it
    assert scheduler.check_once() is None
    assert len(bus.events) == 2


def test_failed_push_is_retried_not_lost(tmp_path):
    clock = Clock(at(8, 0))
    scheduler, bus = make_scheduler(tmp_path, clock)
    bus.fail = True
    with pytest.raises(RuntimeError):
        scheduler.check_once()
    bus.fail = False
    assert scheduler.check_once() is not None
    assert len(bus.events) == 1


def test_corrupt_state_file_is_treated_as_never_run(tmp_path):
    clock = Clock(at(8, 0))
    scheduler, bus = make_scheduler(tmp_path, clock)
    path = tmp_path / "state" / "daily_brief.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert scheduler.check_once() is not None
    assert scheduler.check_once() is None


def test_run_now_does_not_use_up_todays_brief(tmp_path):
    clock = Clock(at(7, 0))
    scheduler, bus = make_scheduler(tmp_path, clock)
    manual = scheduler.run_now()
    assert manual.metadata["trigger"] == "manual"
    clock.now = at(8, 0)
    assert scheduler.check_once() is not None
    assert [e.metadata["trigger"] for e in bus.events] == ["manual", "schedule"]


def test_start_stop_publishes_on_a_real_bus(tmp_path):
    async def run() -> object:
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        scheduler = DailyBriefScheduler(
            bus, state_path=tmp_path / "daily_brief.json", poll_interval=0.02,
            clock=Clock(at(8, 1)), gather=lambda now: {"text": "brief"},
            enabled=lambda: True, time_of_day=lambda: dtime(8, 0), startup_delay=0.0,
        )
        scheduler.start()
        scheduler.start()  # idempotent
        try:
            event = await asyncio.wait_for(bus.consume(), timeout=5)
            await asyncio.sleep(0.1)  # several more checks: still one event
        finally:
            await asyncio.to_thread(scheduler.stop)
        assert bus.qsize() == 0
        return event

    event = asyncio.run(run())
    assert event.event_type == "daily_brief"


# ---- gathering -----------------------------------------------------------------

def fake_events(start="", end=""):
    assert start == "2026-11-10" and end == ""
    return {"status": "ok", "events": [
        {"title": "Standup", "start": "2026-11-10T09:00:00-07:00",
         "end": "2026-11-10T09:30:00-07:00", "location": "Zoom", "all_day": False,
         "notes": "SECRET-NOTES"},
        {"title": "Offsite", "start": "2026-11-10T00:00:00-07:00",
         "end": "2026-11-11T00:00:00-07:00", "location": "", "all_day": True},
    ]}


def fake_mail(since):
    assert since == at(8, 0, day=9)  # the last 24 hours
    return {"status": "ok", "messages": [
        {"message_id": 41, "sender": "Alice <alice@example.com>",
         "subject": "Can you review the contract?", "date": "2026-11-10T01:00:00+00:00",
         "body": "SECRET-BODY"},
    ]}


def fake_reminders(list_name="", include_completed=False):
    assert list_name == "" and include_completed is False
    return {"status": "ok", "reminders": [
        {"title": "Pay rent", "due": "2026-11-08", "all_day": True, "list": "Home"},
        {"title": "Call dentist", "due": "2026-11-10T07:00:00-07:00", "list": "Home"},
        {"title": "Book flights", "due": "2026-11-10T17:00:00-07:00", "list": "Trips"},
        {"title": "Next week thing", "due": "2026-11-17", "all_day": True},
        {"title": "No date", "due": ""},
    ]}


def no_redact(subject: str) -> str:
    return subject


def test_payload_built_from_fake_tool_results():
    payload = gather_brief(
        at(8, 0), list_events=fake_events, recent_mail=fake_mail,
        list_reminders=fake_reminders, redact_subject=no_redact,
    )
    assert payload["date"] == "2026-11-10" and payload["day"] == "Tuesday November 10"
    assert payload["unavailable"] == []
    assert [e["title"] for e in payload["calendar"]["items"]] == ["Standup", "Offsite"]
    assert payload["calendar"]["items"][0]["when"] == "9:00 AM – 9:30 AM"
    assert payload["mail"]["items"] == [{
        "message_id": 41, "sender": "Alice <alice@example.com>",
        "subject": "Can you review the contract?", "date": "2026-11-10T01:00:00+00:00",
    }]
    due = {r["title"]: r for r in payload["reminders"]["items"]}
    assert set(due) == {"Pay rent", "Call dentist", "Book flights"}  # future / undated left out
    assert due["Pay rent"]["overdue"] and due["Call dentist"]["overdue"]
    assert not due["Book flights"]["overdue"]
    assert payload["text"] == "\n".join([
        "Brief for Tuesday November 10",
        "Calendar (2):",
        "- 9:00 AM – 9:30 AM: Standup (Zoom)",
        "- All day: Offsite",
        "Mail (1):",
        "- Alice <alice@example.com>: Can you review the contract? [id 41]",
        "Reminders (3):",
        "- Pay rent (overdue, since Sunday November 8)",
        "- Call dentist (overdue, today 7:00 AM)",
        "- Book flights (due today 5:00 PM)",
    ])
    # Bodies and notes never enter the payload.
    dumped = json.dumps(payload)
    assert "SECRET-BODY" not in dumped and "SECRET-NOTES" not in dumped
    assert db.build_brief_text(
        at(8, 0), list_events=fake_events, recent_mail=fake_mail,
        list_reminders=fake_reminders, redact_subject=no_redact,
    ) == payload["text"]


def test_empty_sources_say_so():
    payload = gather_brief(
        at(8, 0),
        list_events=lambda **k: {"status": "ok", "events": []},
        recent_mail=lambda since: {"status": "ok", "messages": []},
        list_reminders=lambda **k: {"status": "ok", "reminders": []},
    )
    assert payload["text"].splitlines()[1:] == [
        "Calendar: nothing today",
        "Mail: nothing new in the last 24 hours",
        "Reminders: nothing due",
    ]


def test_unavailable_source_is_named_not_dropped():
    def broken_mail(since):
        raise PermissionError("no Full Disk Access")

    payload = gather_brief(
        at(8, 0),
        list_events=lambda **k: {"status": "error", "error": calendar_access.APP_NOT_CONNECTED},
        recent_mail=broken_mail,
        list_reminders=lambda **k: "garbage",
        redact_subject=no_redact,
    )
    assert payload["calendar"] == {"status": "unavailable", "reason": calendar_access.APP_NOT_CONNECTED}
    assert payload["mail"] == {"status": "unavailable", "reason": "no Full Disk Access"}
    assert payload["reminders"]["status"] == "unavailable"
    assert payload["unavailable"] == ["Calendar", "Mail", "Reminders"]
    lines = payload["text"].splitlines()
    assert f"Calendar: unavailable: {calendar_access.APP_NOT_CONNECTED}" in lines
    assert "Mail: unavailable: no Full Disk Access" in lines
    assert "Reminders: unavailable: unexpected answer" in lines


def test_one_unavailable_source_keeps_the_others():
    payload = gather_brief(
        at(8, 0), list_events=fake_events,
        recent_mail=lambda since: {"status": "error", "error": "the daemon needs Full Disk Access"},
        list_reminders=fake_reminders, redact_subject=no_redact,
    )
    assert payload["unavailable"] == ["Mail"]
    assert "Mail: unavailable: the daemon needs Full Disk Access" in payload["text"]
    assert "Standup" in payload["text"] and "Pay rent" in payload["text"]
    assert "Mail unavailable" not in payload["text"]
    assert "emails unavailable" in db.describe(db.make_event(payload))
    assert "2 events" in db.describe(db.make_event(payload))


def test_prompt_carries_the_data_and_forbids_tools():
    payload = gather_brief(
        at(8, 0), list_events=fake_events, recent_mail=fake_mail,
        list_reminders=lambda **k: {"status": "error", "error": "Reminders access is denied"},
        redact_subject=no_redact,
    )
    prompt = db.build_prompt(db.make_event(payload))
    assert payload["text"] in prompt
    assert "do NOT call any tool" in prompt
    assert "Reminders: unavailable: Reminders access is denied" in prompt


# ---- privacy (#31 / #52) ---------------------------------------------------------

def test_mail_subject_redacted_under_a_cloud_engine(monkeypatch):
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: False)
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "analysis")
    payload = gather_brief(
        at(8, 0), list_events=fake_events, recent_mail=fake_mail, list_reminders=fake_reminders,
    )
    item = payload["mail"]["items"][0]
    assert item["subject"] == privacy.PLACEHOLDER
    assert item["sender"] == "Alice <alice@example.com>"  # senders are kept (#31 scope)
    assert "review the contract" not in json.dumps(payload)
    assert "review the contract" not in db.build_prompt(db.make_event(payload))

    # The user's opt-in (#52), or all-local engines, shows the subject.
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: True)
    allowed = gather_brief(
        at(8, 0), list_events=fake_events, recent_mail=fake_mail, list_reminders=fake_reminders,
    )
    assert allowed["mail"]["items"][0]["subject"] == "Can you review the contract?"
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: False)
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: False)
    local = gather_brief(
        at(8, 0), list_events=fake_events, recent_mail=fake_mail, list_reminders=fake_reminders,
    )
    assert local["mail"]["items"][0]["subject"] == "Can you review the contract?"


# ---- mail read path --------------------------------------------------------------

MAIL_SQL = """
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
CREATE TABLE messages (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    subject INTEGER, sender INTEGER, date_received INTEGER, mailbox INTEGER,
    global_message_id INTEGER, list_id_hash INTEGER, unsubscribe_type INTEGER
);
INSERT INTO mailboxes VALUES (1, 'imap://UUID-1/INBOX');
INSERT INTO mailboxes VALUES (2, 'imap://UUID-1/%5BGmail%5D/Sent%20Mail');
INSERT INTO mailboxes VALUES (3, 'imap://UUID-1/%5BGmail%5D/All%20Mail');
INSERT INTO mailboxes VALUES (4, 'imap://UUID-1/Junk');
INSERT INTO addresses VALUES (1, 'alice@example.com', 'Alice Example');
INSERT INTO addresses VALUES (2, 'news@example.com', '');
"""


def _mail_root(tmp_path: Path, rows) -> Path:
    index = tmp_path / "Mail" / "V10" / "MailData" / "Envelope Index"
    index.parent.mkdir(parents=True)
    conn = sqlite3.connect(index)
    conn.executescript(MAIL_SQL)
    for subject, sender, received, mailbox, gid, list_id in rows:
        cur = conn.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,))
        conn.execute(
            "INSERT INTO messages (subject, sender, date_received, mailbox, "
            "global_message_id, list_id_hash, unsubscribe_type) VALUES (?,?,?,?,?,?,0)",
            (cur.lastrowid, sender, int(received.timestamp()), mailbox, gid, list_id),
        )
    conn.commit()
    conn.close()
    return tmp_path / "Mail"


def test_recent_mail_reads_last_24h_and_skips_noise(tmp_path):
    now = at(8, 0)
    root = _mail_root(tmp_path, [
        ("Old news", 1, now - timedelta(hours=30), 1, 1, 0),
        ("Lunch tomorrow?", 1, now - timedelta(hours=3), 1, 2, 0),
        ("Lunch tomorrow?", 1, now - timedelta(hours=3), 3, 2, 0),   # Gmail label duplicate
        ("Weekly digest", 2, now - timedelta(hours=2), 1, 3, 77),    # newsletter
        ("My reply", 1, now - timedelta(hours=1), 2, 4, 0),          # sent mail
        ("You won", 2, now - timedelta(hours=1), 4, 5, 0),           # junk
        ("Invoice question", 1, now - timedelta(minutes=5), 1, 6, 0),
    ])
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    result = db.recent_mail(now - db.MAIL_WINDOW, mail_root=root)
    assert result["status"] == "ok"
    assert [m["subject"] for m in result["messages"]] == ["Invoice question", "Lunch tomorrow?"]
    assert result["messages"][0]["sender"] == "Alice Example <alice@example.com>"
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()} == before  # read-only

    payload = gather_brief(
        now, list_events=fake_events, list_reminders=fake_reminders,
        recent_mail=lambda since: db.recent_mail(since, mail_root=root), redact_subject=no_redact,
    )
    assert [m["subject"] for m in payload["mail"]["items"]] == ["Invoice question", "Lunch tomorrow?"]


def test_recent_mail_missing_index_is_an_error_answer(tmp_path, monkeypatch):
    result = db.recent_mail(at(8, 0), mail_root=tmp_path / "nope")
    assert result["status"] == "error" and "Mail" in result["error"]
    # The default path honours AGENT_MAIL_ROOT (never the real ~/Library/Mail here).
    monkeypatch.setenv("AGENT_MAIL_ROOT", str(tmp_path / "nope"))
    payload = gather_brief(at(8, 0), list_events=fake_events, list_reminders=fake_reminders)
    assert payload["unavailable"] == ["Mail"]


# ---- invariant 7a: read-only, no approval, no prompt ---------------------------------

class RecordingBackend:
    """Calendar + reminders backend that records every call made to it."""

    name = "fake"
    max_range_days = 31

    def __init__(self, ready: bool = True, reminder_status: str = "full_access") -> None:
        self._ready = ready
        self._reminder_status = reminder_status
        self.calls: list[str] = []

    def ready(self) -> bool:
        self.calls.append("ready")
        return self._ready

    def status(self, entity: str = "event") -> str:
        self.calls.append(f"status:{entity}")
        return self._reminder_status

    def list(self, start, end, calendars=None):
        self.calls.append("list")
        return [calendar_access.make_event(
            title="Standup", start=at(9, 0), end=at(9, 30), calendar="Work")]

    def list_reminders(self, lists=None, include_completed=False):
        self.calls.append("list_reminders")
        return [calendar_access.make_reminder(title="Pay rent", due=at(0, 0, day=8), all_day=True)]

    def request_access(self, entity: str = "event") -> bool:
        self.calls.append("request_access")
        return True

    def create(self, *a, **k):
        self.calls.append("create")

    def create_reminder(self, *a, **k):
        self.calls.append("create_reminder")


@pytest.fixture
def backend(monkeypatch, tmp_path):
    fake = RecordingBackend()
    monkeypatch.setattr(calendar_access, "_override", fake)
    monkeypatch.setattr(calendar_access, "_reminder_override", fake)
    monkeypatch.setenv("AGENT_MAIL_ROOT", str(tmp_path / "no-mail"))
    return fake


def test_default_sources_only_read_and_never_request_approval(backend, monkeypatch, tmp_path):
    import subprocess

    from ai import orchestrator as orch

    def forbidden(*a, **k):
        raise AssertionError("side-effect / approval path touched")

    # No approval request, no notification / AppleScript, no gated tool.
    for name in ("request_code_approval", "submit_approval_response"):
        monkeypatch.setattr(orch.Orchestrator, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    scheduler, bus = make_scheduler(tmp_path, Clock(at(8, 0)), gather=gather_brief)
    event = scheduler.check_once()
    assert [e.event_type for e in bus.events] == ["daily_brief"]
    assert "Standup" in event.metadata["text"] and "Pay rent" in event.metadata["text"]
    # Only reads reached the backends.
    assert set(backend.calls) == {"ready", "list", "status:reminder", "list_reminders"}
    assert db.brief_me()["brief"].startswith("Brief for ")
    assert set(backend.calls) == {"ready", "list", "status:reminder", "list_reminders"}


def test_ungranted_access_is_reported_not_prompted(monkeypatch, tmp_path):
    fake = RecordingBackend(ready=False, reminder_status="not_determined")
    monkeypatch.setattr(calendar_access, "_override", fake)
    monkeypatch.setattr(calendar_access, "_reminder_override", fake)
    monkeypatch.setenv("AGENT_MAIL_ROOT", str(tmp_path / "no-mail"))
    payload = gather_brief(at(8, 0))
    assert payload["unavailable"] == ["Calendar", "Mail", "Reminders"]
    assert "Reminders: unavailable: Reminders access is not_determined" in payload["text"]
    # Neither list call ran: either could have shown a macOS permission prompt.
    assert set(fake.calls) == {"ready", "status:reminder"}


def test_module_uses_only_read_only_tools():
    """The module imports nothing from ``tools`` that needs approval."""
    allowed_modules = {"tools", "tools.list_events", "tools.reminders", "tools.read_email"}
    allowed_names = {"calendar_access", "list_events", "list_reminders", "mail_root"}
    tree = ast.parse(Path(db.__file__).read_text())
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tools":
            assert node.module in allowed_modules, node.module
            for alias in node.names:
                assert alias.name in allowed_names, alias.name
                seen.add(alias.name)
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] in {"tools", "subprocess"} for a in node.names)
    assert {"list_events", "list_reminders"} <= seen
    assert {"list_events", "list_reminders", "read_email"} <= tools.READ_ONLY_TOOL_NAMES
    source = Path(db.__file__).read_text()
    for gated in ("show_notification(", ".create(", "create_reminder(", "request_access(", "osascript"):
        assert gated not in source, gated


# ---- settings keys + routes --------------------------------------------------------

def test_settings_keys_are_allowed():
    assert {db.ENABLED_KEY, db.TIME_KEY} <= user_settings.ALLOWED_KEYS
    assert {"MLX_MAIN_MODEL", "AGENT_CLOUD_PERSONAL_TEXT"} <= user_settings.ALLOWED_KEYS


def test_parse_time():
    assert db.parse_time("08:00") == dtime(8, 0)
    assert db.parse_time("7:05") == dtime(7, 5)
    for bad in ("", "8", "24:00", "08:60", "ab:cd", "08:00:00", None):
        assert db.parse_time(bad) is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    monkeypatch.delenv(db.ENABLED_KEY, raising=False)
    monkeypatch.delenv(db.TIME_KEY, raising=False)
    app = FastAPI()
    app.include_router(routes_db.router)
    with TestClient(app) as test_client:
        test_client.settings_path = tmp_path / "settings.json"
        yield test_client


def test_route_reads_and_saves_settings(client):
    assert client.get("/api/daily-brief").json() == {
        "enabled": True, "time": "08:00", "locked_fields": [],
    }
    body = client.put("/api/daily-brief", json={"enabled": False, "time": "7:15"}).json()
    assert body["enabled"] is False and body["time"] == "07:15"
    assert json.loads(client.settings_path.read_text()) == {
        "AGENT_DAILY_BRIEF": "0", "AGENT_DAILY_BRIEF_TIME": "07:15",
    }
    assert db.brief_enabled() is False and db.brief_time() == dtime(7, 15)
    # Omitted fields are left unchanged.
    assert client.put("/api/daily-brief", json={"enabled": True}).json() == {
        "enabled": True, "time": "07:15", "locked_fields": [],
    }
    assert client.put("/api/daily-brief", json={"time": "25:00"}).status_code == 400
    assert client.get("/api/daily-brief").json()["time"] == "07:15"


def test_route_run_publishes_a_manual_brief(client, tmp_path):
    assert client.post("/api/daily-brief/run").status_code == 503  # nothing wired
    scheduler, bus = make_scheduler(tmp_path, Clock(at(7, 0)))
    client.app.state.daily_brief = scheduler
    assert client.post("/api/daily-brief/run").json()["status"] == "ok"
    assert [e.metadata["trigger"] for e in bus.events] == ["manual"]
