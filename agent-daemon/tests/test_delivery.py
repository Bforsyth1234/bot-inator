"""Right-moment delivery (#87): busy gate, held offers, cap, pause, never-watch.

Fake clock and fake busy sources throughout: no calendar, no ``ps``, no
Quartz, no real ``~/.bot-inator`` files.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from ai import delivery as dl
from ai.idea_outcomes import OutcomeStore
from events.clipboard_watcher import ClipboardWatcher
from events.event_bus import ContextEvent, EventBus
from events.mac_listeners import MacAppListener

rat = importlib.import_module("tools.read_active_tab")

# 2026-11-10 10:00 local
T0 = datetime(2026, 11, 10, 10, 0).astimezone().timestamp()


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Sources:
    """Fake busy sources; set attributes to change what the gate sees."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.meetings: list[tuple[float, float]] = []
        self.procs: set[str] = set()
        self.idle = 60.0
        self.calendar_error: Optional[Exception] = None
        self.calendar_calls = 0

    def calendar(self, now: datetime) -> list[dict[str, Any]]:
        self.calendar_calls += 1
        if self.calendar_error is not None:
            raise self.calendar_error
        return [{"title": "x", "all_day": False,
                 "start": datetime.fromtimestamp(a).astimezone().isoformat(),
                 "end": datetime.fromtimestamp(b).astimezone().isoformat()}
                for a, b in self.meetings]

    def gate(self, **kw: Any) -> dl.BusyGate:
        return dl.BusyGate(calendar=self.calendar, processes=lambda: set(self.procs),
                           seconds_since_key=lambda: self.idle, clock=self.clock,
                           calendar_ttl=kw.pop("calendar_ttl", 0.0), **kw)


class FakeOutcomes:
    def __init__(self) -> None:
        self.offers: list[dict[str, Any]] = []
        self.recorded: list[tuple] = []
        self._lock = threading.Lock()

    def record_offer(self, source: Any, tool: str, verdict: str, **kw: Any) -> str:
        self.recorded.append((source, tool, verdict, kw.get("idea_kind")))
        return "off_x"


@pytest.fixture
def settings(tmp_path: Path):
    s = dl.DeliverySettings(tmp_path / "delivery_settings.json")
    dl.set_default_settings(s)
    yield s
    dl.set_default_settings(None)


@pytest.fixture
def env(tmp_path: Path, settings):
    clock = Clock()
    src = Sources(clock)
    outcomes = FakeOutcomes()
    d = dl.Delivery(tmp_path / "held_offers.json", settings=settings, gate=src.gate(),
                    outcomes=outcomes, clock=clock)
    return SimpleNamespace(clock=clock, src=src, outcomes=outcomes, d=d, tmp=tmp_path)


def idea(trigger: str = "mail_deadline", **meta: Any) -> ContextEvent:
    base = {"source": "next_step", "trigger": trigger, "tool_name": "remind_email_deadline",
            "builtin_tool": "create_reminder", "description": "Remind me of the deadline"}
    base.update(meta)
    return ContextEvent(event_type="pattern_detected", metadata=base)


def mail() -> ContextEvent:
    return ContextEvent(event_type="mail_received",
                        metadata={"sender": "a@b.c", "subject": "Due Friday", "message_id": 7})


# ---- busy: calendar --------------------------------------------------------


def test_idea_during_meeting_is_held_then_released_after(env) -> None:
    env.src.meetings = [(T0 - 600, T0 + 1800)]
    decision = env.d.decide(idea())
    assert decision.action == "hold" and decision.reason == "meeting"
    assert not decision.record_activity
    assert env.d.release_due() == []  # still in the meeting
    env.clock.advance(1801)
    (released,) = env.d.release_due()
    assert released.event_type == "pattern_detected"
    assert released.metadata[dl.HELD_SINCE_KEY] == T0
    assert released.metadata["trigger"] == "mail_deadline"
    assert env.d.held == []
    # Re-entering the gate after the meeting: it runs.
    assert env.d.decide(released).action == "run"


