"""Tests for events.meeting_prep and routes.meeting_prep (todo #71)."""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
import tools
import user_settings
from ai import privacy
from events import meeting_prep as mp
from events.event_bus import ContextEvent
from events.meeting_prep import MeetingPrepScheduler, gather_prep
from routes import meeting_prep as routes_mp

TZ = timezone(timedelta(hours=-7))
NOW = datetime(2026, 11, 10, 14, 20, tzinfo=TZ)
START = NOW + timedelta(minutes=10)


def meeting(**overrides):
    meta = {
        "title": "Q4 budget review",
        "start": START.isoformat(),
        "location": "Room 4",
        "url": "",
        "calendar": "Work",
        "video_url": "https://zoom.us/j/123",
        "attendees": [
            {"name": "Me", "email": "mailto:me@example.com", "is_self": True},
            {"name": "Alice Example", "email": "mailto:Alice@Example.com"},
            "Bob <bob@example.com>",
        ],
    }
    meta.update(overrides)
    return meta


class Calls:
    def __init__(self):
        self.mail = []
        self.files = []
        self.notes = []


def fakes(calls: Calls):
    def mail_with(addresses, since):
        calls.mail.append((list(addresses), since))
        return {"status": "ok", "messages": [
            {"message_id": 7, "sender": "Alice Example <alice@example.com>",
             "subject": "Budget numbers attached", "date": "2026-11-08T10:00:00+00:00"},
            {"message_id": 9, "sender": "me@example.com", "subject": "Re: agenda",
             "date": "2026-11-09T10:00:00+00:00", "sent": True},
        ]}

    def find_files(query, limit=5):
        calls.files.append(query)
        hits = {"budget": [{"path": "/Users/u/Docs/Budget 2026.xlsx", "name": "Budget 2026.xlsx", "modified": 200}],
                "Q4": [{"path": "/Users/u/Docs/Q4 plan.pdf", "name": "Q4 plan.pdf", "modified": 300},
                       {"path": "/Users/u/Docs/Budget 2026.xlsx", "name": "Budget 2026.xlsx", "modified": 200}]}
        return {"status": "ok", "results": hits.get(query, [])}

    def find_note(title):
        calls.notes.append(title)
        return {"status": "ok", "notes": [{"title": title, "folder": "Work", "modified": "2026-11-07T09:00:00"}]}

    return {"mail_with": mail_with, "find_files": find_files, "find_note": find_note,
            "redact_subject": lambda s: s}


# ---- payload ---------------------------------------------------------------------

def test_payload_built_for_an_event_with_attendees():
    calls = Calls()
    payload = gather_prep(meeting(), NOW, **fakes(calls))
    assert payload is not None
    # Only the other attendees' addresses are looked up, lowercased, 30 days back.
    assert calls.mail == [(["alice@example.com", "bob@example.com"], NOW - mp.MAIL_WINDOW)]
    assert payload["attendees"]["items"] == ["Alice Example <alice@example.com>", "Bob <bob@example.com>"]
    assert [m["message_id"] for m in payload["mail"]["items"]] == [7, 9]
    # Content words only ("review" is a stop word); files merged newest first.
    assert sorted(calls.files) == ["Q4", "budget"]
    assert [f["name"] for f in payload["files"]["items"]] == ["Q4 plan.pdf", "Budget 2026.xlsx"]
    assert calls.notes == ["Q4 budget review"]
    assert payload["unavailable"] == []
    assert payload["minutes_until"] == 10 and payload["when"] == "2:30 PM"
    text = payload["text"]
    assert text.splitlines()[0] == "Meeting prep: Q4 budget review at 2:30 PM (in 10 min)"
    assert "Where: Room 4 · Call link: https://zoom.us/j/123" in text
    assert "Alice Example <alice@example.com>: Budget numbers attached (2026-11-08) [id 7]" in text
    assert "you (sent): Re: agenda" in text
    assert 'Notes: a note titled "Q4 budget review" in Work, modified 2026-11-07' in text
    assert "Me" not in payload["attendees"]["items"]


def test_content_words():
    assert mp.content_words("Weekly sync with Acme re: Contract renewal") == ["Contract", "renewal", "Acme"]
    assert mp.content_words("1:1") == []
    assert mp.content_words("Team standup") == []


