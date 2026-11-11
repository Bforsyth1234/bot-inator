"""#88 "Watch for…": spec allow-lists, deterministic matching, backtest,
limits, the card path, and the user-only create route."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import prepared_offers as po
from ai import privacy
from ai import watches as w
from ai.idea_outcomes import DecisionLog
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent
from routes import watches as watch_routes
from tests.test_wave13_wiring import _answer_soon, _approval_orch

TZ = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)  # a Tuesday
HOME = str(Path.home())
DL = "~/Downloads"

PDF_SPEC = {
    "trigger": {"kind": "file", "folder": DL, "extension": "pdf"},
    "action": {"tool": "move_file", "arguments": {"src": "{path}", "dst_dir": "~/Documents/Receipts"}},
}
MAIL_SPEC = {
    "trigger": {"kind": "mail_received", "sender": "acme-cpa.com", "subject_contains": "invoice"},
    "action": {"tool": "draft_email", "arguments": {"to": "{sender}", "subject": "Re: {subject}",
                                                    "body": "Thanks, got it."}},
}
EVENT_SPEC = {
    "trigger": {"kind": "event_starting_soon", "title_contains": "standup"},
    "action": {"tool": "create_note", "arguments": {"title": "{title} {date}"}},
}
TIME_SPEC = {
    "trigger": {"kind": "time", "weekdays": ["mon", "tue"], "at": "08:30"},
    "action": {"tool": "open_url", "arguments": {"url": "https://example.com/status"}},
}


def _file(path: str, action: str = "created") -> ContextEvent:
    return ContextEvent(event_type="file", metadata={"path": path, "action": action})


def _mail(sender: str, subject: str, **extra: Any) -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": sender, "subject": subject, "mailbox": "INBOX", "message_id": 7,
        "date": "2026-11-10T08:55:00-08:00", **extra})


# ---- spec allow-lists --------------------------------------------------------


@pytest.mark.parametrize("spec", [PDF_SPEC, MAIL_SPEC, EVENT_SPEC, TIME_SPEC])
def test_valid_specs_normalize(spec: dict) -> None:
    clean = w.validate_spec(spec)
    assert clean["action"]["tool"] == spec["action"]["tool"]
    assert w.validate_spec(clean) == clean  # idempotent
    assert "nothing runs on its own" in w.describe(clean)


def _with(spec: dict, **action: Any) -> dict:
    return {"trigger": spec["trigger"], "action": {**spec["action"], **action}}


@pytest.mark.parametrize("spec, needle", [
    (_with(PDF_SPEC, tool="generate_custom_tool", arguments={"tool_name": "x"}), "not allowed"),
    (_with(PDF_SPEC, tool="send_imessage", arguments={"to": "x", "text": "y"}), "not allowed"),
    (_with(PDF_SPEC, tool="mcp__github__create_issue", arguments={}), "not allowed"),
    (_with(PDF_SPEC, tool="my_generated_tool", arguments={}), "not allowed"),
    # placeholder not from the trigger
    (_with(PDF_SPEC, arguments={"src": "{path}", "dst_dir": "~/x", "new_name": "{subject}.pdf"}),
     "not a field of a file trigger"),
    (_with(MAIL_SPEC, arguments={"to": "{path}"}), "not a field"),
    # a URL never holds a placeholder
    (_with(MAIL_SPEC, tool="open_url", arguments={"url": "https://x.com/?q={subject}"}), "may not contain a placeholder"),
    (_with(MAIL_SPEC, tool="create_note", arguments={"title": "see https://x/{subject}"}), "may not contain a placeholder"),
    # literal-only arguments
    (_with(PDF_SPEC, arguments={"src": "{path}", "dst_dir": "~/x/{date}"}), "may not contain a placeholder"),
    (_with(MAIL_SPEC, tool="run_shortcut", arguments={"name": "{subject}"}), "may not contain a placeholder"),
    (_with(PDF_SPEC, arguments={"src": "~/secret.txt", "dst_dir": "~/x"}), "src must be exactly {path}"),
    (_with(PDF_SPEC, arguments={"src": "{path}", "dst_dir": "~/../../etc"}), "absolute folder"),
    (_with(PDF_SPEC, arguments={"src": "{path}", "dst_dir": "~/x", "mode": "force"}), "has no argument"),
    (_with(MAIL_SPEC, arguments={"to": "{sender}", "subject": "{nope"}), "stray brace"),
    (_with(TIME_SPEC, arguments={"url": "file:///etc/passwd"}), "http(s)"),
    ({"trigger": {"kind": "clipboard"}, "action": PDF_SPEC["action"]}, "unknown trigger kind"),
    ({"trigger": {**PDF_SPEC["trigger"], "regex": ".*"}, "action": PDF_SPEC["action"]}, "does not take"),
    ({"trigger": {"kind": "mail_received"}, "action": MAIL_SPEC["action"]}, "needs a sender"),
    ({"trigger": {"kind": "time", "weekdays": [9], "at": "25:00"}, "action": TIME_SPEC["action"]}, "weekdays"),
    ({**PDF_SPEC, "run_now": True}, "does not take"),
])
def test_spec_outside_allow_lists_is_rejected(spec: dict, needle: str) -> None:
    with pytest.raises(w.SpecError) as err:
        w.validate_spec(spec)
    assert any(needle in p for p in err.value.problems), err.value.problems


# ---- matching ----------------------------------------------------------------


def test_file_trigger_matches() -> None:
    spec = w.validate_spec(PDF_SPEC)
    assert w.matches(spec, _file(f"{HOME}/Downloads/receipt.PDF"), NOW)
    assert w.matches(spec, _file(f"{HOME}/Downloads/receipt.pdf", "renamed"), NOW)
    assert not w.matches(spec, _file(f"{HOME}/Downloads/receipt.pdf", "modified"), NOW)
    assert not w.matches(spec, _file(f"{HOME}/Downloads/receipt.png"), NOW)
    assert not w.matches(spec, _file(f"{HOME}/Downloads/sub/receipt.pdf"), NOW)
    assert not w.matches(spec, _file(f"{HOME}/Downloads/receipt.pdf.crdownload"), NOW)
    assert not w.matches(spec, _file(f"{HOME}/Downloads/.hidden.pdf"), NOW)
    named = w.validate_spec({**PDF_SPEC, "trigger": {**PDF_SPEC["trigger"], "name_contains": "Invoice"}})
    assert w.matches(named, _file(f"{HOME}/Downloads/acme invoice 3.pdf"), NOW)
    assert not w.matches(named, _file(f"{HOME}/Downloads/receipt.pdf"), NOW)


def test_mail_trigger_matches() -> None:
    spec = w.validate_spec(MAIL_SPEC)
    assert w.matches(spec, _mail("Pat <pat@acme-cpa.com>", "Your Invoice for Q3"), NOW)
    assert w.matches(spec, _mail("pat@mail.acme-cpa.com", "invoice"), NOW)
    assert not w.matches(spec, _mail("pat@notacme-cpa.com", "invoice"), NOW)
    assert not w.matches(spec, _mail("pat@acme-cpa.com", "hello"), NOW)
    assert not w.matches(spec, _mail("pat@acme-cpa.com", "invoice", bulk=True), NOW)
    assert not w.matches(spec, _mail("pat@acme-cpa.com", "invoice", mailbox="Junk"), NOW)
    exact = w.validate_spec({**MAIL_SPEC, "trigger": {"kind": "mail_received", "sender": "Pat@Acme-CPA.com"}})
    assert w.matches(exact, _mail("pat@acme-cpa.com", "anything"), NOW)
    assert not w.matches(exact, _mail("sam@acme-cpa.com", "anything"), NOW)


def test_event_and_time_triggers_match() -> None:
    spec = w.validate_spec(EVENT_SPEC)
    soon = ContextEvent(event_type="event_starting_soon", metadata={"title": "Team Standup", "start": "2026-11-10T09:10:00-08:00"})
    assert w.event_fields(spec["trigger"], soon, NOW) == {"title": "Team Standup", "date": "2026-11-10"}
    assert not w.matches(spec, ContextEvent(event_type="event_starting_soon", metadata={"title": "1:1"}), NOW)
    assert not w.matches(spec, ContextEvent(event_type="file", metadata={"title": "standup"}), NOW)
    timed = w.validate_spec(TIME_SPEC)
    tick = ContextEvent(event_type=w.TIME_EVENT_TYPE, metadata={"at": "08:30", "weekday": 1, "date": "2026-11-10"})
    assert w.matches(timed, tick, NOW)
    assert not w.matches(timed, ContextEvent(event_type=w.TIME_EVENT_TYPE, metadata={"at": "08:30", "weekday": 4}), NOW)


def test_text_inside_content_cannot_trigger() -> None:
    """Only structured fields are read: a body or file text naming the watch does nothing."""
    spec = w.validate_spec(MAIL_SPEC)
    evil = _mail("eve@evil.com", "hi", body="invoice from acme-cpa.com", text="acme-cpa.com invoice")
    assert not w.matches(spec, evil, NOW)


# ---- the idea / card ---------------------------------------------------------


def _watch(spec: dict, wid: str = "abc123") -> dict:
    return {"id": wid, "spec": w.validate_spec(spec)}


def test_move_file_idea_is_a_filled_prepared_offer() -> None:
    watch = _watch(PDF_SPEC)
    path = f"{HOME}/Downloads/receipt.pdf"
    fields = w.event_fields(watch["spec"]["trigger"], _file(path), NOW)
    idea = w.to_idea(watch, _file(path), fields)
    assert idea["source"] == "watch" and idea["trigger"] == "watch:abc123"
    assert idea["builtin_tool"] == "move_file"
    offer = po.PreparedOffer.from_dict(idea[po.PREPARED_KEY])
    card = offer.to_card()
    assert card["tool_name"] == "move_file"
    assert card["tool_args"]["kwargs"] == {"src": path, "dst_dir": f"{HOME}/Documents/Receipts"}


def test_mail_idea_is_a_builtin_follow_up_with_redacted_copy(monkeypatch) -> None:
    watch = _watch(MAIL_SPEC)
    event = _mail("Pat <pat@acme-cpa.com>", "Invoice 42 overdue")
    idea = w.to_idea(watch, event, w.event_fields(watch["spec"]["trigger"], event, NOW))
    assert po.PREPARED_KEY not in idea  # draft_email is not a bundle tool
    assert idea["arguments"] == {"to": "pat@acme-cpa.com", "subject": "Re: Invoice 42 overdue", "body": "Thanks, got it."}
    assert "Invoice 42 overdue" in idea["event_details"]
    assert "Invoice 42" not in idea["event_details_redacted"]
    assert "pat@acme-cpa.com" not in idea["event_details_redacted"]
    monkeypatch.setattr(privacy, "should_redact", lambda role: role == "main")
    prompt = Orchestrator._build_pattern_prompt(ContextEvent(event_type="pattern_detected", metadata=idea))
    assert "`draft_email`" in prompt and "Do NOT call `generate_custom_tool`" in prompt
    assert "Invoice 42" not in prompt


def _orch(ran: list) -> Orchestrator:
    orch = _approval_orch()
    orch.memory = None
    orch.pattern_recognizer = None
    orch._turn_tool_calls = set()
    orch.tools = [SimpleNamespace(name="move_file", forward=lambda **k: ran.append(k) or {"status": "ok"})]

    async def run_agent(*_a: Any, **_k: Any) -> str:
        raise AssertionError("a watch card never runs the agent")

    orch._run_agent = run_agent  # type: ignore[method-assign]
    return orch


def _drive(approved: bool, idea: dict, orch: Orchestrator) -> None:
    async def scenario() -> None:
        _answer_soon(orch, approved)
        await orch._handle_event(ContextEvent(event_type="pattern_detected", metadata=idea))

    asyncio.run(scenario())


def _idea_for(path: str, wid: str = "abc123") -> dict:
    watch = _watch(PDF_SPEC, wid)
    return w.to_idea(watch, _file(path), w.event_fields(watch["spec"]["trigger"], _file(path), NOW))


def test_watch_idea_reaches_a_normal_card_and_runs_only_after_approval() -> None:
    from schemas.ws_messages import ApprovalRequest

    ran: list = []
    orch = _orch(ran)
    path = f"{HOME}/Downloads/r.pdf"
    _drive(True, _idea_for(path), orch)
    [card] = [m for m in orch.sent if isinstance(m, ApprovalRequest)]
    assert card.payload.tool_name == "move_file"
    assert ran == [{"src": path, "dst_dir": f"{HOME}/Documents/Receipts"}]
    [row] = orch.approval_decisions.decisions
    assert (row["tool_name"], row["trigger_kind"], row["decision"]) == ("move_file", "watch:abc123", "approved")
    assert orch.approval_decisions.clean_approvals("move_file", "watch:abc123") == 1


def test_denied_watch_card_runs_nothing() -> None:
    ran: list = []
    orch = _orch(ran)
    _drive(False, _idea_for(f"{HOME}/Downloads/r.pdf"), orch)
    assert ran == []
    assert orch.approval_decisions.decisions[0]["decision"] == "denied"


def _forbid_actions(monkeypatch) -> None:
    """Every gated tool a watch may offer fails the test if it runs."""
    import tools

    hit = [t for t in tools.AVAILABLE_TOOLS if getattr(t, "name", "") in w.ACTION_TOOLS]
    assert len(hit) == len(w.ACTION_TOOLS)
    for tool in hit:
        monkeypatch.setattr(tool, "forward", lambda *a, _n=tool.name, **k: pytest.fail(f"{_n} ran"))


class _Bus:
    def __init__(self) -> None:
        self.pushed: list[ContextEvent] = []

    def push_threadsafe(self, event: ContextEvent) -> None:
        self.pushed.append(event)


def _source(tmp_path: Path, spec: dict = PDF_SPEC, **add: Any) -> tuple[w.WatchSource, _Bus, dict]:
    store = w.WatchStore(tmp_path / "watches.json")
    watch = store.add(spec, "s", **add)
    bus = _Bus()
    return w.WatchSource(bus, store, clock=lambda: NOW), bus, watch


def test_observe_only_publishes_a_card_idea(tmp_path, monkeypatch) -> None:
    _forbid_actions(monkeypatch)
    src, bus, watch = _source(tmp_path)
    src.observe(_file(f"{HOME}/Downloads/a.pdf"))
    src.observe(_file(f"{HOME}/Downloads/a.txt"))
    src.observe(ContextEvent(event_type="pattern_detected", metadata={}))
    [event] = bus.pushed
    assert event.event_type == "pattern_detected"
    assert event.metadata["trigger"] == f"watch:{watch['id']}"


def test_same_event_is_offered_once_and_cooldown_applies(tmp_path) -> None:
    src, bus, _ = _source(tmp_path, cooldown_minutes=30)
    src.observe(_file(f"{HOME}/Downloads/a.pdf"))
    src.observe(_file(f"{HOME}/Downloads/a.pdf", "renamed"))  # same file: already offered
    src.observe(_file(f"{HOME}/Downloads/b.pdf"))  # within the cooldown
    assert len(bus.pushed) == 1


def test_same_event_once_even_without_cooldown(tmp_path) -> None:
    src, bus, _ = _source(tmp_path, cooldown_minutes=0)
    src.observe(_file(f"{HOME}/Downloads/a.pdf"))
    src.observe(_file(f"{HOME}/Downloads/a.pdf", "renamed"))
    src.observe(_file(f"{HOME}/Downloads/b.pdf"))
    assert len(bus.pushed) == 2


def test_per_day_limit(tmp_path) -> None:
    clock = {"now": NOW}
    store = w.WatchStore(tmp_path / "watches.json")
    store.add(PDF_SPEC, daily_limit=2, cooldown_minutes=0)
    bus = _Bus()
    src = w.WatchSource(bus, store, clock=lambda: clock["now"])
    for i in range(4):
        src.observe(_file(f"{HOME}/Downloads/{i}.pdf"))
    assert len(bus.pushed) == 2
    clock["now"] = NOW + timedelta(days=1)
    src.observe(_file(f"{HOME}/Downloads/next-day.pdf"))
    assert len(bus.pushed) == 3


def test_three_denials_in_a_row_switch_a_watch_off(tmp_path) -> None:
    t = {"now": 1000.0}
    store = w.WatchStore(tmp_path / "watches.json", clock=lambda: t["now"])
    watch = store.add(PDF_SPEC, cooldown_minutes=0)
    log = DecisionLog(None, clock=lambda: t["now"])
    trig = f"watch:{watch['id']}"
    t["now"] = 2000.0
    for decision in ("denied", "denied", "approved", "denied", "denied"):
        log.record("move_file", trig, decision)
    log.record("move_file", "watch:other", "denied")
    bus = _Bus()
    src = w.WatchSource(bus, store, decisions=log, clock=lambda: NOW)
    src.observe(_file(f"{HOME}/Downloads/a.pdf"))
    assert len(bus.pushed) == 1  # only two in a row
    log.record("move_file", trig, "denied")
    src.observe(_file(f"{HOME}/Downloads/b.pdf"))
    assert len(bus.pushed) == 1
    saved = store.get(watch["id"])
    assert saved["enabled"] is False and "3 denials" in saved["paused_reason"]
    # Switching it back on starts a fresh count.
    t["now"] = 3000.0
    store.update(watch["id"], enabled=True)
    src.observe(_file(f"{HOME}/Downloads/c.pdf"))
    assert len(bus.pushed) == 2


def test_time_watch_fires_once_on_its_day(tmp_path) -> None:
    src, bus, watch = _source(tmp_path, TIME_SPEC)
    assert src.tick(NOW.replace(hour=8, minute=0)) == []
    [idea] = src.tick(NOW.replace(hour=8, minute=31))
    assert idea["builtin_tool"] == "open_url" and idea["arguments"] == {"url": "https://example.com/status"}
    assert src.tick(NOW.replace(hour=8, minute=40)) == []
    assert src.tick(NOW + timedelta(days=1)) == []  # Wednesday is not a watch day
    late = src.store.add(TIME_SPEC, cooldown_minutes=0)
    assert src.tick(NOW.replace(hour=11)) == []  # missed by more than the grace
    assert src.store.get(late["id"])["time_fired_on"] == "2026-11-10"
    assert len(bus.pushed) == 1


def test_store_is_private_and_capped(tmp_path) -> None:
    store = w.WatchStore(tmp_path / "w" / "watches.json")
    for _ in range(w.MAX_WATCHES):
        store.add(EVENT_SPEC)
    with pytest.raises(w.WatchLimitError):
        store.add(EVENT_SPEC)
    path = tmp_path / "w" / "watches.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert len(w.WatchStore(path).list()) == w.MAX_WATCHES
    # A hand-edited invalid watch is dropped on load, not trusted.
    data = json.loads(path.read_text())
    data["watches"][0]["spec"]["action"]["tool"] = "generate_custom_tool"
    path.write_text(json.dumps(data))
    assert len(w.WatchStore(path).list()) == w.MAX_WATCHES - 1


# ---- backtest ----------------------------------------------------------------


def _sources(**over: Any) -> w.BacktestSources:
    def rows(since: datetime) -> list[dict]:
        return [
            {"event_type": "file", "action": "created", "folder": DL, "ext": ".pdf"},
            {"event_type": "file", "action": "renamed", "folder": DL, "ext": ".pdf"},
            {"event_type": "file", "action": "created", "folder": DL, "ext": ".png"},
            {"event_type": "file", "action": "created", "folder": "~/Desktop", "ext": ".pdf"},
            {"event_type": "mail_received", "bulk": 0},
        ]

    def files(query: str, folder: str = "", kind: str = "", limit: int = 50) -> dict:
        recent = (NOW - timedelta(days=2)).timestamp()
        old = (NOW - timedelta(days=60)).timestamp()
        return {"status": "ok", "results": [
            {"path": f"{HOME}/Downloads/acme invoice.pdf", "modified": recent},
            {"path": f"{HOME}/Downloads/old.pdf", "modified": old},
            {"path": f"{HOME}/Downloads/deep/x.pdf", "modified": recent},
        ]}

    base = dict(activity_rows=rows, find_files=files,
                recent_mail=lambda since: {"status": "error", "error": "the daemon needs Full Disk Access"},
                list_events=lambda s, e: {"status": "ok", "events": [
                    {"title": "Standup", "start": "2026-11-09T09:00:00-08:00"},
                    {"title": "Lunch", "start": "2026-11-08T12:00:00-08:00"}]})
    base.update(over)
    return w.BacktestSources(**base)


def test_backtest_counts_file_history_with_spotlight_examples() -> None:
    got = w.backtest(PDF_SPEC, _sources(), NOW)
    assert got["matches"] == 2  # two .pdf arrivals in ~/Downloads
    assert [e["text"] for e in got["examples"]] == ["acme invoice.pdf"]
    assert got["summary"].startswith("Would have matched 2 times in the last 30 days")


def test_backtest_name_filter_uses_spotlight_and_says_history_cannot() -> None:
    spec = {**PDF_SPEC, "trigger": {**PDF_SPEC["trigger"], "name_contains": "invoice"}}
    got = w.backtest(spec, _sources(), NOW)
    assert got["matches"] == 1
    hist = next(s for s in got["sources"] if s["name"] == "Activity history")
    assert hist["status"] == "skipped" and "not file names" in hist["reason"]


def test_backtest_names_an_unavailable_source() -> None:
    got = w.backtest(MAIL_SPEC, _sources(), NOW)
    assert got["matches"] is None
    assert got["unavailable"] == ["Mail: the daemon needs Full Disk Access"]
    assert "Couldn't check" in got["summary"] and "Full Disk Access" in got["summary"]
    broken = w.backtest(PDF_SPEC, _sources(activity_rows=lambda s: 1 / 0), NOW)
    assert broken["matches"] == 1 and "Activity history: division by zero" in broken["unavailable"]


def test_backtest_mail_calendar_and_time() -> None:
    mail = w.backtest(MAIL_SPEC, _sources(recent_mail=lambda since: {"status": "ok", "messages": [
        {"sender": "Pat <pat@acme-cpa.com>", "subject": "Invoice 1", "date": "2026-11-01", "mailbox": "INBOX"},
        {"sender": "x@y.com", "subject": "Invoice 2", "date": "2026-11-02", "mailbox": "INBOX"}]}), NOW)
    assert mail["matches"] == 1 and mail["examples"][0]["text"] == "pat@acme-cpa.com: Invoice 1"
    cal = w.backtest(EVENT_SPEC, _sources(), NOW)
    assert cal["matches"] == 1 and cal["examples"][0]["text"] == "Standup"
    timed = w.backtest(TIME_SPEC, _sources(), NOW)
    assert timed["matches"] == 10  # Mon + Tue at 08:30, Oct 11 09:00 .. Tue Nov 10 09:00
    assert len(timed["examples"]) == w.MAX_EXAMPLES


def test_backtest_is_read_only(monkeypatch) -> None:
    _forbid_actions(monkeypatch)
    w.backtest(PDF_SPEC, _sources(), NOW)


# ---- compile (the one model call) --------------------------------------------


def test_compiler_validates_the_model_answer() -> None:
    asked: list[str] = []

    async def good(text: str) -> str:
        asked.append(text)
        return "```json\n" + json.dumps(PDF_SPEC) + "\n```"

    got = asyncio.run(w.Compiler(good).compile("when a PDF lands in Downloads, file it in Receipts"))
    assert got["spec"] == w.validate_spec(PDF_SPEC) and "Receipts" in got["words"]
    assert asked == ["Sentence: when a PDF lands in Downloads, file it in Receipts"]

    async def sneaky(text: str) -> str:
        return json.dumps(_with(PDF_SPEC, tool="generate_custom_tool", arguments={}))

    bad = asyncio.run(w.Compiler(sneaky).compile("make me a tool"))
    assert "spec" not in bad and any("not allowed" in p for p in bad["problems"])

    async def refuse(text: str) -> str:
        return '{"error": "Watches cannot read message bodies."}'

    assert asyncio.run(w.Compiler(refuse).compile("x"))["error"] == "Watches cannot read message bodies."


def test_compiler_does_not_send_personal_text_to_a_cloud_engine(monkeypatch) -> None:
    async def never(text: str) -> str:
        raise AssertionError("sentence sent to a cloud engine")

    monkeypatch.setattr(privacy, "should_redact", lambda role: role == "detection")
    got = asyncio.run(w.Compiler(never, role="detection").compile("mail from pat@acme-cpa.com"))
    assert got["reason"] == "personal_text_off" and "spec" not in got


# ---- routes: creating a watch is a user action -----------------------------------


@pytest.fixture
def client(tmp_path):
    async def model(text: str) -> str:
        return json.dumps(EVENT_SPEC)

    app = FastAPI()
    app.include_router(watch_routes.router)
    app.state.watches = w.WatchStore(tmp_path / "watches.json")
    app.state.watch_compiler = w.Compiler(model)
    app.state.watch_backtest_sources = _sources()
    app.state.approval_decisions = DecisionLog(None)
    with TestClient(app) as c:
        yield c


def test_routes_preview_save_toggle_delete(client) -> None:
    got = client.post("/api/watches/preview", json={"sentence": "before standup, make a note"}).json()
    assert got["backtest"]["matches"] == 1 and "standup" in got["words"]
    assert client.get("/api/watches").json() == []  # preview saves nothing
    saved = client.post("/api/watches", json={"spec": got["spec"], "sentence": "before standup"}).json()
    assert saved["enabled"] and saved["daily_limit"] == w.DEFAULT_DAILY_LIMIT
    assert saved["trigger_kind"] == f"watch:{saved['id']}"
    off = client.patch(f"/api/watches/{saved['id']}", json={"enabled": False, "daily_limit": 99}).json()
    assert off["enabled"] is False and off["daily_limit"] == w.MAX_DAILY_LIMIT
    assert client.delete(f"/api/watches/{saved['id']}").json()["removed"] == 1
    assert client.delete(f"/api/watches/{saved['id']}").status_code == 404


def test_routes_reject_bad_specs_and_the_limit(client) -> None:
    bad = _with(PDF_SPEC, tool="generate_custom_tool", arguments={})
    assert "problems" in client.post("/api/watches/preview", json={"spec": bad}).json()
    assert client.post("/api/watches", json={"spec": bad}).status_code == 422
    for _ in range(w.MAX_WATCHES):
        assert client.post("/api/watches", json={"spec": EVENT_SPEC}).status_code == 200
    assert client.post("/api/watches", json={"spec": EVENT_SPEC}).status_code == 409


def test_list_switches_off_a_watch_denied_three_times(client) -> None:
    saved = client.post("/api/watches", json={"spec": EVENT_SPEC}).json()
    log = client.app.state.approval_decisions
    for _ in range(3):
        log.record("create_note", saved["trigger_kind"], "denied")
    [row] = client.get("/api/watches").json()
    assert row["enabled"] is False and row["paused_reason"]


def test_no_tool_can_create_or_edit_a_watch(client) -> None:
    import tools

    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.AVAILABLE_TOOLS}
    assert not {n for n in names if "watch" in n}
    for tool in tools.AVAILABLE_TOOLS:
        fn = getattr(tool, "forward", tool)
        assert "watches" not in getattr(fn, "__module__", "")
    # A GET (all open_url could ever make) never creates one.
    assert client.get("/api/watches", params={"spec": json.dumps(EVENT_SPEC)}).json() == []
    assert client.get("/api/watches/preview").status_code == 405