def test_ambient_mail_turn_is_held_too(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 60)]
    assert env.d.decide(mail()).action == "hold"


def test_all_day_event_is_not_busy(env) -> None:
    env.src.meetings = []
    gate = dl.BusyGate(calendar=lambda now: [{"all_day": True, "start": "2026-11-10",
                                               "end": "2026-11-11"}],
                       processes=set, seconds_since_key=lambda: 99.0, clock=env.clock)
    assert gate.busy() is None


def test_chat_turn_is_never_held(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 60)]
    env.src.idle = 0.0
    env.d.pause("indefinite")
    chat = ContextEvent(event_type="user_message", metadata={"text": "hi"})
    assert env.d.decide(chat) == dl.Decision("run")
    assert env.d.held == []


def test_inbox_bypass_is_never_held(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 60)]
    assert env.d.decide(idea(**{dl.BYPASS_KEY: "inbox"})).action == "run"


def test_signal_events_run_while_busy(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 60)]
    ev = ContextEvent(event_type="app_activated", app_name="Mail")
    assert env.d.decide(ev).action == "run"


# ---- busy: screen share, typing, failures ----------------------------------


def test_screen_share_and_typing_hold(env) -> None:
    env.src.procs = {"CptHost"}
    assert env.d.decide(idea()).reason == "screen_sharing"
    env.src.procs = set()
    env.clock.advance(dl.SCREEN_SHARE_TTL_S)  # the process list is cached briefly
    env.src.idle = 1.0
    assert env.d.decide(idea()).reason == "typing"
    env.src.idle = 5.0
    assert env.d.decide(idea()).action == "run"


def test_a_busy_check_that_fails_holds_nothing(env) -> None:
    env.src.calendar_error = RuntimeError("no calendar access")

    def boom() -> float:
        raise ImportError("no Quartz")

    env.d.gate._seconds_since_key = boom
    assert env.d.decide(idea()).action == "run"
    assert env.d.held == []
    checks = env.d.status()["checks"]
    assert checks["calendar"].startswith("unavailable: RuntimeError")
    assert checks["typing"].startswith("unavailable: ImportError")
    assert checks["screen_share"] == "ok"


def test_calendar_is_cached_for_a_minute(env) -> None:
    gate = env.src.gate(calendar_ttl=60.0)
    gate.busy()
    gate.busy()
    assert env.src.calendar_calls == 1
    env.clock.advance(61)
    gate.busy()
    assert env.src.calendar_calls == 2
    # A failure is cached too.
    env.src.calendar_error = RuntimeError("slow")
    env.clock.advance(61)
    gate.busy()
    gate.busy()
    assert env.src.calendar_calls == 3


# ---- expiry ----------------------------------------------------------------


def test_held_meeting_link_past_start_is_dropped_as_expired(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 3600)]
    start = datetime.fromtimestamp(T0 + 600).astimezone().isoformat()
    ev = idea("meeting_with_video_link", tool_name="open_meeting_video_link",
              builtin_tool="open_url", event_start=start)
    assert env.d.decide(ev).action == "hold"
    assert env.d.held[0].expires_at == pytest.approx(T0 + 600)
    env.clock.advance(601)
    assert env.d.release_due() == []
    assert env.d.held == []
    assert env.outcomes.recorded == [("next_step", "open_url", "expired",
                                      "meeting_with_video_link")]


def test_meeting_link_without_start_lasts_ten_minutes() -> None:
    meta = {"trigger": "meeting_with_video_link"}
    assert dl.shelf_life("pattern_detected", meta, T0) == T0 + 600


def test_expired_ambient_event_is_dropped_without_an_outcome(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 3 * 86400)]
    env.d.decide(mail())
    env.clock.advance(86401)
    env.d.expire()
    assert env.d.held == [] and env.outcomes.recorded == []


