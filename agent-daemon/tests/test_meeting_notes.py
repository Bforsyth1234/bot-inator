"""#86: meeting follow-through — local notes, action items as one card."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ai import prepared_offers
from events import meeting_notes as MN
from events.event_bus import ContextEvent

FIXTURE = Path(__file__).parent / "fixtures" / "meeting_notes_transcript.txt"
TZ = datetime(2026, 11, 10, 13, 50).astimezone().tzinfo
NOW = datetime(2026, 11, 10, 13, 50, tzinfo=TZ)  # a Tuesday
START = datetime(2026, 11, 10, 14, 0, tzinfo=TZ)
END = datetime(2026, 11, 10, 14, 30, tzinfo=TZ)
ATTENDEES = [
    {"name": "Sam Rivera", "email": "sam@example.com", "is_self": True},
    {"name": "Alice Chen", "email": "alice@example.com", "is_self": False},
    {"name": "Bob Diaz", "email": "mailto:bob@example.com", "is_self": False},
]
META = {"title": "Design sync", "start": START.isoformat(), "end": END.isoformat(),
        "id": "EV1", "attendees": ATTENDEES, "video_url": "https://meet.example/x"}


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(MN.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


class FakeTimer:
    def __init__(self) -> None:
        self.calls: list[tuple[float, Any]] = []

    def __call__(self, delay, fn):
        self.calls.append((delay, fn))
        return self


class FakeLink(MN.RecorderLink):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[tuple[str, dict]] = []

    def send(self, frame_type, payload):
        self.frames.append((frame_type, dict(payload)))
        return True


def make(**kw) -> tuple[MN.MeetingNotes, FakeLink, FakeTimer, list[ContextEvent]]:
    link, timer, published = FakeLink(), FakeTimer(), []

    async def publish(ev):
        published.append(ev)

    kw.setdefault("enabled", lambda: True)
    notes = MN.MeetingNotes(link=link, clock=lambda: NOW, timer=timer,
                            lead_minutes=lambda: 10, engine_name=lambda: "apple",
                            full_name="", publish=publish, **kw)
    return notes, link, timer, published


def transcript() -> str:
    return FIXTURE.read_text("utf-8")


# ── extraction from the fixture ──────────────────────────────────────────

def test_users_action_items_get_due_dates() -> None:
    s = MN.extract(transcript(), date(2026, 11, 10), {"sam rivera", "sam"}, ATTENDEES, "Design sync")
    mine = {a.task: a.due for a in s.mine}
    assert mine == {
        "Send the deck": "2026-11-13",                       # Friday
        "Book the venue for the offsite": "2026-11-16",      # next week
        "Write the launch checklist": "",
    }
    others = {a.task: a.owner for a in s.action_items if not a.owner_is_user}
    assert others["Update the budget sheet"] == "Alice Chen"
    assert "Draft the press release" in others  # "I'll" on the others' channel
    assert s.decisions == ["We decided to ship the beta on December 1"]
    assert s.follow_up is not None and s.follow_up.start == "2026-11-16T15:00"


def test_unlabelled_first_person_is_the_user() -> None:
    s = MN.extract("I'll send the deck by Friday.", date(2026, 11, 10), set())
    assert [(a.task, a.due, a.owner_is_user) for a in s.action_items] == [
        ("Send the deck", "2026-11-13", True)]


@pytest.mark.parametrize("text,due", [
    ("by tomorrow", "2026-11-11"), ("by end of day", "2026-11-10"),
    ("by Friday", "2026-11-13"), ("by Tuesday", "2026-11-17"),
    ("next Thursday", "2026-11-19"), ("by the end of the week", "2026-11-13"),
    ("by November 20th", "2026-11-20"), ("by the 3rd of January", "2027-01-03"),
    ("end of the month", "2026-11-30"), ("no date here", ""),
])
def test_find_due(text, due) -> None:
    assert MN.find_due(text, date(2026, 11, 10))[0] == due


# ── one prepared offer ───────────────────────────────────────────────────

def offer_from_fixture() -> Any:
    s = MN.extract(transcript(), date(2026, 11, 10), {"sam rivera", "sam"}, ATTENDEES, "Design sync")
    return MN.build_offer({**META, "meeting_id": "m1"}, s, "NOTES")


def test_offer_is_one_bundle_card() -> None:
    offer = offer_from_fixture()
    tools = [a.tool for a in offer.actions]
    assert tools == ["create_reminder"] * 3 + ["create_event", "draft_email", "create_note"]
    reminders = [a.args for a in offer.actions if a.tool == "create_reminder"]
    assert reminders[0] == {"title": "Send the deck", "due": "2026-11-13",
                            "notes": "From Design sync on 2026-11-10"}
    draft = next(a for a in offer.actions if a.tool == "draft_email")
    assert draft.args["to"] == "alice@example.com, bob@example.com"
    assert set(a.tool for a in offer.actions) <= prepared_offers.BUNDLE_TOOLS
    card = offer.to_card()
    assert card["tool_name"] == prepared_offers.PREPARED_TOOL_NAME
    assert "Decided: We decided to ship" in card["reasoning"]
    assert "Send the deck (by 2026-11-13)" in card["reasoning"]


def test_unticking_one_runs_the_rest() -> None:
    offer = offer_from_fixture()
    ran: list[tuple[str, dict]] = []
    table = {name: (lambda name=name: (lambda **kw: ran.append((name, kw)) or {"status": "ok"}))()
             for name in prepared_offers.BUNDLE_TOOLS}
    returned = [a.to_dict() for a in offer.actions]
    returned[0]["enabled"] = False  # untick "Send the deck"
    results = prepared_offers.run_approved_actions(offer.to_dict(), {"actions": returned}, table)
    assert [r["tool"] for r in results] == [a.tool for a in offer.actions[1:]]
    titles = [kw.get("title") for name, kw in ran if name == "create_reminder"]
    assert "Send the deck" not in titles and "Write the launch checklist" in titles


def test_process_publishes_one_card_and_runs_nothing(monkeypatch, home) -> None:
    called: list[str] = []
    monkeypatch.setattr(prepared_offers, "run_approved_actions", lambda *a, **k: called.append("run"))
    monkeypatch.setattr(prepared_offers, "default_tools", lambda: called.append("tools") or {})
    notes, _, _, published = make()
    notes.observe(ContextEvent(event_type="event_starting_soon", metadata=META))
    mid = MN.meeting_id_for(META)
    tpath = MN.notes_dir() / f"{mid}.txt"
    tpath.write_text(transcript(), "utf-8")
    out = asyncio.run(notes.process({"meeting_id": mid, "transcript_path": str(tpath)}))
    assert out["status"] == "offered"
    assert called == []  # no tool ran, no tool table was even loaded
    assert len(published) == 1 and published[0].event_type == "pattern_detected"
    meta = published[0].metadata
    offer = prepared_offers.PreparedOffer.from_dict(meta[prepared_offers.PREPARED_KEY])
    assert sum(a.tool == "create_reminder" for a in offer.actions) == 3
    assert (tpath.stat().st_mode & 0o777) == 0o600


# ── privacy: the transcript never reaches a cloud engine ──────────────────

def test_cloud_main_engine_never_gets_the_transcript(monkeypatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: True)
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: True)  # opt-in doesn't apply
    prompts: list[str] = []

    async def cloud(prompt: str) -> str:
        prompts.append(prompt)
        return '{"summary": "x"}'

    notes, _, _, published = make(evaluators=[("main", cloud)])
    ev = asyncio.run(notes.summarize_to_event({**META, "meeting_id": "m1"}, transcript()))
    assert prompts == []
    meta = ev.metadata
    offer = meta[prepared_offers.PREPARED_KEY]
    assert MN.CLOUD_SKIPPED in next(a["args"]["body"] for a in offer["actions"] if a["tool"] == "create_note")
    # Outside the card itself, nothing quotes the transcript.
    rest = {k: v for k, v in meta.items() if k != prepared_offers.PREPARED_KEY}
    assert "deck" not in str(rest) and "budget" not in str(rest)
    assert meta["event_details_redacted"] == privacy.PLACEHOLDER


def test_local_engine_words_the_summary() -> None:
    prompts: list[str] = []

    async def local(prompt: str) -> str:
        prompts.append(prompt)
        return '{"summary": "Beta ships December 1.", "decisions": []}'

    async def cloud(prompt: str) -> str:
        raise AssertionError("cloud engine called")

    notes, *_ = make(evaluators=[("main", cloud), ("analysis", local)],
                     is_cloud=lambda role: role == "main")
    s = asyncio.run(notes.summarize({**META, "meeting_id": "m1"}, transcript()))
    assert len(prompts) == 1 and "send the deck" in prompts[0]
    assert s.summary == "Beta ships December 1." and s.summary_note == ""


# ── audio is deleted after transcription and on failure ───────────────────

def _audio(name: str = "rec.m4a") -> Path:
    p = MN.audio_dir() / name
    p.write_bytes(b"\0" * 16)
    return p


def test_audio_deleted_after_transcription() -> None:
    audio = _audio()
    seen: list[bool] = []

    def transcriber(path: str) -> str:
        seen.append(Path(path).exists())
        return transcript()

    notes, _, _, published = make(transcriber=transcriber)
    out = asyncio.run(notes.process({"meeting_id": "m1", "title": "Call", "audio_path": str(audio)}))
    assert seen == [True] and out["status"] == "offered"
    assert not audio.exists() and len(published) == 1


def test_audio_deleted_when_transcription_fails() -> None:
    audio = _audio()

    def boom(path: str) -> str:
        raise RuntimeError("model missing")

    notes, _, _, published = make(transcriber=boom)
    out = asyncio.run(notes.process({"meeting_id": "m1", "audio_path": str(audio)}))
    assert out["status"] == "error" and not audio.exists() and published == []


def test_audio_deleted_when_the_app_reports_failure() -> None:
    audio = _audio()
    notes, *_ = make()
    out = asyncio.run(notes.process({"meeting_id": "m1", "audio_path": str(audio),
                                     "error": "speech recognizer unavailable"}))
    assert out["status"] == "error" and not audio.exists()


def test_audio_deleted_alongside_an_app_transcript() -> None:
    audio = _audio()
    tpath = MN.notes_dir() / "m1.txt"
    tpath.write_text(transcript(), "utf-8")
    notes, *_ = make()
    asyncio.run(notes.process({"meeting_id": "m1", "audio_path": str(audio),
                               "transcript_path": str(tpath)}))
    assert not audio.exists()


def test_never_deletes_outside_the_audio_folder(home) -> None:
    other = home / "keep.m4a"
    other.write_bytes(b"x")
    assert MN.delete_audio(other) is False and other.exists()
    with pytest.raises(ValueError):
        MN.read_transcript(home / "elsewhere.txt")


# ── recording never starts by itself; it stops at end + 5 minutes ─────────

def test_only_an_offer_is_sent_never_a_start() -> None:
    notes, link, timer, _ = make()
    ev = ContextEvent(event_type="event_starting_soon", metadata=META)
    assert notes.observe(ev) == 0.0  # prep moment (start - 10 min) is now
    assert notes.observe(ev) is None  # once per meeting
    _, fire = timer.calls[0]
    fire()
    assert [t for t, _ in link.frames] == [MN.FRAME_OFFER]
    payload = link.frames[0][1]
    assert payload["stop_at"] == (END + MN.STOP_GRACE).isoformat(timespec="seconds")
    assert notes.recording is None  # the offer started nothing
    assert len(timer.calls) == 1   # and armed no stop timer


def test_no_offer_when_off_or_solo() -> None:
    notes, link, timer, _ = make(enabled=lambda: False)
    assert notes.observe(ContextEvent(event_type="event_starting_soon", metadata=META)) is None
    solo = {**META, "attendees": [ATTENDEES[0]]}
    notes2, _, timer2, _ = make()
    assert notes2.observe(ContextEvent(event_type="event_starting_soon", metadata=solo)) is None
    assert timer.calls == [] and timer2.calls == [] and link.frames == []


def test_started_by_click_stops_at_end_plus_five() -> None:
    notes, link, timer, _ = make()
    notes.observe(ContextEvent(event_type="event_starting_soon", metadata=META))
    mid = MN.meeting_id_for(META)
    notes.on_record_state("c1", {"meeting_id": mid, "state": "started"})
    delay, fire = timer.calls[-1]
    assert delay == (END + timedelta(minutes=5) - NOW).total_seconds()
    assert notes.recording == mid
    fire()
    assert link.frames[-1] == (MN.FRAME_STOP, {"meeting_id": mid, "reason": "meeting_ended"})


def test_stopped_by_click_cancels_the_stop_timer() -> None:
    class Handle:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    handles: list[Handle] = []

    def timer(delay, fn):
        handles.append(Handle())
        return handles[-1]

    notes = MN.MeetingNotes(link=FakeLink(), clock=lambda: NOW, timer=timer, full_name="")
    notes.on_record_state("c1", {"meeting_id": "m1", "state": "started"})
    notes.on_record_state("c1", {"meeting_id": "m1", "state": "stopped"})
    assert handles[0].cancelled and notes.recording is None


def test_menu_item_recording_is_capped() -> None:
    notes, _, timer, _ = make()
    far = (NOW + timedelta(hours=9)).isoformat()
    notes.on_record_state("c1", {"meeting_id": "manual1", "state": "started", "stop_at": far})
    assert timer.calls[-1][0] == MN.MANUAL_MAX.total_seconds()


def test_state_feeds_permission_rows() -> None:
    from ai import permissions as P

    notes, *_ = make()
    try:
        notes.on_record_state("c1", {"state": "idle", "microphone": "authorized",
                                     "speech_recognition": "denied"})
        assert P._row(P.get("microphone"))["status"] == P.GRANTED
        assert P._row(P.get("speech_recognition"))["status"] == P.DENIED
    finally:
        P.set_external_check("microphone", None)
        P.set_external_check("speech_recognition", None)


# ── link and settings ─────────────────────────────────────────────────────

def test_link_sends_only_to_the_app_that_reported_state() -> None:
    sent: list[tuple[str, dict]] = []

    async def main() -> None:
        link = MN.RecorderLink()

        async def sender(t, p):
            sent.append((t, p))

        link.connect("c1", sender, asyncio.get_running_loop())
        assert link.send(MN.FRAME_OFFER, {"x": 1}) is False  # debug client, no state yet
        link.advertise("c1")
        assert link.send(MN.FRAME_OFFER, {"x": 1}) is True
        await asyncio.sleep(0)
        link.disconnect("c1")
        assert link.send(MN.FRAME_OFFER, {}) is False

    asyncio.run(main())
    assert sent == [(MN.FRAME_OFFER, {"x": 1})]


def test_engine_falls_back_to_on_device(monkeypatch) -> None:
    monkeypatch.setattr(MN, "_setting", lambda key, default: "whisper" if key == MN.ENGINE_KEY else default)
    monkeypatch.setattr(MN, "whisper_available", lambda: False)
    assert MN.engine() == "apple"
    monkeypatch.setattr(MN, "whisper_available", lambda: True)
    assert MN.engine() == "whisper"
    with pytest.raises(ValueError):
        MN.update_settings(engine_name="cloud")


def test_notes_off_by_default(monkeypatch) -> None:
    monkeypatch.setattr(MN, "_setting", lambda key, default: default)
    assert MN.notes_enabled() is False


def test_transcribe_audio_deletes_on_its_own() -> None:
    ok, bad = _audio("a.m4a"), _audio("b.m4a")
    assert MN.transcribe_audio(ok, lambda p: "hi") == "hi" and not ok.exists()

    def boom(path: str) -> str:
        raise MN.TranscriptionError("no model")

    with pytest.raises(MN.TranscriptionError):
        MN.transcribe_audio(bad, boom)
    assert not bad.exists()


def test_whisper_engine_tags_channels_and_deletes_both() -> None:
    mic, sys_audio = _audio("m1-me.m4a"), _audio("m1-others.m4a")
    said = {"m1-me.m4a": "I'll send the deck by Friday.",
            "m1-others.m4a": "I'll draft the press release by Thursday."}
    notes, _, _, published = make(transcriber=lambda p: said[Path(p).name])
    out = asyncio.run(notes.process({"meeting_id": "m1", "title": "Call", "started_at": START.isoformat(),
                                     "audio_path": str(mic), "system_audio_path": str(sys_audio)}))
    assert out["status"] == "offered" and not mic.exists() and not sys_audio.exists()
    offer = published[0].metadata[prepared_offers.PREPARED_KEY]
    reminders = [a["args"]["title"] for a in offer["actions"] if a["tool"] == "create_reminder"]
    assert reminders == ["Send the deck"]  # the others' "I'll" is not the user's


def test_second_audio_deleted_when_first_fails() -> None:
    mic, sys_audio = _audio("m2-me.m4a"), _audio("m2-others.m4a")

    def boom(path: str) -> str:
        raise RuntimeError("model missing")

    notes, *_ = make(transcriber=boom)
    out = asyncio.run(notes.process({"meeting_id": "m2", "audio_path": str(mic),
                                     "system_audio_path": str(sys_audio)}))
    assert out["status"] == "error" and not mic.exists() and not sys_audio.exists()
