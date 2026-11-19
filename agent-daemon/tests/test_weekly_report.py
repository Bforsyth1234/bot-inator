"""Tests for events.weekly_report and routes.weekly_report (todo #128)."""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import user_settings
from ai.action_journal import ActionJournal, UndoOps
from ai.idea_outcomes import MIN_SETTLED, DecisionLog, OutcomeStore
from events import weekly_report as wr
from events.weekly_report import WeeklyReportScheduler, gather_week
from routes import weekly_report as routes_wr

TZ = timezone(timedelta(hours=-7))
KEYS = (wr.ENABLED_KEY, wr.DAY_KEY, wr.TIME_KEY)


def at(day: int, hour: int = 12, minute: int = 0) -> datetime:
    """A moment in November 2026 (the 20th is a Friday)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


NOW = at(20, 16)  # Friday 16:00: the week is Nov 13 16:00 .. Nov 20 16:00


class Ts:
    """The epoch clock the stores share; tests move it before each record."""

    def __init__(self) -> None:
        self.now = NOW.timestamp()

    def __call__(self) -> float:
        return self.now

    def set(self, day: int, hour: int = 12) -> None:
        self.now = at(day, hour).timestamp()


class FakeOps(UndoOps):
    """A moved file that is still where the move left it."""

    def file_stat(self, path):
        return (1, 1)

    def path_taken(self, path):
        return False

    def dir_exists(self, path):
        return True

    def move(self, src, dst):
        return None


class FakeWatches:
    def __init__(self, rows) -> None:
        self.rows = rows

    def list(self):
        return [dict(r) for r in self.rows]


class Stores:
    def __init__(self, tmp_path: Path) -> None:
        self.ts = Ts()
        self.outcomes = OutcomeStore(None, clock=self.ts)
        self.decisions = DecisionLog(None, clock=self.ts)
        self.journal = ActionJournal(None, outcomes=self.outcomes, clock=self.ts, ops=FakeOps())
        self.watches = FakeWatches([])
        self.generated = tmp_path / "generated"
        (self.generated / ".provenance").mkdir(parents=True)

    def act(self, day: int, tool: str, **kw):
        self.ts.set(day)
        result = {"status": "ok"}
        if tool == "move_file":
            result = {"status": "ok", "old_path": "/tmp/a/x.pdf", "new_path": "/tmp/b/x.pdf"}
        entry = self.journal.record(tool, {}, result, **kw)
        self.ts.now = NOW.timestamp()
        return entry

    def offer(self, day: int, source: str, tool: str, verdict: str | None, **kw) -> str:
        self.ts.set(day)
        if verdict is None:
            offer_id = self.outcomes.record_shown(source, tool, **kw)
        else:
            offer_id = self.outcomes.record_offer(source, tool, verdict, **kw)
        self.ts.now = NOW.timestamp()
        return offer_id

    def decide(self, day: int, tool: str, decision: str, edited=()) -> None:
        self.ts.set(day)
        self.decisions.record(tool, "mail_received", decision, edited)
        self.ts.now = NOW.timestamp()

    def provenance(self, name: str, installed: datetime, repairs=()) -> None:
        record = {"version": 1, "tool_name": name, "installed_at": installed.isoformat(),
                  "repairs": [{"at": r.isoformat(), "error_class": "KeyError"} for r in repairs]}
        (self.generated / ".provenance" / f"{name}.json").write_text(json.dumps(record))

    def kwargs(self) -> dict:
        return {"journal": self.journal, "outcomes": self.outcomes, "decisions": self.decisions,
                "watches": self.watches, "generated_dir": self.generated}

    def gather(self, now: datetime = NOW, **kw) -> dict:
        return gather_week(now, **{**self.kwargs(), **kw})


@pytest.fixture
def stores(tmp_path):
    s = Stores(tmp_path)
    yield s
    s.journal.close()


@pytest.fixture
def busy(stores):
    """One week with something in every section, and older rows around it."""
    s = stores
    # Journal: five actions this week (three without a card, one of those
    # undone), two the week before.
    s.act(16, "create_reminder")
    s.act(17, "create_reminder", source="rule", rule_id="r1")
    moved = s.act(17, "move_file", source="rule", rule_id="r1")
    s.act(18, "create_note", source="always_allow")
    s.act(19, "open_url")
    s.act(10, "move_file")
    s.act(9, "create_reminder", source="rule", rule_id="r1")
    assert s.journal.undo(moved["id"])["status"] == "undone"
    # Card decisions: three this week, one before.
    s.decide(16, "create_reminder", "approved")
    s.decide(17, "create_event", "approved", ["title"])
    s.decide(18, "create_note", "denied")
    s.decide(10, "create_note", "denied")
    # Offers: six shown this week, one expired unseen, one two weeks ago.
    for day in (14, 15, 16):
        s.offer(day, "clipboard", "create_reminder", "declined", idea_kind="date_copied", one_shot=True)
    s.offer(18, "open-help", "pdf_sorter", "approved", idea_kind="pdf_download")
    s.offer(19, "friction", "rename_shots", None, idea_kind="screenshots")
    s.offer(19, "watch", "create_reminder", "approved", idea_kind="watch:w1", one_shot=True)
    s.offer(19, "pattern", "tidy_desktop", "expired", idea_kind="desktop")
    s.offer(8, "open-help", "old_tool", "approved", idea_kind="csv_open")  # never run since
    # Generated tools: one installed this week, one repaired this week.
    s.provenance("pdf_sorter", at(18))
    s.provenance("old_tool", at(8), repairs=[at(19)])
    s.watches.rows = [
        {"id": "w1", "last_fired_at": at(19).timestamp()},
        {"id": "w2", "last_fired_at": at(15).timestamp()},
        {"id": "w3", "last_fired_at": at(9).timestamp()},
        {"id": "w4", "last_fired_at": None},
    ]
    return s


BUSY_TEXT = "\n".join([
    "bot-inator, Nov 13 – Nov 20",
    "Actions run: 5 (create_reminder 2, create_note 1, move_file 1, open_url 1)",
    "Run without asking: 3 (standing rules 2, always allow 1), 1 undone",
    "Approval cards answered: 3 (2 approved (1 after an edit), 1 declined)",
    "Offers: 6 shown, 2 approved, 3 declined, 1 not answered; 1 held back and never shown",
    "Tools installed: 1 (pdf_sorter)",
    "Tools repaired: 1 (old_tool)",
    "Tools never run again: 1 (old_tool)",
    "Watches that fired: 2 (1 card)",
    "Advice: in the last 4 weeks you approved 0 of 3 clipboard offers. Turn them off?",
])


# ---- gather + format -------------------------------------------------------------

def test_fixture_stores_give_exact_counts(busy):
    p = busy.gather()
    assert p["actions"] == {
        "status": "ok", "total": 5, "truncated": False,
        "by_tool": [{"tool": "create_reminder", "count": 2}, {"tool": "create_note", "count": 1},
                    {"tool": "move_file", "count": 1}, {"tool": "open_url", "count": 1}],
        "undone": 1,
        "auto": {"rule": 2, "always_allow": 1, "total": 3, "undone": 1},
    }
    assert p["cards"] == {"status": "ok", "answered": 3, "approved": 1, "edited": 1, "denied": 1}
    assert p["offers"] == {"status": "ok", "shown": 6, "approved": 2, "declined": 3,
                           "timed_out": 0, "open": 1, "expired": 1}
    assert p["tools"] == {"status": "ok", "installed": ["pdf_sorter"], "repaired": ["old_tool"]}
    assert p["unused_tools"] == {"status": "ok", "count": 1, "names": ["old_tool"]}
    assert p["watches"] == {"status": "ok", "fired": 2, "cards": 1}
    assert p["unavailable"] == [] and p["empty"] is False
    assert (p["start"], p["end"], p["label"]) == (at(13, 16).isoformat(), NOW.isoformat(), "Nov 13 – Nov 20")


def test_fixture_stores_give_exact_text(busy):
    p = busy.gather()
    assert p["text"] == BUSY_TEXT
    assert wr.format_report(p) == BUSY_TEXT
    assert "saved" not in p["text"].lower()  # decision 2: no "time saved" number


def test_the_week_before_counts_only_its_own_rows(busy):
    p = busy.gather(weeks_ago=1)
    assert p["label"] == "Nov 6 – Nov 13" and p["weeks_ago"] == 1
    assert p["actions"]["total"] == 2
    assert p["actions"]["auto"] == {"rule": 1, "always_allow": 0, "total": 1, "undone": 0}
    assert p["cards"]["answered"] == 1 and p["cards"]["denied"] == 1
    assert p["offers"]["shown"] == 1 and p["offers"]["approved"] == 1
    assert p["tools"] == {"status": "ok", "installed": ["old_tool"], "repaired": []}
    assert p["watches"] == {"status": "ok", "fired": 1, "cards": 0}
    assert "Actions run: 2 (create_reminder 1, move_file 1)" in p["text"]
    assert "Run without asking: 1 (standing rules 1), 0 undone" in p["text"]
    # weeks_ago is kept inside what the journal still holds (30 days).
    assert busy.gather(weeks_ago=99)["weeks_ago"] == wr.MAX_WEEKS_AGO


def test_week_edges_are_start_exclusive_end_inclusive(stores):
    stores.ts.now = at(13, 16).timestamp()          # exactly the start: last week's
    stores.journal.record("open_url", {}, {"status": "ok"})
    stores.ts.now = NOW.timestamp()                 # exactly the end: this week's
    stores.journal.record("open_app", {}, {"status": "ok"})
    assert stores.gather()["actions"]["by_tool"] == [{"tool": "open_app", "count": 1}]
    assert stores.gather(weeks_ago=1)["actions"]["by_tool"] == [{"tool": "open_url", "count": 1}]


def test_an_undone_rule_action_is_counted_as_undone(stores):
    entry = stores.act(17, "move_file", source="rule", rule_id="r1")
    stores.act(18, "move_file")  # approved on a card, then undone: not an automatic run
    before = stores.gather()
    assert before["actions"]["auto"] == {"rule": 1, "always_allow": 0, "total": 1, "undone": 0}
    assert "Run without asking: 1 (standing rules 1), 0 undone" in before["text"]
    assert stores.journal.undo(entry["id"])["status"] == "undone"
    carded = stores.journal.rows()[0]
    assert carded["source"] == "approval" and stores.journal.undo(carded["id"])["status"] == "undone"
    after = stores.gather()
    assert after["actions"]["auto"]["undone"] == 1 and after["actions"]["undone"] == 2
    assert "Run without asking: 1 (standing rules 1), 1 undone" in after["text"]


def test_always_allow_runs_count_as_run_without_asking(stores):
    stores.act(18, "create_note", source="always_allow")
    stores.act(18, "create_note")
    auto = stores.gather()["actions"]["auto"]
    assert auto == {"rule": 0, "always_allow": 1, "total": 1, "undone": 0}


def test_no_automatic_runs_says_zero_without_an_undone_count(stores):
    stores.act(18, "open_url")
    assert "Run without asking: 0\n" in stores.gather()["text"]


def test_journal_cap_is_reported_as_at_least():
    class CappedJournal:
        def rows(self, limit=200):
            assert limit == wr.JOURNAL_LIMIT
            return [{"tool": "open_url", "created_at": at(19).timestamp(), "source": "approval",
                     "undone_at": None}] * limit

    p = gather_week(NOW, journal=CappedJournal())
    assert p["actions"]["truncated"] is True and p["actions"]["total"] == wr.JOURNAL_LIMIT
    assert f"Actions run: at least {wr.JOURNAL_LIMIT} (open_url {wr.JOURNAL_LIMIT})" in p["text"]


def test_missing_and_failing_sources_are_named_not_dropped(busy):
    class BrokenDecisions:
        @property
        def decisions(self):
            raise RuntimeError("file is locked")

    p = busy.gather(journal=None, decisions=BrokenDecisions(), generated_dir=None)
    assert p["unavailable"] == ["Actions run", "Approval cards answered", "Tools installed or repaired"]
    assert p["actions"] == {"status": "unavailable", "reason": "not connected yet"}
    lines = p["text"].splitlines()
    assert "Actions run: unavailable: not connected yet" in lines
    assert "Approval cards answered: unavailable: file is locked" in lines
    assert "Tools installed or repaired: unavailable: not connected yet" in lines
    assert not any(line.startswith("Run without asking") for line in lines)  # no guessed number
    assert "Offers: 6 shown, 2 approved, 3 declined, 1 not answered; 1 held back and never shown" in lines
    missing_dir = busy.gather(generated_dir=busy.generated / "nope")
    assert missing_dir["tools"]["status"] == "unavailable"
    assert "no generated tools folder" in missing_dir["tools"]["reason"]


def test_nothing_connected_is_all_unavailable_and_empty():
    p = gather_week(NOW)
    assert p["unavailable"] == [label for _, label in wr.SECTIONS]
    assert p["empty"] is True and p["advice"] is None
    assert p["text"] == (
        "Nothing to report for Nov 13 – Nov 20: no actions ran and no offers were shown. "
        "Unavailable: Actions run, Approval cards answered, Offers, Tools installed or repaired, "
        "Tools never run again, Watches that fired."
    )


def test_gather_changes_no_store(busy):
    before = (json.dumps(busy.outcomes.offers, sort_keys=True), json.dumps(busy.decisions.decisions, sort_keys=True),
              busy.journal.rows(), sorted(p.name for p in busy.generated.rglob("*")))
    busy.gather()
    busy.gather(weeks_ago=2)
    after = (json.dumps(busy.outcomes.offers, sort_keys=True), json.dumps(busy.decisions.decisions, sort_keys=True),
             busy.journal.rows(), sorted(p.name for p in busy.generated.rglob("*")))
    assert before == after


def test_module_never_asks_notifies_or_calls_a_model():
    source = Path(wr.__file__).read_text()
    for gated in ("show_notification(", "approval_request", "evaluate(", "engine", "subprocess",
                  "osascript", "requests", "urllib", "httpx", ".record(", ".record_", ".undo("):
        assert gated not in source, gated


# ---- advice ------------------------------------------------------------------------

def _decline(stores, count: int, source: str = "clipboard", day: int = 16) -> None:
    for _ in range(count):
        stores.offer(day, source, "create_reminder", "declined", one_shot=True)


def test_advice_appears_only_past_min_settled(stores):
    _decline(stores, MIN_SETTLED - 1)
    below = stores.gather()
    assert below["advice"] is None and "Advice" not in below["text"]
    _decline(stores, 1)
    p = stores.gather()
    assert p["advice"] == {"source": "clipboard", "approved": 0, "settled": MIN_SETTLED,
                           "approval_rate": 0.0, "weeks": 4, "setting": ""}
    assert p["text"].splitlines()[-1] == (
        f"Advice: in the last 4 weeks you approved 0 of {MIN_SETTLED} clipboard offers. Turn them off?"
    )
    assert p["text"].count("Advice:") == 1


def test_advice_picks_the_lowest_rate_and_only_one_line(stores):
    _decline(stores, 3, "clipboard")
    stores.offer(16, "clipboard", "create_reminder", "approved", one_shot=True)   # 1 of 4
    _decline(stores, 4, "open-help")                                            # 0 of 4
    stores.offer(16, "friction", "x", "timed_out")
    p = stores.gather()
    assert (p["advice"]["source"], p["advice"]["approved"], p["advice"]["settled"]) == ("open_help", 0, 4)
    assert p["advice"]["setting"] == "AGENT_OPEN_HELP"
    assert p["text"].splitlines()[-1].endswith("0 of 4 open help offers. Turn them off? (AGENT_OPEN_HELP)")
    assert p["text"].count("Advice:") == 1


def test_no_advice_when_half_or_more_were_approved(stores):
    _decline(stores, 2)
    for _ in range(2):
        stores.offer(16, "clipboard", "create_reminder", "approved", one_shot=True)
    assert stores.gather()["advice"] is None  # 2 of 4


def test_no_advice_for_chat_or_for_verdicts_older_than_four_weeks(stores):
    _decline(stores, 5, "chat")
    assert stores.gather()["advice"] is None
    stores.ts.now = (NOW - timedelta(days=29)).timestamp()
    for _ in range(5):
        stores.outcomes.record_offer("clipboard", "create_reminder", "declined", one_shot=True)
    stores.ts.now = NOW.timestamp()
    assert stores.gather()["advice"] is None
    # ... but the same verdicts count for a report whose window still sees them.
    assert gather_week(NOW - timedelta(days=7), outcomes=stores.outcomes)["advice"]["source"] == "clipboard"


def test_open_offers_are_not_verdicts(stores):
    for _ in range(5):
        stores.offer(16, "clipboard", "create_reminder", None, one_shot=True)
    assert stores.gather()["advice"] is None


# ---- empty week ----------------------------------------------------------------------

def test_empty_week_says_so_in_one_line(stores):
    stores.act(10, "open_url")  # last week's action doesn't make this week busy
    p = stores.gather()
    assert p["empty"] is True and p["unavailable"] == []
    assert p["text"] == "Nothing to report for Nov 13 – Nov 20: no actions ran and no offers were shown."
    assert "\n" not in p["text"]
    assert stores.gather(weeks_ago=1)["empty"] is False


def test_one_action_makes_the_week_not_empty(stores):
    stores.act(18, "open_url")
    assert stores.gather()["empty"] is False


# ---- scheduler -----------------------------------------------------------------------

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


class Gather:
    def __init__(self, empty: bool = False) -> None:
        self.calls: list[datetime] = []
        self.empty = empty

    def __call__(self, now: datetime) -> dict:
        self.calls.append(now)
        return {"label": "Nov 13 – Nov 20", "empty": self.empty, "text": "report"}


def make_scheduler(tmp_path, clock, bus=None, *, enabled=True, weekday=4, when=dtime(16, 0), gather=None):
    bus = bus or FakeBus()
    scheduler = WeeklyReportScheduler(
        bus,
        state_path=tmp_path / "state" / "weekly_report.json",
        clock=clock,
        gather=gather or Gather(),
        enabled=lambda: enabled,
        weekday=(weekday if callable(weekday) else (lambda: weekday)),
        time_of_day=(when if callable(when) else (lambda: when)),
    )
    return scheduler, bus


def test_fires_once_on_the_set_weekday(tmp_path):
    clock = Clock(at(20, 15, 59))  # Friday, a minute early
    scheduler, bus = make_scheduler(tmp_path, clock)
    assert scheduler.check_once() is None
    clock.now = at(20, 16)
    event = scheduler.check_once()
    assert event is not None and event.event_type == "weekly_report"
    assert event.metadata["trigger"] == "schedule" and event.metadata["text"] == "report"
    clock.now = at(20, 16, 1)
    assert scheduler.check_once() is None
    clock.now = at(20, 23, 59)
    assert scheduler.check_once() is None
    assert len(bus.events) == 1
    clock.now = at(27, 16)  # the next Friday
    assert scheduler.check_once() is not None
    assert len(bus.events) == 2


def test_does_not_fire_on_other_days(tmp_path):
    clock = Clock(at(16, 16))
    scheduler, bus = make_scheduler(tmp_path, clock)
    for day in (16, 17, 18, 19, 21, 22):  # Mon..Thu, Sat, Sun
        for hour in (0, 16, 23):
            clock.now = at(day, hour)
            assert not scheduler.is_due() and scheduler.check_once() is None
    assert bus.events == []
    assert not (tmp_path / "state" / "weekly_report.json").exists()


def test_does_not_fire_again_after_a_restart(tmp_path):
    clock = Clock(at(20, 16))
    scheduler, bus = make_scheduler(tmp_path, clock)
    assert scheduler.check_once() is not None
    saved = json.loads((tmp_path / "state" / "weekly_report.json").read_text())
    assert saved == {"last_run": at(20, 16).isoformat(), "sent": True}
    clock.now = at(20, 17)
    restarted, bus2 = make_scheduler(tmp_path, clock)
    assert restarted.is_due() is False and restarted.check_once() is None
    assert bus2.events == []


def test_missed_time_catches_up_the_same_day_only(tmp_path):
    clock = Clock(at(20, 21, 30))  # asleep at 16:00, awake that evening
    scheduler, bus = make_scheduler(tmp_path, clock)
    assert scheduler.check_once() is not None
    other = tmp_path / "other"
    clock.now = at(21, 9)  # asleep all Friday: Saturday does not replay it
    late, late_bus = make_scheduler(other, clock)
    assert late.check_once() is None and late_bus.events == []


def test_the_weekday_setting_is_followed(tmp_path):
    day = {"value": 0}
    clock = Clock(at(20, 16))
    scheduler, bus = make_scheduler(tmp_path, clock, weekday=lambda: day["value"])
    assert scheduler.check_once() is None  # Friday, but set to Monday
    clock.now = at(23, 16)  # Monday
    assert scheduler.check_once() is not None


def test_disabled_means_no_event_and_no_state(tmp_path):
    scheduler, bus = make_scheduler(tmp_path, Clock(at(20, 17)), enabled=False)
    assert scheduler.check_once() is None and bus.events == []
    assert not (tmp_path / "state" / "weekly_report.json").exists()


def test_an_empty_week_sends_nothing(tmp_path):
    gather = Gather(empty=True)
    clock = Clock(at(20, 16))
    scheduler, bus = make_scheduler(tmp_path, clock, gather=gather)
    assert scheduler.check_once() is None
    assert bus.events == []
    saved = json.loads((tmp_path / "state" / "weekly_report.json").read_text())
    assert saved == {"last_run": at(20, 16).isoformat(), "sent": False}
    clock.now = at(20, 16, 1)  # counted as the run: not gathered again all day
    assert scheduler.check_once() is None and len(gather.calls) == 1
    payload, event = scheduler.run_now()
    assert event is None and payload["empty"] is True and bus.events == []


def test_an_empty_week_from_real_stores_sends_nothing(tmp_path, stores):
    scheduler, bus = make_scheduler(tmp_path, Clock(NOW), gather=lambda now: stores.gather(now))
    assert scheduler.check_once() is None and bus.events == []
    stores.act(21, "open_url")
    busy_scheduler, busy_bus = make_scheduler(tmp_path / "b", Clock(at(27, 16)),
                                              gather=lambda now: stores.gather(now))
    event = busy_scheduler.check_once()
    assert event is not None and event.metadata["actions"]["total"] == 1
    assert [e.event_type for e in busy_bus.events] == ["weekly_report"]


def test_failed_push_is_retried_not_lost(tmp_path):
    clock = Clock(at(20, 16))
    scheduler, bus = make_scheduler(tmp_path, clock)
    bus.fail = True
    with pytest.raises(RuntimeError):
        scheduler.check_once()
    assert not (tmp_path / "state" / "weekly_report.json").exists()
    bus.fail = False
    assert scheduler.check_once() is not None


def test_corrupt_state_file_is_treated_as_never_run(tmp_path):
    path = tmp_path / "state" / "weekly_report.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    scheduler, _ = make_scheduler(tmp_path, Clock(at(20, 16)))
    assert scheduler.check_once() is not None


def test_run_now_does_not_use_up_this_weeks_report(tmp_path):
    clock = Clock(at(18, 9))  # Wednesday
    scheduler, bus = make_scheduler(tmp_path, clock)
    payload, event = scheduler.run_now()
    assert event is not None and event.metadata["trigger"] == "manual" and payload["text"] == "report"
    clock.now = at(20, 16)
    assert scheduler.check_once() is not None
    assert [e.metadata["trigger"] for e in bus.events] == ["manual", "schedule"]


def test_default_gather_reads_the_configured_stores(tmp_path, busy, configured):
    scheduler = WeeklyReportScheduler(FakeBus(), state_path=tmp_path / "s.json", clock=Clock(NOW),
                                      enabled=lambda: True, weekday=lambda: 4, time_of_day=lambda: dtime(16, 0))
    event = scheduler.check_once()
    assert event is not None and event.metadata["text"] == BUSY_TEXT


# ---- settings ------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A settings file of our own, with the three keys allowed (the runner
    adds them to ``user_settings.ALLOWED_KEYS`` at wave close)."""
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    monkeypatch.setattr(user_settings, "ALLOWED_KEYS", user_settings.ALLOWED_KEYS | set(KEYS))
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "settings.json"