def test_time_routine_expires_at_end_of_its_slot() -> None:
    since = datetime(2026, 11, 10, 9, 40).astimezone().timestamp()
    end = datetime(2026, 11, 10, 10, 0).astimezone().timestamp()
    assert dl.shelf_life("pattern_detected", {"trigger": "time_routine"}, since) == end


def test_expired_verdict_tolerates_store_without_it(env, tmp_path) -> None:
    # Until the runner adds "expired" to idea_outcomes.VERDICTS the real
    # store raises ValueError; delivery must not crash.
    env.d.outcomes = OutcomeStore(None, clock=env.clock)
    env.src.meetings = [(T0 - 60, T0 + 86400 * 3)]
    env.d.decide(idea())
    env.clock.advance(86401)
    assert env.d.expire() == 1


# ---- daily cap -------------------------------------------------------------


def test_ninth_agent_card_of_the_day_goes_to_the_inbox(env) -> None:
    store = OutcomeStore(None, clock=env.clock)
    env.d.outcomes = store
    store.record_shown("chat", "x")  # user-caused: never counted
    for _ in range(7):
        store.record_shown("next_step", "create_reminder")
    env.d.note_ambient_card()  # an ambient turn's card counts too
    assert env.d.cards_today() == 8
    decision = env.d.decide(idea())
    assert decision.action == "inbox"
    assert env.d.status()["inbox_count"] == 1
    # The cap never applies to chat.
    assert env.d.decide(ContextEvent(event_type="user_message")).action == "run"
    # Opening it from the Inbox: user-caused, runs, and is not counted.
    (entry,) = env.d.inbox
    event = env.d.open_inbox(entry.id)
    assert event is not None and event.metadata[dl.BYPASS_KEY] == "inbox"
    assert env.d.decide(event).action == "run"
    store.record_shown("next_step", "create_reminder")  # its card
    assert env.d.cards_today() == 8


def test_eighth_card_still_shows_and_counts_reset_next_day(env) -> None:
    store = OutcomeStore(None, clock=env.clock)
    env.d.outcomes = store
    for _ in range(7):
        store.record_shown("next_step", "create_reminder")
    assert env.d.decide(idea()).action == "run"
    store.record_shown("next_step", "create_reminder")
    assert env.d.decide(idea()).action == "inbox"
    env.clock.advance(86400)
    assert env.d.cards_today() == 0
    assert env.d.decide(idea()).action == "run"


def test_cap_is_a_setting(env, settings) -> None:
    settings.update(daily_cap=0)
    assert env.d.decide(idea()).action == "inbox"
    settings.update(daily_cap=3)
    assert env.d.decide(idea()).action == "run"


def test_ambient_turns_are_not_capped(env, settings) -> None:
    settings.update(daily_cap=0)
    assert env.d.decide(mail()).action == "run"


# ---- pause -----------------------------------------------------------------


def test_pause_stops_turns_and_activity_resume_restores_without_restart(env) -> None:
    env.d.pause("1h")
    app = ContextEvent(event_type="app_activated", app_name="Mail")
    dropped = env.d.decide(app)
    assert dropped.action == "drop" and not dropped.record_activity
    held = env.d.decide(mail())
    assert held.action == "hold" and held.reason == "paused" and not held.record_activity
    assert env.d.release_due() == []  # paused: nothing comes out
    env.d.resume()
    # Same Delivery object, listeners untouched: events flow again.
    after = env.d.decide(app)
    assert after.action == "run" and after.record_activity
    (released,) = env.d.release_due()
    # Happened during the pause: runs now, never reaches the activity history.
    again = env.d.decide(released)
    assert again.action == "run" and not again.record_activity


def test_pause_one_hour_ends_by_itself(env) -> None:
    env.d.pause("1h")
    assert env.d.paused
    env.clock.advance(3601)
    assert not env.d.paused
    assert env.d.decide(ContextEvent(event_type="app_activated")).action == "run"