def test_title_without_content_words_says_so():
    calls = Calls()
    payload = gather_prep(meeting(title="Weekly sync"), NOW, **fakes(calls))
    assert calls.files == []
    assert "Files: no content words in the title" in payload["text"]


# ---- skips ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides, reason", [
    ({"attendees": [{"name": "Me", "email": "me@example.com", "is_self": True}]}, "no other attendee"),
    ({"attendees": []}, "no other attendee"),
    ({"all_day": True}, "all-day event"),
    ({"start": (NOW - timedelta(minutes=1)).isoformat()}, "already started"),
    ({"attendees": None, "video_url": ""}, "attendees unknown and no call link"),
])
def test_solo_all_day_and_unknown_events_are_skipped(overrides, reason):
    meta = meeting(**overrides)
    if overrides.get("attendees", "x") is None:
        meta.pop("attendees")
    calls = Calls()
    assert mp.skip_reason(meta, NOW) == reason
    assert gather_prep(meta, NOW, **fakes(calls)) is None
    assert (calls.mail, calls.files, calls.notes) == ([], [], [])  # nothing gathered


def test_unknown_attendees_with_a_call_link_are_prepped_and_named():
    """Today's calendar layer sends no attendees; a call link still qualifies."""
    meta = meeting()
    meta.pop("attendees")
    calls = Calls()
    payload = gather_prep(meta, NOW, **fakes(calls))
    assert payload is not None
    assert calls.mail == []
    assert payload["unavailable"] == ["Attendees", "Mail"]
    assert f"Attendees: unavailable: {mp.ATTENDEES_UNKNOWN}" in payload["text"]
    assert "Recent mail with them: unavailable:" in payload["text"]


# ---- failing sources -------------------------------------------------------------------

def test_a_failing_source_is_named_not_dropped():
    calls = Calls()
    sources = fakes(calls)

    def broken_mail(addresses, since):
        return {"status": "error", "error": "the daemon needs Full Disk Access to read Mail"}

    def broken_files(query, limit=5):
        raise RuntimeError("mdfind timed out")

    def broken_note(title):
        return {"status": "error", "error": "Notes automation isn't available (not_asked)"}

    payload = gather_prep(meeting(), NOW, **{**sources, "mail_with": broken_mail,
                                             "find_files": broken_files, "find_note": broken_note})
    assert payload["unavailable"] == ["Mail", "Files", "Notes"]
    text = payload["text"]
    assert "Recent mail with them: unavailable: the daemon needs Full Disk Access to read Mail" in text
    assert "Files: unavailable: mdfind timed out" in text
    assert "Notes: unavailable: Notes automation isn't available (not_asked)" in text
    # The sources that worked are still there.
    assert "Alice Example <alice@example.com>" in text


def test_one_failing_word_keeps_the_other_files():
    calls = Calls()
    sources = fakes(calls)
    good = sources["find_files"]

    def flaky(query, limit=5):
        if query == "budget":
            return {"status": "error", "error": "boom"}
        return good(query, limit)

    payload = gather_prep(meeting(), NOW, **{**sources, "find_files": flaky})
    assert payload["files"]["status"] == "ok"
    assert [f["name"] for f in payload["files"]["items"]] == ["Q4 plan.pdf", "Budget 2026.xlsx"]


# ---- privacy -------------------------------------------------------------------------------

def test_subjects_redacted_under_a_cloud_engine(monkeypatch):
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: False)
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    sources = {k: v for k, v in fakes(Calls()).items() if k != "redact_subject"}
    payload = gather_prep(meeting(), NOW, **sources)
    assert [m["subject"] for m in payload["mail"]["items"]] == [privacy.PLACEHOLDER] * 2
    assert "Budget numbers attached" not in json.dumps(payload)
    assert "Budget numbers attached" not in mp.build_prompt(mp.make_event(payload))
    assert payload["mail"]["items"][0]["sender"] == "Alice Example <alice@example.com>"

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: False)
    local = gather_prep(meeting(), NOW, **sources)
    assert local["mail"]["items"][0]["subject"] == "Budget numbers attached"


# ---- prompt / describe ----------------------------------------------------------------------

