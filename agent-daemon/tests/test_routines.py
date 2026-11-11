"""Tests for ai.routines (#75)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai import routines
from ai.activity_store import ActivityStore
from ai.routines import find_candidates, idea_summary
from events.event_bus import ContextEvent

def _monday(weeks_ago: int) -> datetime:
    """A Monday 09:05 UTC ``weeks_ago`` weeks back (inside the 90-day retention)."""
    today = datetime.now(timezone.utc).replace(hour=9, minute=5, second=0, microsecond=0)
    return today - timedelta(days=today.weekday(), weeks=weeks_ago)


MON = _monday(10)  # a Monday
NOW = MON + timedelta(weeks=8)
HOME = Path.home()

_pid = [1000]


def _store(tmp_path: Path) -> ActivityStore:
    return ActivityStore(tmp_path / "activity.db", enabled=True, tz=timezone.utc)


def _launch(store, app, ts):
    _pid[0] += 1
    store.record(ContextEvent("app_activated", app_name=app, timestamp=ts,
                              metadata={"pid": _pid[0]}))


def _switch(store, app, pid, ts):
    store.record(ContextEvent("app_activated", app_name=app, timestamp=ts,
                              metadata={"pid": pid}))


def _filler(store, ts):
    """Some unrelated activity so the day counts as observed."""
    store.record(ContextEvent("imessage_received", timestamp=ts))


def _mondays(store, n, apps=("Numbers", "Mail")):
    for w in range(n):
        day = MON + timedelta(weeks=w)
        for i, app in enumerate(apps):
            _launch(store, app, day + timedelta(minutes=i))


def test_three_mondays_give_one_candidate_with_evidence(tmp_path):
    store = _store(tmp_path)
    _mondays(store, 3)
    cands = find_candidates(store, now=NOW)
    assert len(cands) == 1
    c = cands[0]
    assert c["kind"] == "time_routine"
    assert c["action"]["apps"] == ["Mail", "Numbers"]
    assert c["trigger"] == {"type": "time", "weekdays": ["Monday"], "hour": 9}
    assert c["evidence"]["days"] == 3
    assert c["evidence"]["weeks"] == 3
    assert c["evidence"]["last_seen"].startswith((MON + timedelta(weeks=2)).date().isoformat())
    assert c["evidence_text"] == "seen on 3 of the last 3 Mondays"
    assert c["tool"] == "open_app"
    assert c["tool_calls"] == [{"name": "Mail"}, {"name": "Numbers"}]
    json.dumps(cands)  # plain dicts


def test_two_mondays_give_nothing(tmp_path):
    store = _store(tmp_path)
    _mondays(store, 2)
    assert find_candidates(store, now=NOW) == []


def test_rare_slot_is_not_a_routine(tmp_path):
    store = _store(tmp_path)
    _mondays(store, 3)
    # Seven more Mondays of activity without the opens: 3 of 10 is not a habit.
    for w in range(3, 10):
        _filler(store, MON + timedelta(weeks=w))
    assert find_candidates(store, now=NOW) == []


def test_weekdays_merge_into_one_candidate(tmp_path):
    store = _store(tmp_path)
    for w in range(3):
        for d in range(3):  # Mon, Tue, Wed
            _launch(store, "Slack", MON + timedelta(weeks=w, days=d))
    cands = find_candidates(store, now=NOW)
    assert len(cands) == 1
    assert cands[0]["trigger"]["weekdays"] == ["Monday", "Tuesday", "Wednesday"]
    assert cands[0]["evidence_text"] == (
        "seen on 9 of the last 9 Mondays, Tuesdays and Wednesdays")


def _pdf_day(store, day, moved=True):
    dl = HOME / "Downloads"
    store.record(ContextEvent("file", timestamp=day, metadata={
        "path": str(dl / f"statement-{day.date()}.pdf"), "action": "created"}))
    if moved:
        store.record(ContextEvent("file", timestamp=day + timedelta(minutes=3), metadata={
            "path": str(HOME / "Documents" / "Invoices" / f"statement-{day.date()}.pdf"),
            "from": str(dl / f"statement-{day.date()}.pdf"),
            "action": "renamed"}))


def test_trigger_action_on_five_days(tmp_path):
    store = _store(tmp_path)
    for d in range(5):
        _pdf_day(store, MON + timedelta(days=d, hours=5))
    cands = find_candidates(store, now=NOW)
    assert len(cands) == 1
    c = cands[0]
    assert c["kind"] == "trigger_action"
    assert c["trigger"] == {"type": "file_arrival", "ext": ".pdf", "folder": "~/Downloads"}
    assert c["action"]["to_folder"] == "~/Documents/Invoices"
    assert c["evidence"]["days"] == 5 and c["evidence"]["count"] == 5
    assert c["tool"] == "move_file"
    assert c["tool_args"] == {"dst_dir": str(HOME / "Documents" / "Invoices")}
    assert "5 separate days" in c["evidence_text"]


def test_trigger_action_on_four_days_is_not_enough(tmp_path):
    store = _store(tmp_path)
    for d in range(4):
        _pdf_day(store, MON + timedelta(days=d, hours=5))
    assert find_candidates(store, now=NOW) == []


def test_action_outside_window_does_not_pair(tmp_path):
    store = _store(tmp_path)
    for d in range(6):
        day = MON + timedelta(days=d, hours=5)
        store.record(ContextEvent("event_starting_soon", timestamp=day,
                                  metadata={"title": "Standup"}))
        _launch(store, "zoom.us", day + timedelta(minutes=30))
    assert find_candidates(store, now=NOW) == []


def test_meeting_then_app_launch(tmp_path):
    store = _store(tmp_path)
    for d in range(6):
        day = MON + timedelta(days=d, hours=5)
        store.record(ContextEvent("event_starting_soon", timestamp=day,
                                  metadata={"title": "Standup"}))
        _launch(store, "zoom.us", day + timedelta(minutes=2))
    cands = [c for c in find_candidates(store, now=NOW) if c["kind"] == "trigger_action"]
    assert len(cands) == 1
    assert cands[0]["tool"] == "open_app"
    assert cands[0]["tool_args"] == {"name": "zoom.us"}


def test_app_switch_only_history_gives_nothing(tmp_path):
    """#66: switching between two running apps all day, every day, is not a routine."""
    store = _store(tmp_path)
    _launch(store, "Xcode", MON - timedelta(days=1))
    _launch(store, "Safari", MON - timedelta(days=1))
    xpid, spid = _pid[0] - 1, _pid[0]
    for d in range(35):
        day = MON + timedelta(days=d)
        for m in range(0, 120, 5):
            _switch(store, "Xcode", xpid, day + timedelta(minutes=m))
            _switch(store, "Safari", spid, day + timedelta(minutes=m + 2))
    assert find_candidates(store, now=NOW) == []