def test_pause_until_tomorrow_is_eight_am(env) -> None:
    until = env.d.pause("tomorrow")
    assert datetime.fromtimestamp(until).astimezone() == datetime(2026, 11, 11, 8, 0).astimezone()
    env.clock.t = datetime(2026, 11, 11, 2, 0).astimezone().timestamp()
    until = env.d.pause("tomorrow")
    assert datetime.fromtimestamp(until).astimezone() == datetime(2026, 11, 11, 8, 0).astimezone()


def test_pause_indefinite_survives_restart(env, settings) -> None:
    env.d.pause("indefinite")
    reborn = dl.Delivery(env.tmp / "held_offers.json", settings=settings,
                         gate=env.src.gate(), clock=env.clock)
    assert reborn.paused and reborn.status()["paused_until"] == "indefinite"
    with pytest.raises(ValueError):
        env.d.pause("forever")


def test_pause_is_a_busy_check(env) -> None:
    env.d.pause("1h")
    assert env.d.gate.busy() == "paused"


# ---- persistence + privacy -------------------------------------------------


def test_held_offers_persist_but_message_text_never_does(env, settings) -> None:
    env.src.meetings = [(T0 - 60, T0 + 3600)]
    env.d.decide(idea())
    imsg = ContextEvent(event_type="imessage_received",
                        metadata={"sender": "Mom", "text": "the secret code is 4471"})
    assert env.d.decide(imsg).action == "hold"
    path = env.tmp / "held_offers.json"
    raw = path.read_text()
    assert "4471" not in raw and "Mom" not in raw
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    reborn = dl.Delivery(path, settings=settings, gate=env.src.gate(), clock=env.clock)
    assert [h.event_type for h in reborn.held] == ["pattern_detected"]
    # In memory both are kept.
    assert len(env.d.held) == 2


def test_release_is_one_per_tick_oldest_first(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 60)]
    env.d.decide(idea(description="first"))
    env.clock.advance(1)
    env.d.decide(idea(description="second"))
    env.clock.advance(120)
    (a,) = env.d.release_due()
    (b,) = env.d.release_due()
    assert [a.metadata["description"], b.metadata["description"]] == ["first", "second"]


def test_status_and_debug_line(env) -> None:
    assert env.d.debug_line() == "held: 0"
    env.src.meetings = [(T0 - 60, T0 + 60)]
    env.d.decide(idea())
    env.d.decide(mail())
    s = env.d.status()
    assert s["held_count"] == 2 and s["held_reasons"] == {"meeting": 2}
    assert s["held"][0]["title"] == "Remind me of the deadline"
    assert env.d.debug_line() == "held: 2 (meeting 2)"


def test_on_change_fires(env) -> None:
    calls: list[int] = []
    env.d.on_change = lambda: calls.append(1)
    env.d.pause("1h")
    env.d.resume()
    assert len(calls) == 2


def test_release_loop_pushes_onto_the_bus(env) -> None:
    env.src.meetings = [(T0 - 60, T0 + 60)]
    env.d.decide(idea())
    env.clock.advance(120)

    async def run() -> ContextEvent:
        bus = EventBus()
        task = asyncio.create_task(env.d.run_release_loop(bus, interval=0.01))
        try:
            return await asyncio.wait_for(bus.consume(), timeout=2.0)
        finally:
            task.cancel()

    event = asyncio.run(run())
    assert event.event_type == "pattern_detected"


# ---- never-watch -----------------------------------------------------------


def test_never_watch_seed_and_settings(settings) -> None:
    assert dl.is_never_watched("com.1password.1password")
    assert dl.is_never_watched("COM.BITWARDEN.DESKTOP")
    assert not dl.is_never_watched("com.apple.mail", "Mail")
    settings.update(never_watch=[e for e in dl.NEVER_WATCH_SEED
                                 if e != "com.bitwarden.desktop"] + ["com.example.vault", "Secrets"])
    assert not dl.is_never_watched("com.bitwarden.desktop")
    assert dl.is_never_watched("com.example.vault")
    assert dl.is_never_watched(None, "secrets")  # a dotless entry is an app name
    reread = dl.DeliverySettings(settings.path)
    assert "com.example.vault" in reread.never_watch()
    assert "com.bitwarden.desktop" not in reread.never_watch()


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def push_threadsafe(self, event) -> None:
        self.events.append(event)