def test_prompt_carries_the_data_caps_lines_and_forbids_tools():
    payload = gather_prep(meeting(), NOW, **fakes(Calls()))
    event = mp.make_event(payload)
    assert event.event_type == "meeting_prep"
    prompt = mp.build_prompt(event)
    assert "do NOT call any tool" in prompt and "do not ask for approval" in prompt
    assert "At most 6 short lines" in prompt
    assert payload["text"] in prompt
    assert mp.describe(event) == (
        "Meeting prep for Q4 budget review at 2:30 PM: 2 attendees, 2 emails, 2 files"
    )


# ---- mail read path ----------------------------------------------------------------------------

MAIL_SQL = """
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
CREATE TABLE messages (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    subject INTEGER, sender INTEGER, date_received INTEGER, mailbox INTEGER,
    global_message_id INTEGER, list_id_hash INTEGER, unsubscribe_type INTEGER
);
CREATE TABLE recipients (ROWID INTEGER PRIMARY KEY, message INTEGER, address INTEGER, type INTEGER);
INSERT INTO mailboxes VALUES (1, 'imap://UUID-1/INBOX');
INSERT INTO mailboxes VALUES (2, 'imap://UUID-1/%5BGmail%5D/Sent%20Mail');
INSERT INTO mailboxes VALUES (3, 'imap://UUID-1/%5BGmail%5D/All%20Mail');
INSERT INTO mailboxes VALUES (4, 'imap://UUID-1/Junk');
INSERT INTO mailboxes VALUES (5, 'imap://UUID-1/Drafts');
INSERT INTO addresses VALUES (1, 'Alice@Example.com', 'Alice Example');
INSERT INTO addresses VALUES (2, 'carol@example.com', 'Carol');
INSERT INTO addresses VALUES (3, 'me@example.com', 'Me');
"""


def _mail_root(tmp_path: Path, rows) -> Path:
    index = tmp_path / "Mail" / "V10" / "MailData" / "Envelope Index"
    index.parent.mkdir(parents=True)
    conn = sqlite3.connect(index)
    conn.executescript(MAIL_SQL)
    for subject, sender, received, mailbox, gid, to in rows:
        cur = conn.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,))
        msg = conn.execute(
            "INSERT INTO messages (subject, sender, date_received, mailbox, "
            "global_message_id, list_id_hash, unsubscribe_type) VALUES (?,?,?,?,?,0,0)",
            (cur.lastrowid, sender, int(received.timestamp()), mailbox, gid),
        )
        for address in to:
            conn.execute("INSERT INTO recipients (message, address, type) VALUES (?,?,0)",
                         (msg.lastrowid, address))
    conn.commit()
    conn.close()
    return tmp_path / "Mail"


def test_mail_with_reads_from_and_to_attendees_only(tmp_path):
    root = _mail_root(tmp_path, [
        ("Old thread", 1, NOW - timedelta(days=40), 1, 1, [3]),         # too old
        ("Budget numbers", 1, NOW - timedelta(days=2), 1, 2, [3]),      # from Alice
        ("Budget numbers", 1, NOW - timedelta(days=2), 3, 2, [3]),      # Gmail label duplicate
        ("Re: agenda", 3, NOW - timedelta(days=1), 2, 3, [1]),          # sent to Alice
        ("Unrelated", 2, NOW - timedelta(hours=5), 1, 4, [3]),          # from Carol
        ("Spam from Alice", 1, NOW - timedelta(hours=4), 4, 5, [3]),    # junk
        ("Unsent draft", 3, NOW - timedelta(hours=3), 5, 6, [1]),       # draft
    ])
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    result = mp.mail_with(["alice@example.com"], NOW - mp.MAIL_WINDOW, mail_root=root)
    assert result["status"] == "ok"
    assert [(m["subject"], m["sent"]) for m in result["messages"]] == [
        ("Re: agenda", True), ("Budget numbers", False),
    ]
    assert result["messages"][1]["sender"] == "Alice Example <Alice@Example.com>"
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()} == before  # read-only

    payload = gather_prep(
        meeting(), NOW, mail_with=lambda a, s: mp.mail_with(a, s, mail_root=root),
        find_files=lambda q, limit=5: {"status": "ok", "results": []},
        find_note=lambda t: {"status": "ok", "notes": []}, redact_subject=lambda s: s,
    )
    assert [m["subject"] for m in payload["mail"]["items"]] == ["Re: agenda", "Budget numbers"]
    assert "Notes: no note with this title" in payload["text"]