def test_defaults_are_on_friday_at_four(settings):
    assert wr.report_enabled() is True
    assert (wr.report_weekday(), wr.report_weekday_text()) == (4, "fri")
    assert (wr.report_time(), wr.report_time_text()) == (dtime(16, 0), "16:00")


def test_settings_file_drives_the_scheduler_live(settings, tmp_path):
    clock = Clock(at(18, 9, 30))  # Wednesday 09:30
    bus = FakeBus()
    scheduler = WeeklyReportScheduler(bus, state_path=tmp_path / "s.json", clock=clock, gather=Gather())
    assert scheduler.check_once() is None  # default Friday 16:00
    user_settings.write({wr.DAY_KEY: "wed", wr.TIME_KEY: "09:00"})
    assert scheduler.is_due() is True
    user_settings.write({wr.ENABLED_KEY: "0"})
    assert scheduler.check_once() is None
    user_settings.write({wr.ENABLED_KEY: "1"})
    assert scheduler.check_once() is not None and len(bus.events) == 1


def test_environment_is_the_fallback_and_bad_values_fall_back(settings, monkeypatch):
    monkeypatch.setenv(wr.DAY_KEY, "Tuesday")
    monkeypatch.setenv(wr.TIME_KEY, "7:05")
    monkeypatch.setenv(wr.ENABLED_KEY, "0")
    assert (wr.report_weekday(), wr.report_time(), wr.report_enabled()) == (1, dtime(7, 5), False)
    monkeypatch.setenv(wr.DAY_KEY, "someday")
    monkeypatch.setenv(wr.TIME_KEY, "25:00")
    assert (wr.report_weekday(), wr.report_time()) == (4, dtime(16, 0))