def _wait(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_never_watch_app_activation_produces_no_event(settings) -> None:
    bus = _Bus()
    listener = MacAppListener(bus, debounce_s=0.01)
    listener._handle_line(json.dumps({"app_name": "1Password",
                                      "bundle_id": "com.1password.1password", "pid": 9}))
    time.sleep(0.1)
    assert bus.events == []
    listener._handle_line(json.dumps({"app_name": "Mail", "bundle_id": "com.apple.mail", "pid": 3}))
    assert _wait(lambda: len(bus.events) == 1)
    assert bus.events[0].app_name == "Mail"


def test_switch_into_never_watch_app_cancels_pending_and_return_is_reported(settings) -> None:
    bus = _Bus()
    listener = MacAppListener(bus, debounce_s=0.2)
    mail_line = json.dumps({"app_name": "Mail", "bundle_id": "com.apple.mail", "pid": 3})
    listener._handle_line(mail_line)
    assert _wait(lambda: len(bus.events) == 1, 1.0)
    listener._handle_line(json.dumps({"app_name": "Notes", "bundle_id": "com.apple.Notes", "pid": 4}))
    listener._handle_line(json.dumps({"app_name": "1Password",
                                      "bundle_id": "com.1password.1password", "pid": 9}))
    time.sleep(0.4)
    assert [e.app_name for e in bus.events] == ["Mail"]  # Notes never held focus
    listener._handle_line(mail_line)
    assert _wait(lambda: len(bus.events) == 2, 1.0)


class _Pasteboard:
    def __init__(self) -> None:
        self.count = 1

    def changeCount(self) -> int:
        return self.count

    def types(self) -> list[str]:
        return ["public.utf8-plain-text"]


def test_never_watch_app_copy_produces_no_event(settings) -> None:
    pb = _Pasteboard()
    front = {"bundle": "com.bitwarden.desktop", "name": "Bitwarden"}
    w = ClipboardWatcher(EventBus(), pasteboard_factory=lambda: pb,
                         frontmost_app=lambda: front["name"],
                         frontmost_bundle=lambda: front["bundle"])
    w.poll_once()
    pb.count = 2
    assert w.poll_once() is None
    front.update(bundle="com.apple.mail", name="Mail")
    pb.count = 3
    ev = w.poll_once()
    assert ev is not None and ev.metadata == {"app": "Mail", "kind": "text"}


def test_clipboard_name_entry_matches_without_bundle(settings) -> None:
    settings.update(never_watch=list(dl.NEVER_WATCH_SEED) + ["Vaulty"])
    pb = _Pasteboard()
    w = ClipboardWatcher(EventBus(), pasteboard_factory=lambda: pb, frontmost_app=lambda: "Vaulty")
    w.poll_once()
    pb.count = 2
    assert w.poll_once() is None


class _FakeRun:
    """``subprocess.run`` for read_active_tab_text with a chosen front app."""

    def __init__(self, front_bundle: str, front_name: str) -> None:
        self.front_bundle, self.front_name = front_bundle, front_name
        self.js_calls = 0

    def __call__(self, argv: list[str], **kw: Any) -> SimpleNamespace:
        if argv[:2] == ["lsappinfo", "front"]:
            return SimpleNamespace(returncode=0, stdout="ASN:0x0-0x9:\n", stderr="")
        if argv[:2] == ["lsappinfo", "info"]:
            out = (f'"CFBundleIdentifier"="{self.front_bundle}"\n'
                   f'"LSDisplayName"="{self.front_name}"\n')
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if argv[0] == "lsappinfo":
            return SimpleNamespace(returncode=0, stdout='ASN:0x0-0x3-"Google_Chrome": ', stderr="")
        script = argv[2]
        if script == rat._META_SCRIPTS[rat.CHROME]:
            return SimpleNamespace(returncode=0, stdout="https://news.example.com/a\nNews\n", stderr="")
        if script in rat._META_SCRIPTS.values():
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        self.js_calls += 1
        return SimpleNamespace(returncode=0, stdout="page body\n", stderr="")


@pytest.fixture
def local_engine(monkeypatch):
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: False)


