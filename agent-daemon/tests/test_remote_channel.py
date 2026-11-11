"""#85 iPhone remote: own-thread commands, code approvals, egress, exclusion.

Every test builds its own fixture ``chat.db`` and injects the sender, so no
iMessage is ever sent.
"""
from __future__ import annotations

import asyncio
import sqlite3
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import user_settings
from ai.egress_log import EgressLog
from events import remote_channel as rc
from events.imessage_watcher import IMessageWatcher
from events.remote_channel import RemoteChannel

OWN = "+14155550100"
OTHER = "+14155550199"

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, service_name TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, attributedBody BLOB,
    is_from_me INTEGER, service TEXT, handle_id INTEGER,
    associated_message_type INTEGER DEFAULT 0
);
"""

OWN_CHAT, OTHER_CHAT, GROUP_CHAT, OWN_SMS_CHAT = 1, 2, 3, 4


class ChatDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO handle VALUES (?, ?, ?)", [
            (1, OWN, "iMessage"), (2, OTHER, "iMessage"), (3, OWN, "SMS"),
        ])
        conn.executemany("INSERT INTO chat VALUES (?, ?, ?)", [
            (OWN_CHAT, OWN, "iMessage"), (OTHER_CHAT, OTHER, "iMessage"),
            (GROUP_CHAT, "chat123", "iMessage"), (OWN_SMS_CHAT, OWN, "SMS"),
        ])
        conn.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [
            (OWN_CHAT, 1), (OTHER_CHAT, 2), (GROUP_CHAT, 1), (GROUP_CHAT, 2),
            (OWN_SMS_CHAT, 3),
        ])
        conn.execute("INSERT INTO message (text, is_from_me, service, handle_id)"
                     " VALUES ('old history yes AAAA', 1, 'iMessage', 1)")
        conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
        conn.commit()
        conn.close()

    def add(self, text, *, chat=OWN_CHAT, from_me=1, service="iMessage", handle=1,
            attributed=None, associated=0) -> int:
        conn = sqlite3.connect(self.path)
        cur = conn.execute(
            "INSERT INTO message (text, attributedBody, is_from_me, service, handle_id,"
            " associated_message_type) VALUES (?, ?, ?, ?, ?, ?)",
            (text, attributed, from_me, service, handle, associated),
        )
        rowid = cur.lastrowid
        conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat, rowid))
        conn.commit()
        conn.close()
        return rowid


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    def push_threadsafe(self, event) -> None:
        self.events.append(event)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(rc.ENABLED_KEY, "1")
    monkeypatch.setenv(rc.HANDLE_KEY, OWN)
    monkeypatch.setenv(rc.IDLE_MINUTES_KEY, "10")


@pytest.fixture
def env(tmp_path):
    db = ChatDB(tmp_path / "chat.db")
    bus = FakeBus()
    sent: list[tuple[str, str]] = []
    submitted: list[tuple[str, bool]] = []
    clock = Clock()
    egress = EgressLog(db_path=tmp_path / "egress.db")
    state = types.SimpleNamespace(idle=3600.0, paused=False, open={"req_a", "req_b"})

    def sender(handle, body):
        sent.append((handle, body))
        return True

    def submit(request_id, approved):
        submitted.append((request_id, approved))
        if request_id in state.open:
            state.open.discard(request_id)
            return True
        return False

    ch = RemoteChannel(
        bus, db.path, sender=sender, submit=submit, clock=clock, egress=egress,
        idle_seconds=lambda: state.idle, paused=lambda: state.paused,
    )
    ch.poll_once()  # baseline: history is never replayed
    return types.SimpleNamespace(db=db, bus=bus, sent=sent, submitted=submitted,
                                 clock=clock, ch=ch, state=state, egress=egress)


def _args(**kw):
    return {"args": [], "kwargs": kw}


def _code(sent_body: str) -> str:
    return sent_body.rsplit('"no ', 1)[1].split('"', 1)[0]


# ---- off by default (7c) ----------------------------------------------------

def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv(rc.ENABLED_KEY, raising=False)
    monkeypatch.setenv(rc.HANDLE_KEY, OWN)
    assert rc.remote_enabled() is False
    assert rc.excluded_handle() is None
    assert env.ch.mirror_approval("req_a", "create_reminder", _args(title="x")) is None
    env.db.add("what's on my calendar?")
    assert env.ch.poll_once() == 0
    assert env.bus.events == [] and env.sent == []


def test_on_without_handle_is_off(env, monkeypatch):
    monkeypatch.setenv(rc.ENABLED_KEY, "1")
    monkeypatch.delenv(rc.HANDLE_KEY, raising=False)
    assert RemoteChannel.enabled() is False
    assert env.ch.mirror_approval("req_a", "create_reminder", _args()) is None


# ---- own thread only (decision 1) ------------------------------------------

def test_history_is_not_replayed(env, on):
    assert env.ch.poll_once() == 0
    assert env.bus.events == []


def test_other_handles_sms_groups_and_received_rows_are_ignored(env, on):
    env.db.add("hi from someone", chat=OTHER_CHAT, from_me=0, handle=2)
    env.db.add("sent to someone", chat=OTHER_CHAT, from_me=1, handle=2)
    env.db.add("group text", chat=GROUP_CHAT, from_me=1, handle=1)
    env.db.add("over sms", chat=OWN_SMS_CHAT, from_me=1, service="SMS", handle=3)
    env.db.add("echo copy", chat=OWN_CHAT, from_me=0, handle=1)
    env.db.add("Liked a message", chat=OWN_CHAT, associated=2000)
    assert env.ch.poll_once() == 0
    assert env.bus.events == [] and env.sent == []


def test_own_thread_text_is_a_chat_turn_answered_in_thread(env, on):
    rowid = env.db.add("what's on my calendar tomorrow?")
    assert env.ch.poll_once() == 1
    [event] = env.bus.events
    assert event.event_type == "user_message"
    assert event.metadata == {"event_id": f"remote_{rowid}",
                              "text": "what's on my calendar tomorrow?",
                              "source": rc.REMOTE_SOURCE}
    assert env.ch.send_reply(f"remote_{rowid}", "Two meetings.") is True
    assert env.sent == [(OWN, rc.PREFIX + "Two meetings.")]
    # Each turn is answered once; turns it did not start are never answered.
    assert env.ch.send_reply(f"remote_{rowid}", "again") is False
    assert env.ch.send_reply("chat_123", "not from the phone") is False
    assert len(env.sent) == 1


def test_attributed_body_text_is_read(env, on):
    text = "remind me at 5"
    blob = (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
            b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
            + bytes([len(text)]) + text.encode() + b"\x86\x84\x02iI")
    assert rc.text_from_attributed_body(blob) == text
    env.db.add(None, attributed=blob)
    assert env.ch.poll_once() == 1
    assert env.bus.events[0].metadata["text"] == text


# ---- approve by code (decision 3) -------------------------------------------

def test_yes_code_approves_exactly_that_request(env, on):
    code_a = env.ch.mirror_approval("req_a", "create_reminder", _args(title="Pay rent"))
    code_b = env.ch.mirror_approval("req_b", "create_note", _args(title="Ideas"))
    assert code_a and code_b and code_a != code_b
    assert len(code_a) == rc.CODE_LENGTH
    env.db.add(f"yes {code_b.lower()}")
    assert env.ch.poll_once() == 1
    assert env.submitted == [("req_b", True)]
    assert env.ch.pending_codes() == {code_a: "req_a"}
    assert env.sent[-1] == (OWN, f"{rc.PREFIX}Approved ({code_b}).")
    env.db.add(f"No {code_a}.")
    env.ch.poll_once()
    assert env.submitted == [("req_b", True), ("req_a", False)]
    assert env.bus.events == []  # replies are never chat turns


def test_wrong_reused_expired_and_forgotten_codes_do_nothing(env, on):
    code = env.ch.mirror_approval("req_a", "create_reminder", _args(title="x"))
    env.db.add("yes ZZZZ")  # wrong
    env.ch.poll_once()
    assert env.submitted == []
    env.db.add(f"yes {code}")
    env.ch.poll_once()
    assert env.submitted == [("req_a", True)]
    env.db.add(f"yes {code}")  # reused
    env.ch.poll_once()
    assert env.submitted == [("req_a", True)]

    code2 = env.ch.mirror_approval("req_b", "create_reminder", _args(title="y"))
    env.clock.t += rc.REMOTE_TIMEOUT_S + 1  # expired
    env.db.add(f"yes {code2}")
    env.ch.poll_once()
    assert env.submitted == [("req_a", True)]

    env.state.open.add("req_c")
    code3 = env.ch.mirror_approval("req_c", "create_reminder", _args(title="z"))
    env.ch.forget("req_c")  # answered on the Mac
    env.db.add(f"yes {code3}")
    env.ch.poll_once()
    assert env.submitted == [("req_a", True)]
    assert env.bus.events == []


def test_bare_yes_does_nothing(env, on):
    env.ch.mirror_approval("req_a", "create_reminder", _args(title="x"))
    for text in ("yes", "Yes!", "no"):
        env.db.add(text)
    env.ch.poll_once()
    assert env.submitted == []
    assert env.bus.events == []
    assert len(env.ch.pending_codes()) == 1


# ---- never from the phone (decision 4) ----------------------------------------

@pytest.mark.parametrize("tool", sorted(rc.NEVER_FROM_PHONE))
def test_never_mirrored_tools(env, on, tool):
    assert env.ch.mirror_approval("req_a", tool, _args(), from_phone=True) is None
    assert env.sent == []


def test_cards_with_origin_notes_or_actions_are_not_mirrored(env, on):
    assert env.ch.mirror_approval("req_a", "create_reminder", _args(),
                                  origin_notes=["title: from the email"], from_phone=True) is None
    assert env.ch.mirror_approval("req_a", "create_reminder", _args(),
                                  actions=[{"id": "x"}], from_phone=True) is None
    assert env.sent == []


def test_no_code_review_api():
    # Code review (code_approval_request) has no path through the channel.
    names = [n for n in dir(RemoteChannel) if "code" in n.lower() and "approv" in n.lower()]
    assert names == []


# ---- when (decision 5) --------------------------------------------------------

def test_mirrored_only_when_idle_or_from_phone(env, on):
    env.state.idle = 60.0  # active at the Mac (10 min threshold)
    assert env.ch.eligible("create_reminder") is False
    assert env.ch.mirror_approval("req_a", "create_reminder", _args()) is None
    assert env.ch.eligible("create_reminder", from_phone=True) is True
    env.state.idle = 601.0
    assert env.ch.eligible("create_reminder") is True
    env.state.paused = True  # #87 pause holds ambient cards, not phone turns
    assert env.ch.eligible("create_reminder") is False
    assert env.ch.eligible("create_reminder", from_phone=True) is True


# ---- the sender (decisions 2, 6) ----------------------------------------------

def test_recipient_is_always_the_configured_handle(env, on):
    code = env.ch.mirror_approval("req_a", "send_email",
                                  _args(to="+12025550123", body="hi"), summary="Email Bob")
    rowid = env.db.add("hello?")
    env.ch.poll_once()
    env.ch.send_reply(f"remote_{rowid}", "Hi.")
    env.db.add("yes")
    env.db.add(f"yes {code}")
    env.ch.poll_once()
    assert len(env.sent) == 4
    assert {h for h, _ in env.sent} == {OWN}
    card = env.sent[0][1]
    assert card.startswith(rc.PREFIX + "Approve? Email Bob")
    assert "to: +12025550123" in card and f'"yes {code}"' in card


def test_rate_limit_holds(env, on):
    env.ch.max_per_hour = 3
    codes = [env.ch.mirror_approval(f"r{i}", "create_note", _args(title=str(i)))
             for i in range(5)]
    assert sum(c is not None for c in codes) == 3
    assert len(env.sent) == 3
    assert len(env.ch.pending_codes()) == 3  # unsent cards hold no code
    env.clock.t += 3600
    assert env.ch.mirror_approval("r9", "create_note", _args()) is not None
    assert len(env.sent) == 4


def test_own_messages_are_not_read_back(env, on):
    code = env.ch.mirror_approval("req_a", "create_reminder", _args(title="x"))
    card = env.sent[0][1]
    env.db.add(card)  # the echo of the card (contains "yes CODE")
    env.db.add(card[len(rc.PREFIX):])  # even if the prefix were lost
    env.db.add(f"{rc.PREFIX}yes {code}")
    assert env.ch.poll_once() == 0
    assert env.submitted == [] and env.bus.events == []


def test_every_send_is_recorded_as_egress(env, on):
    env.ch.mirror_approval("req_a", "create_reminder", _args(title="x"))
    rowid = env.db.add("hi")
    env.ch.poll_once()
    env.ch.send_reply(f"remote_{rowid}", "Hello.")
    rows = env.egress.rows()
    assert len(rows) == len(env.sent) == 2
    assert {r["kind"] for r in rows} == {rc.KIND_IMESSAGE}
    assert all(r["tool"] == rc.EGRESS_TOOL and r["personal_text"] == "sent" for r in rows)


def test_failed_send_is_not_mirrored(env, on):
    env.ch._sender = lambda h, b: False
    assert env.ch.mirror_approval("req_a", "create_reminder", _args()) is None
    assert env.ch.pending_codes() == {}
    assert env.egress.rows() == []


# ---- resolving the SAME orchestrator future ------------------------------------

def test_attach_orchestrator_resolves_the_waiting_future(env, on):
    from ai.orchestrator import Orchestrator

    async def scenario():
        loop = asyncio.get_running_loop()
        orch = types.SimpleNamespace(_pending={})
        orch.submit_approval_response = types.MethodType(
            Orchestrator.submit_approval_response, orch)
        fut = loop.create_future()
        orch._pending["req_x"] = fut
        env.ch.attach_orchestrator(orch, loop)
        code = env.ch.mirror_approval("req_x", "create_reminder", _args(title="x"))
        env.db.add(f"yes {code}")
        await asyncio.to_thread(env.ch.poll_once)
        resp = await asyncio.wait_for(fut, 1)
        return resp

    resp = asyncio.run(scenario())
    assert resp.request_id == "req_x" and resp.approved is True
    assert resp.user_note == "answered from your phone"


# ---- imessage_watcher excludes the own thread ----------------------------------

def test_watcher_excludes_own_thread(env, on):
    env.db.add("echo of my own text", chat=OWN_CHAT, from_me=0, handle=1)
    env.db.add("hi from someone", chat=OTHER_CHAT, from_me=0, handle=2)
    bus = FakeBus()
    watcher = IMessageWatcher(bus, env.db.path)
    watcher._last_rowid = 1
    conn = sqlite3.connect(env.db.path)
    watcher._poll_once(conn)
    assert [e.metadata["sender"] for e in bus.events] == [OTHER]

    off = IMessageWatcher(FakeBus(), env.db.path, exclude_handle=lambda: None)
    off._last_rowid = 1
    off._poll_once(conn)
    assert [e.metadata["sender"] for e in off._bus.events] == [OWN, OTHER]
    conn.close()


def test_handles_match_across_formats():
    assert rc.same_handle("+1 (415) 555-0100", OWN)
    assert rc.same_handle("4155550100", OWN)
    assert rc.same_handle("Me@iCloud.com", "me@icloud.com")
    assert not rc.same_handle(OTHER, OWN)
    assert not rc.same_handle("", OWN)
    assert rc.normalize_handle("not a handle") == ""


# ---- settings route ---------------------------------------------------------------

def test_settings_route(tmp_path, monkeypatch):
    from routes import remote as remote_routes

    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    for key in (rc.ENABLED_KEY, rc.HANDLE_KEY, rc.IDLE_MINUTES_KEY):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(user_settings, "ALLOWED_KEYS", user_settings.ALLOWED_KEYS | {
        rc.ENABLED_KEY, rc.HANDLE_KEY, rc.IDLE_MINUTES_KEY})
    app = FastAPI()
    app.include_router(remote_routes.router)
    client = TestClient(app)
    view = client.get("/api/remote").json()
    assert view["enabled"] is False and view["handle"] == ""
    assert view["idle_minutes"] == rc.DEFAULT_IDLE_MINUTES
    assert client.put("/api/remote", json={"enabled": True}).status_code == 400
    assert client.put("/api/remote", json={"handle": "bob"}).status_code == 400
    assert client.put("/api/remote", json={"idle_minutes": 0}).status_code == 400
    view = client.put("/api/remote", json={"handle": "+1 415 555 0100", "enabled": True,
                                           "idle_minutes": 5}).json()
    assert view["enabled"] is True and view["handle"] == OWN and view["idle_minutes"] == 5
    view = client.put("/api/remote", json={"handle": ""}).json()
    assert view["enabled"] is False