def test_parse_weekday_and_time():
    assert [wr.parse_weekday(d) for d in ("mon", "FRI", " sunday ", "Wed")] == [0, 4, 6, 2]
    for bad in ("", "frid", "fr", "5", None, "funday"):
        assert wr.parse_weekday(bad) is None
    assert wr.parse_time("16:00") == dtime(16, 0)
    for bad in ("", "16", "24:00", "16:60", "ab:cd", None):
        assert wr.parse_time(bad) is None


# ---- tool, describe, notification ------------------------------------------------------

@pytest.fixture
def configured(busy, monkeypatch):
    monkeypatch.setattr(wr, "_SOURCES", dict(wr._SOURCES))
    monkeypatch.setattr(wr, "_now", lambda: NOW)
    wr.configure(**busy.kwargs())
    return busy


def test_tool_answers_from_the_configured_stores(configured):
    assert wr.weekly_report(weeks_ago=0) == {
        "status": "ok", "report": BUSY_TEXT, "week": "Nov 13 – Nov 20", "empty": False,
    }
    last = wr.weekly_report(weeks_ago=1)
    assert last["week"] == "Nov 6 – Nov 13" and "Actions run: 2" in last["report"]


def test_tool_before_configure_names_every_source_unavailable(monkeypatch):
    monkeypatch.setattr(wr, "_SOURCES", {k: None for k in wr._SOURCES})
    monkeypatch.setattr(wr, "_now", lambda: NOW)
    answer = wr.weekly_report()
    assert answer["empty"] is True and "Unavailable: Actions run" in answer["report"]