def test_read_active_tab_text_refuses_while_never_watch_app_is_front(
        settings, monkeypatch, local_engine) -> None:
    fake = _FakeRun("com.1password.1password", "1Password")
    monkeypatch.setattr(rat.subprocess, "run", fake)
    out = rat.read_active_tab_text()
    assert out["error"] == rat.NEVER_WATCH_ERROR
    assert out["text"] == "" and out["url"] == "" and out["title"] == ""
    assert fake.js_calls == 0
    fake.front_bundle, fake.front_name = "com.google.Chrome", "Google Chrome"
    out = rat.read_active_tab_text()
    assert out.get("error") is None and out["text"] == "page body"
    assert fake.js_calls == 1


def test_read_active_tab_text_refuses_a_never_watch_browser(
        settings, monkeypatch, local_engine) -> None:
    settings.update(never_watch=list(dl.NEVER_WATCH_SEED) + ["com.google.Chrome"])
    fake = _FakeRun("com.apple.finder", "Finder")
    monkeypatch.setattr(rat.subprocess, "run", fake)
    assert rat.read_active_tab_text()["error"] == rat.NEVER_WATCH_ERROR
    assert fake.js_calls == 0


# ---- routes ----------------------------------------------------------------


@pytest.fixture
def client(env):
    from routes import delivery as routes

    app = FastAPI()
    app.include_router(routes.router)
    app.state.delivery = env.d
    app.state.event_bus = EventBus()
    with TestClient(app) as c:
        c.bus = app.state.event_bus  # type: ignore[attr-defined]
        yield c


def test_routes_pause_resume_and_status(client, env) -> None:
    body = client.post("/api/delivery/pause", json={"duration": "1h"}).json()
    assert body["paused"] is True and body["paused_until"]
    assert client.post("/api/delivery/pause", json={"duration": "nope"}).status_code == 422
    body = client.post("/api/delivery/resume").json()
    assert body["paused"] is False and body["debug_line"] == "held: 0"
    body = client.post("/api/delivery/pause", json={"minutes": 5}).json()
    assert body["paused_until"] == dl._iso(T0 + 300)


def test_routes_inbox_open_and_dismiss(client, env, settings) -> None:
    settings.update(daily_cap=0)
    env.d.decide(idea(description="one"))
    env.d.decide(idea(description="two"))
    items = client.get("/api/delivery/inbox").json()
    assert [i["title"] for i in items] == ["one", "two"]
    r = client.post(f"/api/delivery/inbox/{items[0]['id']}/open")
    assert r.status_code == 200
    pushed = client.bus._queue.get_nowait()  # type: ignore[attr-defined]
    assert pushed.metadata[dl.BYPASS_KEY] == "inbox"
    assert client.delete(f"/api/delivery/inbox/{items[1]['id']}").json()["dismissed"]
    assert client.get("/api/delivery/inbox").json() == []
    assert client.post("/api/delivery/inbox/nope/open").status_code == 404


def test_routes_settings(client) -> None:
    body = client.get("/api/delivery/settings").json()
    assert body["daily_cap"] == 8 and "com.1password.1password" in body["never_watch"]
    body = client.put("/api/delivery/settings",
                      json={"daily_cap": 3, "never_watch": ["com.example.vault"]}).json()
    assert body["daily_cap"] == 3 and body["never_watch"] == ["com.example.vault"]
    assert client.put("/api/delivery/settings", json={"daily_cap": -1}).status_code == 422


def test_route_503_without_delivery() -> None:
    from routes import delivery as routes

    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as c:
        assert c.get("/api/delivery").status_code == 503