def test_shortcut_mapping_and_fallback():
    c = {"kind": "time_routine", "title": "x",
         "action": {"type": "app_launch", "apps": ["Numbers"]}}
    assert routines.map_to_tool(c, ["Open Numbers budget"]) == {
        "tool": "run_shortcut", "args": {"name": "Open Numbers budget"}}
    other = {"kind": "trigger_action", "title": "When x, y", "action": {"type": "other"}}
    assert routines.map_to_tool(other)["tool"] == "generate_custom_tool"


def test_idea_summary_has_no_content(tmp_path):
    store = _store(tmp_path)
    for d in range(5):
        _pdf_day(store, MON + timedelta(days=d, hours=5))
    for d in range(5):
        store.record(ContextEvent("mail_received", timestamp=MON + timedelta(days=d), metadata={
            "sender": "alice@example.com", "subject": "Quarterly salary review", "bulk": False}))
    c = find_candidates(store, now=NOW)[0]
    s = idea_summary(c)
    assert "~/Downloads" in s and "~/Documents/Invoices" in s and ".pdf" in s
    assert "statement" not in s  # file name never reaches the summary
    assert "alice" not in s and "salary" not in s
    assert c["summary"] == s


def test_report_cli(tmp_path, capsys):
    store = _store(tmp_path)
    _mondays(store, 3)
    store.close()
    assert routines.main(["--report", "--db", str(tmp_path / "activity.db")]) == 0
    out = capsys.readouterr().out
    assert "Open Mail and Numbers on Mondays" in out
    assert "seen on 3 of the last 3 Mondays" in out
