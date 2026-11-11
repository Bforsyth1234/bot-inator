"""Look-ahead gap check (#77): each detector finds its gap, stays quiet when
there is none, and is skipped and named when its source is unavailable."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ai import privacy
from events import lookahead as la
from events.event_bus import ContextEvent

TZ = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=TZ)  # Tuesday


def at(day: int, hour: int, minute: int = 0) -> str:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).isoformat()


def ev(title, day, hour, minutes=60, location="", notes="", url="", all_day=False, start_minute=0):
    start = datetime(2026, 11, day, hour, start_minute, tzinfo=TZ)
    return {"title": title, "start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(),
            "location": location, "notes": notes, "url": url, "all_day": all_day}


def mail(mid, subject, sender="Alice <alice@example.com>", days_ago=1, mailbox="INBOX"):
    received = (NOW - timedelta(days=days_ago)).astimezone(timezone.utc).isoformat()
    return {"message_id": mid, "sender": sender, "subject": subject, "date": received, "mailbox": mailbox}


def events_src(*events):
    calls = []

    def fn(start="", end=""):
        calls.append((start, end))
        return {"status": "ok", "events": list(events)}
    fn.calls = calls
    return fn


def mail_src(*messages):
    def fn(since):
        return {"status": "ok", "messages": [m for m in messages]}
    return fn


def reminders_src(*items, truncated=False):
    def fn(list_name="", include_completed=False):
        assert list_name == "" and include_completed is False
        return {"status": "ok", "reminders": list(items), "truncated": truncated}
    return fn


def down(reason):
    def fn(*a, **k):
        return {"status": "error", "error": reason}
    return fn


def boom(*a, **k):
    raise RuntimeError("source exploded")


def run(events=(), messages=(), reminders=(), **kw):
    kw.setdefault("list_events", events_src(*events))
    kw.setdefault("recent_mail", mail_src(*messages))
    kw.setdefault("list_reminders", reminders_src(*reminders))
    kw.setdefault("redact_subject", lambda s: s)
    return la.check_gaps(NOW, **kw)


def of(result, detector):
    return [g for g in result["gaps"] if g["detector"] == detector]


def skipped(result):
    return {s["detector"]: s["reason"] for s in result["skipped"]}


# ---- window and sources -------------------------------------------------------

def test_reads_three_days_of_events_plus_travel_lookback():
    src = events_src()
    run(list_events=src)
    assert src.calls == [("2026-10-27", "2026-11-12")]


def test_nothing_missing_means_no_gap_lines():
    result = run(events=[ev("Standup", 10, 9, location="Room 1")],
                 messages=[mail(1, "Lunch?")])
    assert result["gaps"] == []
    # Only the always-skipped file check is named.
    assert result["text"] == "Look-ahead skipped: unopened files (" + la.NO_FILE_HISTORY + ")"


# ---- (a) unopened linked file ---------------------------------------------------

DOC = "https://docs.google.com/document/d/abc123/edit"


def test_unopened_file_skipped_and_named_without_history():
    result = run(events=[ev("Budget review", 11, 10, notes=f"Agenda: {DOC}")])
    assert of(result, "unopened_file") == []
    assert skipped(result)["unopened_file"] == la.NO_FILE_HISTORY
    assert "unopened files" in result["text"]


def test_unopened_file_found_with_history():
    seen = []

    def opened(ref):
        seen.append(ref)
        return {"status": "ok", "opened": False}
    result = run(events=[ev("Budget review", 11, 10, notes=f"Agenda: {DOC}")], file_opened=opened)
    gaps = of(result, "unopened_file")
    assert seen == [DOC]
    assert len(gaps) == 1 and "not opened yet" in gaps[0]["line"]
    assert gaps[0]["offer"]["builtin_tool"] == "open_url"
    assert gaps[0]["offer"]["arguments"] == {"url": DOC}
    assert "unopened_file" not in skipped(result)


def test_local_linked_file_offers_a_preview():
    result = run(events=[ev("Plan", 11, 10, notes="see /Users/me/Documents/plan.pdf")],
                 file_opened=lambda ref: {"status": "ok", "opened": False})
    gap = of(result, "unopened_file")[0]
    assert gap["offer"]["builtin_tool"] == "summarize_file"
    assert gap["offer"]["arguments"] == {"path": "/Users/me/Documents/plan.pdf"}
    assert "plan.pdf" in gap["line"]


def test_opened_or_unknown_file_is_not_a_gap():
    events = [ev("Budget review", 11, 10, notes=DOC)]
    assert of(run(events=events, file_opened=lambda r: {"status": "ok", "opened": True}), "unopened_file") == []
    assert of(run(events=events, file_opened=lambda r: {"status": "ok"}), "unopened_file") == []
    assert of(run(events=events, file_opened=boom), "unopened_file") == []


def test_unopened_file_skipped_when_calendar_unavailable():
    result = run(list_events=down("not granted"), file_opened=lambda r: {"status": "ok", "opened": False})
    assert "calendar unavailable: not granted" in skipped(result)["unopened_file"]


# ---- (b) travel ----------------------------------------------------------------

def test_flight_without_confirmation_mail_is_a_gap():
    result = run(events=[ev("Flight to Denver", 11, 9, minutes=150)],
                 messages=[mail(5, "Hotel reservation confirmed: Hilton Denver", sender="Hilton <x@hilton.com>")])
    gaps = of(result, "travel")
    assert len(gaps) == 1
    assert "no flight confirmation" in gaps[0]["line"]
    assert gaps[0]["evidence"]["leg"] == "flight"
    assert gaps[0]["offer"]["builtin_tool"] == "create_reminder"


def test_outbound_flight_with_no_hotel_is_a_gap():
    result = run(events=[ev("Flight to Denver", 11, 9, minutes=150)],
                 messages=[mail(5, "Your United flight confirmation", sender="United <u@united.com>", days_ago=20)])
    gaps = of(result, "travel")
    assert [g["evidence"]["leg"] for g in gaps] == ["hotel"]
    assert "no hotel booking" in gaps[0]["line"]


def test_confirmed_trip_has_no_travel_gap():
    result = run(events=[ev("Flight to Denver", 11, 9, minutes=150)],
                 messages=[mail(5, "Your United flight confirmation", sender="United <u@united.com>", days_ago=20),
                           mail(6, "Booking confirmation - Hilton Denver", sender="Hilton <h@hilton.com>", days_ago=19)])
    assert of(result, "travel") == []


def test_return_flight_and_day_trip_do_not_need_a_hotel():
    conf = mail(5, "Your United flight confirmation", sender="United <u@united.com>", days_ago=20)
    home = run(events=[ev("Flight to Denver", 5, 9), ev("Flight to SFO", 11, 9)], messages=[conf])
    assert of(home, "travel") == []
    day_trip = run(events=[ev("Flight to LA", 11, 7), ev("Flight to SFO", 11, 19)], messages=[conf])
    assert of(day_trip, "travel") == []


def test_old_confirmation_outside_60_days_does_not_count():
    result = run(events=[ev("Train to Portland", 12, 9)],
                 messages=[mail(5, "Amtrak eTicket confirmation", sender="Amtrak <a@amtrak.com>", days_ago=61)])
    assert [g["evidence"]["leg"] for g in of(result, "travel")] == ["train"]


def test_travel_skipped_when_mail_unavailable():
    result = run(events=[ev("Flight to Denver", 11, 9)], recent_mail=down("needs Full Disk Access"))
    assert of(result, "travel") == []
    assert skipped(result)["travel"] == "mail unavailable: needs Full Disk Access"
    assert "travel bookings (mail unavailable: needs Full Disk Access)" in result["text"]


# ---- (c) mail deadline ---------------------------------------------------------

DEADLINE = mail(42, "Please submit the expense report by Thursday", days_ago=1)


def test_deadline_without_reminder_is_a_gap():
    result = run(messages=[DEADLINE], reminders=[{"title": "Call vet", "due": "2026-11-11"}])
    gaps = of(result, "deadline")
    assert len(gaps) == 1
    assert gaps[0]["line"] == ("No reminder for “Please submit the expense report by Thursday” "
                               "(from Alice, due Thursday November 12) [id 42]")
    offer = gaps[0]["offer"]
    assert offer["builtin_tool"] == "create_reminder"
    assert offer["arguments"] == {"due": "2026-11-12", "message_id": 42}
    assert offer["source"] == "lookahead" and offer["trigger"] == "lookahead_deadline"
    assert "Please submit" not in offer["event_details_redacted"]
    assert "message_id: 42" in offer["event_details_redacted"]
    assert "No reminder for" in result["text"]


def test_deadline_with_matching_reminder_or_event_is_not_a_gap():
    assert of(run(messages=[DEADLINE], reminders=[{"title": "Expense report", "due": "2026-11-11"}]), "deadline") == []
    assert of(run(messages=[DEADLINE], reminders=[{"title": "Expense report", "due": ""}]), "deadline") == []
    assert of(run(messages=[DEADLINE], events=[ev("Do expense report", 11, 15)]), "deadline") == []


def test_reminder_after_the_deadline_does_not_cover_it():
    result = run(messages=[DEADLINE], reminders=[{"title": "Expense report", "due": "2026-11-20"}])
    assert len(of(result, "deadline")) == 1


def test_deadline_outside_window_or_vetoed_is_not_a_gap():
    later = mail(1, "Please submit the expense report by Nov 30")
    past = mail(2, "Please submit the expense report by Monday", days_ago=8)  # Monday Nov 2
    sale = mail(3, "Last chance: sale ends, pay by Thursday")
    vague = mail(4, "Please review by next Friday")
    assert of(run(messages=[later, past, sale, vague]), "deadline") == []


def test_deadline_skipped_when_reminders_unavailable():
    result = run(messages=[DEADLINE], list_reminders=down("Reminders access is denied"))
    assert of(result, "deadline") == []
    assert skipped(result)["deadline"] == "reminders unavailable: Reminders access is denied"


def test_long_unavailable_reason_is_shortened():
    reason = skipped(run(list_reminders=down("x" * 500)))["deadline"]
    assert len(reason) < 200 and reason.endswith("\u2026")


def test_deadline_skipped_when_reminders_truncated_or_source_raises():
    assert "too many reminders" in skipped(run(messages=[DEADLINE], reminders=[], list_reminders=reminders_src(truncated=True)))["deadline"]
    assert "source exploded" in skipped(run(messages=[DEADLINE], list_reminders=boom))["deadline"]


def test_deadline_subject_redacted_for_cloud(monkeypatch):
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: False)
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    result = la.check_gaps(NOW, list_events=events_src(), recent_mail=mail_src(DEADLINE),
                           list_reminders=reminders_src())
    gap = of(result, "deadline")[0]
    assert "expense" not in gap["line"].lower() and "expense" not in result["text"].lower()
    assert privacy.PLACEHOLDER in gap["line"]


@pytest.mark.parametrize("subject,received,expected", [
    ("Pay invoice by Friday", date(2026, 11, 10), date(2026, 11, 13)),
    ("Sign the lease by tomorrow", date(2026, 11, 10), date(2026, 11, 11)),
    ("Respond by today", date(2026, 11, 10), date(2026, 11, 10)),
    ("RSVP by Nov 12", date(2026, 11, 10), date(2026, 11, 12)),
    ("Submit form by 11/12", date(2026, 11, 10), date(2026, 11, 12)),
    ("Approve budget by EOW", date(2026, 11, 10), date(2026, 11, 13)),
    ("Submit by Jan 3", date(2026, 12, 20), date(2027, 1, 3)),
    ("Review by next Friday", date(2026, 11, 10), None),
    ("Submit by 2/30", date(2026, 11, 10), None),
    ("Thanks for lunch", date(2026, 11, 10), None),
])
def test_deadline_date(subject, received, expected):
    assert la.deadline_date(subject, received) == expected


# ---- (d) overlaps and back-to-back ---------------------------------------------

def test_overlap_is_a_gap():
    result = run(events=[ev("1:1 Sam", 11, 10), ev("Design review", 11, 10, start_minute=30)])
    gaps = of(result, "schedule")
    assert len(gaps) == 1 and gaps[0]["evidence"]["kind"] == "overlap"
    assert "overlaps" in gaps[0]["line"]
    assert gaps[0]["offer"]["builtin_tool"] == "create_reminder"


def test_back_to_back_in_different_places_is_a_gap():
    result = run(events=[ev("Dentist", 12, 9, location="123 Main St"),
                         ev("Client lunch", 12, 10, start_minute=5, location="Cafe Rio, 9 Pine St")])
    gaps = of(result, "schedule")
    assert len(gaps) == 1 and gaps[0]["evidence"]["kind"] == "back_to_back"
    assert "5 min later" in gaps[0]["line"]


def test_no_clash_cases_are_quiet():
    events = [
        ev("A", 11, 9, location="Room 1"), ev("B", 11, 10, location="Room 1"),          # same place
        ev("C", 11, 13, location="Office"), ev("D", 11, 14, location="https://zoom.us/j/1"),  # virtual
        ev("E", 12, 9, location="Office"), ev("F", 12, 11, location="Gym"),             # 1h apart
        ev("Dup", 12, 15), ev("Dup", 12, 15),                                            # same meeting twice
        ev("Holiday", 11, 0, minutes=24 * 60, all_day=True),                            # all-day
    ]
    assert of(run(events=events), "schedule") == []


def test_schedule_skipped_when_calendar_unavailable():
    result = run(list_events=down("Calendar access isn't granted yet"))
    assert result["gaps"] == []
    reasons = skipped(result)
    assert reasons["schedule"] == "calendar unavailable: Calendar access isn't granted yet"
    assert "calendar unavailable" in reasons["travel"] and "calendar unavailable" in reasons["deadline"]


# ---- offers -----------------------------------------------------------------------

def test_offer_events_are_builtin_pattern_ideas():
    result = run(messages=[DEADLINE], events=[ev("X", 11, 10), ev("Y", 11, 10, start_minute=15)])
    events = la.offer_events(result)
    assert len(events) == len(result["gaps"]) == 2
    for event in events:
        assert isinstance(event, ContextEvent) and event.event_type == "pattern_detected"
        assert event.metadata["builtin_tool"] and event.metadata["lookahead_key"]