def test_mail_with_missing_index_is_an_error_answer(tmp_path):
    result = mp.mail_with(["alice@example.com"], NOW, mail_root=tmp_path / "nope")
    assert result["status"] == "error" and "Mail" in result["error"]
    assert mp.mail_with([], NOW)["status"] == "error"


# ---- invariant 7a: read-only, no approval, no prompt ----------------------------------------------

def test_default_sources_only_read_and_never_request_approval(monkeypatch, tmp_path):
    import subprocess

    from ai import orchestrator as orch
    from ai import permissions

    def forbidden(*a, **k):
        raise AssertionError("side-effect / approval path touched")

    for name in ("request_code_approval", "submit_approval_response"):
        monkeypatch.setattr(orch.Orchestrator, name, forbidden)
    ran = []

    def fake_run(cmd, *a, **k):
        ran.append(cmd[0])
        assert cmd[0] == "mdfind", cmd  # Spotlight read only; never osascript here
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # Notes automation not granted: the note lookup must not run osascript.
    monkeypatch.setattr(permissions, "check_automation",
                        lambda bundle, app: {"status": permissions.NOT_ASKED, "detail": ""})
    monkeypatch.setenv("AGENT_MAIL_ROOT", str(tmp_path / "no-mail"))

    bus = FakeBus()
    sched = MeetingPrepScheduler(bus, clock=lambda: NOW, enabled=lambda: True,
                                 lead_minutes=lambda: 10, timer=ImmediateTimer())
    sched.observe(ContextEvent(event_type="event_starting_soon", metadata=meeting()))
    assert [e.event_type for e in bus.events] == ["meeting_prep"]
    payload = bus.events[0].metadata
    assert set(ran) == {"mdfind"}
    assert payload["unavailable"] == ["Mail", "Notes"]
    assert "Notes: unavailable: Notes automation isn't available (not_asked" in payload["text"]


def test_module_uses_only_read_only_tools():
    allowed_modules = {"tools.find_files", "tools.read_email"}
    tree = ast.parse(Path(mp.__file__).read_text())
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tools":
            assert node.module in allowed_modules, node.module
            seen.update(alias.name for alias in node.names)
    assert seen == {"find_files", "mail_root"}
    assert "find_files" in tools.READ_ONLY_TOOL_NAMES
    source = Path(mp.__file__).read_text()
    for gated in ("show_notification(", ".create(", "request_access(", "approval_request",
                  "set body", "make new", "delete "):
        assert gated not in source, gated
    # The only AppleScript is the note lookup, and it only reads.
    assert source.count("osascript") == 1


# ---- scheduler --------------------------------------------------------------------------------------

class FakeBus:
    def __init__(self):
        self.events = []

    def push_threadsafe(self, event):
        self.events.append(event)


class ImmediateTimer:
    def __init__(self):
        self.delays = []

    def __call__(self, delay, fn):
        self.delays.append(delay)
        fn()
        return self


class HeldTimer:
    def __init__(self):
        self.scheduled = []

    def __call__(self, delay, fn):
        self.scheduled.append((delay, fn))
        return self


def make_sched(*, enabled=True, minutes=10, timer=None, gather=None, clock=lambda: NOW):
    bus = FakeBus()
    state = {"enabled": enabled, "minutes": minutes}
    sched = MeetingPrepScheduler(
        bus, clock=clock,
        gather=gather or (lambda meta, now: gather_prep(meta, now, **fakes(Calls()))),
        enabled=lambda: state["enabled"], lead_minutes=lambda: state["minutes"],
        timer=timer or ImmediateTimer(),
    )
    return sched, bus, state


def soon(**overrides):
    return ContextEvent(event_type="event_starting_soon", metadata=meeting(**overrides))


def test_observe_publishes_one_prep_event_per_meeting():
    sched, bus, _ = make_sched()
    assert sched.observe(soon()) == 0.0
    assert sched.observe(soon()) is None  # same meeting again
    assert [e.event_type for e in bus.events] == ["meeting_prep"]
    assert bus.events[0].metadata["title"] == "Q4 budget review"
    # Other events are ignored.
    assert sched.observe(ContextEvent(event_type="mail_received", metadata={})) is None
    assert len(bus.events) == 1