def test_describe_and_notification_text(busy):
    event = wr.make_event(busy.gather())
    assert event.event_type == "weekly_report" and event.metadata["trigger"] == "schedule"
    assert wr.describe(event) == (
        "Weekly report for Nov 13 – Nov 20: 5 actions (3 without asking), 6 offers shown, 3 cards answered"
    )
    title, body = wr.notification_text(event)
    assert title == "Your week with bot-inator (Nov 13 – Nov 20)"
    assert body == "\n".join(BUSY_TEXT.splitlines()[1:])[:240] and len(body) == 240
    assert body.startswith("Actions run: 5 (create_reminder 2")
    missing = wr.make_event(gather_week(NOW, outcomes=busy.outcomes))
    assert wr.describe(missing).startswith("Weekly report for Nov 13 – Nov 20: actions unavailable, 6 offers shown")


# ---- routes ----------------------------------------------------------------------------

@pytest.fixture
def client(settings, configured):
    app = FastAPI()
    app.include_router(routes_wr.router)
    with TestClient(app) as test_client:
        test_client.settings_path = settings
        yield test_client


def test_route_reads_and_saves_settings(client):
    assert client.get("/api/weekly-report").json() == {
        "enabled": True, "weekday": "fri", "time": "16:00", "locked_fields": [],
    }
    body = client.put("/api/weekly-report", json={"enabled": False, "weekday": "Monday", "time": "9:05"}).json()
    assert body == {"enabled": False, "weekday": "mon", "time": "09:05", "locked_fields": []}
    assert json.loads(client.settings_path.read_text()) == {
        "AGENT_WEEKLY_REPORT": "0", "AGENT_WEEKLY_REPORT_DAY": "mon", "AGENT_WEEKLY_REPORT_TIME": "09:05",
    }
    assert (wr.report_enabled(), wr.report_weekday(), wr.report_time()) == (False, 0, dtime(9, 5))
    # Omitted fields are left unchanged.
    assert client.put("/api/weekly-report", json={"enabled": True}).json() == {
        "enabled": True, "weekday": "mon", "time": "09:05", "locked_fields": [],
    }
    assert client.put("/api/weekly-report", json={"time": "25:00"}).status_code == 400
    assert client.put("/api/weekly-report", json={"weekday": "someday"}).status_code == 400
    assert client.get("/api/weekly-report").json()["weekday"] == "mon"