def test_minutes_before_sets_the_wait():
    timer = HeldTimer()
    sched, bus, state = make_sched(minutes=4, timer=timer)
    # Start in 10 min, prep 4 min before -> wait 6 min.
    assert sched.observe(soon()) == pytest.approx(360.0)
    assert bus.events == []
    delay, fire = timer.scheduled[0]
    fire()
    assert [e.event_type for e in bus.events] == ["meeting_prep"]
    # Values past the watcher's 10-minute lead are clamped: no wait.
    state["minutes"] = 30
    assert sched.delay_for(meeting(start=(NOW + timedelta(minutes=8)).isoformat()), NOW) == 0.0


def test_disabled_setting_means_no_event_even_after_scheduling():
    timer = HeldTimer()
    sched, bus, state = make_sched(enabled=False, timer=timer)
    assert sched.observe(soon()) is None and timer.scheduled == []
    state["enabled"] = True
    sched.observe(soon())
    state["enabled"] = False  # turned off while waiting
    timer.scheduled[0][1]()
    assert bus.events == []


def test_skipped_meetings_schedule_nothing():
    timer = HeldTimer()
    sched, bus, _ = make_sched(timer=timer)
    sched.observe(soon(attendees=[{"name": "Me", "email": "me@x.com", "is_self": True}]))
    sched.observe(soon(all_day=True, title="Holiday"))
    assert timer.scheduled == [] and bus.events == []


def test_gather_failure_is_logged_not_raised():
    def boom(meta, now):
        raise RuntimeError("boom")

    sched, bus, _ = make_sched(gather=boom)
    sched.observe(soon())
    assert bus.events == []


def test_settings_file_drives_enabled_and_minutes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    monkeypatch.delenv(mp.ENABLED_KEY, raising=False)
    monkeypatch.delenv(mp.MINUTES_KEY, raising=False)
    assert mp.prep_enabled() is True and mp.minutes_before() == 10
    user_settings.write({mp.ENABLED_KEY: "0", mp.MINUTES_KEY: "5"})
    assert mp.prep_enabled() is False and mp.minutes_before() == 5
    user_settings.write({mp.MINUTES_KEY: "99"})  # out of range -> default
    assert mp.minutes_before() == 10


def test_parse_minutes():
    assert mp.parse_minutes("5") == 5 and mp.parse_minutes(10) == 10
    for bad in ("0", "11", "", "abc", "-3", None, "5.5"):
        assert mp.parse_minutes(bad) is None


# ---- settings keys + routes ------------------------------------------------------------------------

def test_settings_keys_are_allowed():
    assert {mp.ENABLED_KEY, mp.MINUTES_KEY} <= user_settings.ALLOWED_KEYS


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    monkeypatch.delenv(mp.ENABLED_KEY, raising=False)
    monkeypatch.delenv(mp.MINUTES_KEY, raising=False)
    app = FastAPI()
    app.include_router(routes_mp.router)
    with TestClient(app) as test_client:
        test_client.settings_path = tmp_path / "settings.json"
        yield test_client


def test_route_reads_and_saves_settings(client):
    assert client.get("/api/meeting-prep").json() == {
        "enabled": True, "minutes_before": 10, "min_minutes": 1, "max_minutes": 10,
        "locked_fields": [],
    }
    body = client.put("/api/meeting-prep", json={"enabled": False, "minutes_before": 5}).json()
    assert body["enabled"] is False and body["minutes_before"] == 5
    assert json.loads(client.settings_path.read_text()) == {
        "AGENT_MEETING_PREP": "0", "AGENT_MEETING_PREP_MINUTES": "5",
    }
    assert mp.prep_enabled() is False and mp.minutes_before() == 5
    assert client.put("/api/meeting-prep", json={"enabled": True}).json()["minutes_before"] == 5
    assert client.put("/api/meeting-prep", json={"minutes_before": 11}).status_code == 400
    assert client.put("/api/meeting-prep", json={"minutes_before": 0}).status_code == 400
    assert client.get("/api/meeting-prep").json()["minutes_before"] == 5


def test_route_reports_shell_locked_fields(client, monkeypatch):
    monkeypatch.setattr(user_settings, "shell_exported", frozenset({mp.MINUTES_KEY}))
    assert client.get("/api/meeting-prep").json()["locked_fields"] == ["minutes_before"]