def test_route_reports_shell_locked_fields(client, monkeypatch):
    monkeypatch.setattr(user_settings, "shell_exported", frozenset({wr.DAY_KEY}))
    assert client.get("/api/weekly-report").json()["locked_fields"] == ["weekday"]


def test_route_week_and_last_four_weeks(client):
    week = client.get("/api/weekly-report/week").json()
    assert week["text"] == BUSY_TEXT and week["actions"]["auto"]["undone"] == 1
    assert client.get("/api/weekly-report/week", params={"weeks_ago": 1}).json()["label"] == "Nov 6 – Nov 13"
    assert client.get("/api/weekly-report/week", params={"weeks_ago": 4}).status_code == 422
    weeks = client.get("/api/weekly-report/weeks").json()["weeks"]
    assert [w["label"] for w in weeks] == ["Nov 13 – Nov 20", "Nov 6 – Nov 13", "Oct 30 – Nov 6", "Oct 23 – Oct 30"]
    assert [w["weeks_ago"] for w in weeks] == [0, 1, 2, 3]
    assert [w["empty"] for w in weeks] == [False, False, True, True]
    assert weeks[0]["text"] == BUSY_TEXT


def test_route_run_publishes_a_manual_report(client, tmp_path):
    assert client.post("/api/weekly-report/run").status_code == 503  # nothing wired
    bus = FakeBus()
    client.app.state.event_bus = bus  # no scheduler: the bus alone is enough
    body = client.post("/api/weekly-report/run").json()
    assert body["status"] == "ok" and body["sent"] is True and body["report"]["text"] == BUSY_TEXT
    scheduler, scheduler_bus = make_scheduler(tmp_path, Clock(NOW), gather=lambda now: wr.report_for(0, now))
    client.app.state.weekly_report = scheduler
    assert client.post("/api/weekly-report/run").json()["sent"] is True
    assert [e.metadata["trigger"] for e in bus.events + scheduler_bus.events] == ["manual", "manual"]
    assert scheduler_bus.events[0].metadata["text"] == BUSY_TEXT
    assert not (tmp_path / "state" / "weekly_report.json").exists()  # not this week's run


def test_route_run_on_an_empty_week_sends_nothing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(wr, "_now", lambda: NOW - timedelta(days=21))  # before any fixture row
    bus = FakeBus()
    client.app.state.event_bus = bus
    body = client.post("/api/weekly-report/run").json()
    assert body["sent"] is False and body["report"]["empty"] is True and bus.events == []
    scheduler, scheduler_bus = make_scheduler(
        tmp_path, Clock(NOW - timedelta(days=21)), gather=lambda now: wr.report_for(0, now))
    client.app.state.weekly_report = scheduler
    assert client.post("/api/weekly-report/run").json()["sent"] is False
    assert scheduler_bus.events == []
