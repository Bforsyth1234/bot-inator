"""Wave 14 close: hooks the runner added (#72 #73 #76 #77 #80)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import orchestrator as orch_mod
from ai import preference_learning as pl
from ai import prepared_offers as po
from ai import privacy
from ai.idea_outcomes import DecisionLog
from ai.idea_sources import BUILTIN_FOLLOW_UPS, FOLLOW_UPS, IdeaState, NextStepSuggester
from ai.memory import Memory, _fallback_embed
from ai.orchestrator import Orchestrator
from events import daily_brief as db
from events import meeting_prep as mp
from events.event_bus import ContextEvent
from tests.test_integration import client  # noqa: F401  - pytest fixture
from tests.test_wave13_wiring import _answer_soon, _approval_orch, _fake_for_wrapper, _Loop
from tools.move_file import move_file

ROOT = Path(__file__).resolve().parents[2]
SWIFT = ROOT / "MenuBarUI" / "MenuBarUI"
TZ = timezone(timedelta(hours=-7))
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=TZ)


@pytest.fixture
def mem(tmp_path):
    m = Memory(db_path=tmp_path / "memory.db", embedder=lambda t: _fallback_embed(t, 8),
               dim=8, max_rows=50)
    yield m
    m.close()


def _stop(mem: Memory, tool: str, trigger: str) -> None:
    log = DecisionLog(None)
    for _ in range(pl.DENIALS_TO_STOP):
        log.record(tool, trigger, "denied")
    assert pl.learn(log, mem)


# ---- #80 attendees ---------------------------------------------------------------


def test_attendees_are_a_personal_event_field() -> None:
    assert privacy.PERSONAL_EVENT_FIELDS["event_starting_soon"] == ("attendees",)
    event = ContextEvent(event_type="event_starting_soon", metadata={
        "title": "Sync", "attendees": [{"name": "Alice", "email": "alice@example.com"}]})
    red = privacy.redacted_event(event)
    assert red.metadata["attendees"] == privacy.PLACEHOLDER and red.metadata["title"] == "Sync"
    with mock.patch.object(privacy, "should_redact", return_value=True):
        from ai.open_help import _describe

        assert "alice@example.com" not in _describe(privacy.redact_event(event, "detection"))


def _prep_meta() -> dict[str, Any]:
    start = datetime.now().astimezone() + timedelta(minutes=10)
    return {"title": "Q4 budget", "start": start.isoformat(), "location": "", "video_url": "",
            "attendees": [{"name": "Me", "email": "me@example.com", "is_self": True},
                          {"name": "Alice", "email": "alice@example.com"}, "bob@example.com"]}


def _prep(**kw: Any) -> dict[str, Any]:
    def mail_with(addresses, since):
        assert "alice@example.com" in addresses  # still looked up locally
        return {"status": "ok", "messages": [
            {"message_id": 1, "sender": "Alice <alice@example.com>", "subject": "Numbers", "date": "2026-11-09"}]}

    return mp.gather_prep(_prep_meta(), mail_with=mail_with,
                          find_files=lambda q, limit=5: {"status": "ok", "results": []},
                          find_note=lambda t: {"status": "ok", "notes": []},
                          redact_subject=lambda s: s, **kw)


def test_meeting_prep_drops_addresses_under_cloud_main_only() -> None:
    with mock.patch.object(privacy, "should_redact", side_effect=lambda role: role == "main"):
        cloud = _prep()
    assert "@example.com" not in cloud["text"]
    assert cloud["attendees"]["items"] == ["Alice", mp.ADDRESS_HIDDEN]
    assert cloud["mail"]["items"][0]["sender"] == "Alice"
    with mock.patch.object(privacy, "should_redact", return_value=False):
        local = _prep()
    assert "Alice <alice@example.com>" in local["text"]


# ---- #72 learned preferences ---------------------------------------------------


def test_preferences_route_mounted(client) -> None:  # noqa: F811
    assert client.get("/api/preferences/learned").status_code == 200


def test_each_decision_learns_off_the_loop(mem) -> None:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        orch.memory = mem
        orch._current_event_type = "mail_received"
        for _ in range(pl.DENIALS_TO_STOP):
            _answer_soon(orch, False)
            await orch._request_approval("evt", "create_reminder", {"kwargs": {"title": "x"}}, "r")
            orch._pending.clear()  # the test answers by taking the first pending card
            await orch._preference_learning
        return orch

    asyncio.run(scenario())
    assert pl.is_suppressed(mem, "create_reminder", "mail_received")


def test_suppressed_call_is_refused_before_its_card(mem) -> None:
    _stop(mem, "move_file", "file")
    asked: list[dict] = []
    ran: list[str] = []
    with _Loop() as loop:
        fake = _fake_for_wrapper(loop, asked, memory=mem, _current_event_type="file",
                                 _current_idea=None, _last_offer_id=None)
        stub = SimpleNamespace(name="move_file", forward=lambda *a, **k: ran.append("ran") or "ok")
        wrapped = Orchestrator._wrap_tool_for_approval(fake, stub)
        result = wrapped.forward(src="a", dst_dir="b")
        assert result["error"] == "user_declined_before" and asked == [] and ran == []
        # A typed chat request is never blocked by a learned rule; it still asks.
        fake._current_event_type = "user_message"
        wrapped.forward(src="a", dst_dir="b")
    assert [a["tool_name"] for a in asked] == ["move_file"] and ran == ["ran"]


def test_suppressed_builtin_idea_is_not_offered(mem) -> None:
    _stop(mem, "create_reminder", "mail_deadline")
    runs: list[str] = []
    orch = Orchestrator.__new__(Orchestrator)
    orch.memory = mem
    orch.pattern_recognizer = None
    orch.idea_sources = []
    orch._current_builtin_idea = None

    async def emit(*_a: Any) -> None:
        return None

    async def run_agent(event_id: str, prompt: str, max_steps: int = 4) -> str:
        runs.append(prompt)
        return "ok"

    orch._emit_thought = emit  # type: ignore[method-assign]
    orch._run_agent = run_agent  # type: ignore[method-assign]
    for trigger in ("mail_deadline", "mail_invite"):
        meta = {"tool_name": "x", "builtin_tool": "create_reminder", "trigger": trigger,
                "source": "next_step", "description": "d", "expected_logic": "e"}
        asyncio.run(orch._handle_event(ContextEvent(event_type="pattern_detected", metadata=meta)))
    assert len(runs) == 1  # only mail_invite reached the agent


def test_recall_includes_learned_preferences(mem) -> None:
    _stop(mem, "move_file", "file")
    orch = Orchestrator.__new__(Orchestrator)
    orch.memory = mem
    # Similarity search alone returns other memories; the preference rides along.
    with mock.patch.object(mem, "recall_memory", return_value=["the user likes tea"]):
        got = asyncio.run(orch._recall_memories(ContextEvent(event_type="app_activated", app_name="Mail")))
    assert got[0] == "the user likes tea"
    assert any("Don't offer move file" in t for t in got)


def test_learned_preferences_view_embedded() -> None:
    assert "LearnedPreferencesView()" in (SWIFT / "Views" / "ToolManagerView.swift").read_text()


# ---- #73 waiting on + #77 look-ahead in the brief --------------------------------


ITEM = {"message_id": 12, "name": "Sam", "address": "sam@example.com", "to": ["sam@example.com"],
        "subject": "Budget?", "sent": "2026-11-05", "days": 5, "line": "waiting on Sam: Budget?"}


def _waiting(items: list[dict]) -> Any:
    def fn(now=None, **kw: Any) -> dict:
        assert "redact_subject" in kw
        return {"status": "ok", "items": items, "truncated": False}
    return fn


def _gap(key: str) -> dict:
    return {"detector": "deadline", "key": key, "line": f"No reminder for {key}", "evidence": {},
            "offer": {"tool_name": f"remind_{key}", "description": f"remind {key}",
                      "builtin_tool": "create_reminder", "source": "lookahead",
                      "trigger": "lookahead_deadline"}}


def _gather(**kw: Any) -> dict:
    calls: dict[str, Any] = {"reminders": 0}

    def reminders(**k: Any) -> dict:
        calls["reminders"] += 1
        return {"status": "ok", "reminders": []}

    def check_gaps(now, **sources: Any) -> dict:
        calls["gap_sources"] = sources
        sources["list_reminders"](list_name="", include_completed=False)
        return {"status": "ok", "days": 3, "gaps": [_gap("report")], "skipped": [],
                "text": "Look-ahead, next 3 days (1):\n- No reminder for report"}

    events = lambda **k: {"status": "ok", "events": []}  # noqa: E731
    mail = lambda since: {"status": "ok", "messages": []}  # noqa: E731
    kw.setdefault("waiting_on", _waiting([ITEM]))
    payload = db.gather_brief(NOW, list_events=events, recent_mail=mail, list_reminders=reminders,
                              redact_subject=lambda s: s, check_gaps=check_gaps, **kw)
    payload["_calls"] = calls
    payload["_events"], payload["_mail"] = events, mail
    return payload


def test_brief_has_waiting_on_section() -> None:
    assert ("waiting_on", "Waiting on") in db.SOURCES
    payload = _gather()
    assert "Waiting on (1):" in payload["text"] and "waiting on Sam: Budget? (5 days) [id 12]" in payload["text"]
    assert "1 waiting" in db.describe(db.make_event({k: v for k, v in payload.items() if not k.startswith("_")}))
    broken = _gather(waiting_on=lambda now=None, **k: {"status": "unavailable", "reason": "no index"})
    assert "Waiting on" in broken["unavailable"] and "Waiting on: unavailable: no index" in broken["text"]


def test_brief_runs_lookahead_on_its_own_sources() -> None:
    payload = _gather()
    sources = payload["_calls"]["gap_sources"]
    assert sources["list_events"] is payload["_events"] and sources["recent_mail"] is payload["_mail"]
    assert payload["_calls"]["reminders"] == 1  # asked once, shared with the look-ahead
    assert "- No reminder for report" in payload["text"]
    assert "keep each Look-ahead line as written" in db.build_prompt(db.make_event({"text": payload["text"]}))


def test_waiting_on_days_setting_key() -> None:
    import user_settings

    assert "AGENT_WAITING_ON_DAYS" in user_settings.ALLOWED_KEYS


class _Bus:
    def __init__(self) -> None:
        self.events: list[ContextEvent] = []

    def push_threadsafe(self, event: ContextEvent) -> None:
        self.events.append(event)


def _scheduler(tmp_path, payload: dict, clock: list, feedback: Any = None) -> tuple[db.DailyBriefScheduler, _Bus]:
    bus = _Bus()
    sched = db.DailyBriefScheduler(
        bus, state_path=tmp_path / "brief.json", clock=lambda: clock[0], gather=lambda now: dict(payload),
        enabled=lambda: True, time_of_day=lambda: db.parse_time("08:00"), startup_delay=0,
    )
    sched.attach_ideas(IdeaState(None), feedback)
    return sched, bus


def _brief_payload(n_items: int, gap_keys: list[str]) -> dict:
    items = [{**ITEM, "message_id": 100 + i} for i in range(n_items)]
    gaps = [_gap(k) for k in gap_keys]
    return {"day": "Tuesday", "text": "x", "waiting_on": {"status": "ok", "items": items},
            "lookahead": {"status": "ok", "gaps": gaps}}


def test_scheduled_brief_pushes_capped_nudges_and_gap_offers_with_cooldowns(tmp_path) -> None:
    clock = [NOW]
    sched, bus = _scheduler(tmp_path, _brief_payload(5, ["deadline:a", "deadline:b"]), clock)
    sched.check_once()
    kinds = [(e.event_type, (e.metadata or {}).get("source")) for e in bus.events]
    assert kinds[0] == ("daily_brief", None)
    nudges = [e for e in bus.events if e.metadata.get("source") == "waiting_on"]
    gaps = [e for e in bus.events if e.metadata.get("source") == "lookahead"]
    assert len(nudges) == db.MAX_NUDGES and all(e.event_type == "pattern_detected" for e in nudges)
    assert nudges[0].metadata["builtin_tool"] == "draft_email" and nudges[0].metadata["reply_to_message_id"] == 100
    assert [g.metadata["lookahead_key"] for g in gaps] == ["deadline:a", "deadline:b"]
    # Next day: the two nudges not shown yet come; shown ones and gaps cool down.
    bus.events.clear()
    clock[0] = NOW + timedelta(days=1)
    sched.check_once()
    later = [e.metadata.get("reply_to_message_id") for e in bus.events[1:]]
    assert later == [103, 104]
    # "Brief me now" never pushes offers.
    bus.events.clear()
    sched.run_now()
    assert [e.event_type for e in bus.events] == ["daily_brief"]


def test_declined_gap_offer_is_dropped(tmp_path) -> None:
    feedback = SimpleNamespace(resembles_rejected=lambda name, desc: name == "remind_deadline:a")
    sched, bus = _scheduler(tmp_path, _brief_payload(0, ["deadline:a", "deadline:b"]), [NOW], feedback)
    sched.check_once()
    assert [e.metadata.get("lookahead_key") for e in bus.events[1:]] == ["deadline:b"]


def test_main_attaches_idea_state_to_the_brief() -> None:
    import main

    assert "attach_ideas(idea_state, idea_feedback)" in inspect.getsource(main.lifespan)


# ---- #76 prepared offers -------------------------------------------------------


def _doc_event(path: str) -> ContextEvent:
    return ContextEvent(event_type="file", metadata={"path": path, "action": "created"})


def test_document_trigger_registered() -> None:
    assert NextStepSuggester.classify(_doc_event("/Users/me/Downloads/invoice_7.pdf")) == po.DOCUMENT_TRIGGER
    assert FOLLOW_UPS[po.DOCUMENT_TRIGGER] == po.DOCUMENT_FOLLOW_UP
    assert BUILTIN_FOLLOW_UPS[po.DOCUMENT_TRIGGER] == (po.DOCUMENT_IDEA_NAME, "move_file")


class _Preparer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[str] = []

    async def prepare(self, idea: dict, event: ContextEvent) -> dict:
        self.seen.append(idea["trigger"])
        if self.fail:
            raise RuntimeError("boom")
        return {**idea, po.PREPARED_KEY: {"kind": "x", "headline": "h", "actions": []}}


def _observe(preparer: _Preparer, event: ContextEvent) -> list[ContextEvent]:
    published: list[ContextEvent] = []

    async def publish(e: ContextEvent) -> None:
        published.append(e)

    async def run() -> None:
        src = NextStepSuggester(event_bus=SimpleNamespace(push=publish), evaluate=None,  # type: ignore[arg-type]
                                publish=publish, state=IdeaState(None), preparer=preparer)
        task = src.observe(event)
        if task is not None:
            await task

    asyncio.run(run())
    return published


def test_next_step_runs_the_preparer_for_prepared_kinds_only() -> None:
    prep = _Preparer()
    [doc] = _observe(prep, _doc_event("/Users/me/Downloads/invoice_7.pdf"))
    assert po.PREPARED_KEY in doc.metadata
    [ics] = _observe(prep, _doc_event("/Users/me/Downloads/invite.ics"))
    assert po.PREPARED_KEY not in ics.metadata and prep.seen == [po.DOCUMENT_TRIGGER]
    [plain] = _observe(_Preparer(fail=True), _doc_event("/Users/me/Downloads/invoice_8.pdf"))
    assert po.PREPARED_KEY not in plain.metadata and plain.metadata["builtin_tool"] == "move_file"


def test_main_builds_the_preparer(client) -> None:  # noqa: F811
    [nss] = [s for s in client.app.state.orchestrator.idea_sources if isinstance(s, NextStepSuggester)]
    assert isinstance(nss.preparer, po.Preparer) and nss.preparer._evaluate is not None


def test_request_approval_sends_actions() -> None:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        orch.approval_timeout = 0.05
        await orch._request_approval("evt", "prepared_offer", {"kwargs": {}}, "r",
                                     actions=[{"tool": "move_file", "args": {"src": "a"}, "label": "Move"}])
        return orch

    orch = asyncio.run(scenario())
    [msg] = _cards(orch)
    assert msg.payload.actions[0].tool == "move_file" and msg.payload.actions[0].label == "Move"


OFFER = po.PreparedOffer(po.DOCUMENT_TRIGGER, "File it and remind you?", [
    po.OfferAction("move_file", {"src": "/d/inv.pdf", "dst_dir": "/x"}, label="File"),
    po.OfferAction("create_reminder", {"title": "Pay", "due": "2026-11-20"}, label="Remind"),
]).to_dict()


def _cards(orch: Orchestrator) -> list[Any]:
    from schemas.ws_messages import ApprovalRequest

    return [m for m in orch.sent if isinstance(m, ApprovalRequest)]  # type: ignore[attr-defined]


def _prepared_orch(ran: list[tuple[str, dict]]) -> Orchestrator:
    orch = _approval_orch()
    orch.memory = None
    orch.pattern_recognizer = None
    orch._turn_tool_calls = set()

    def tool(name: str) -> Any:
        return SimpleNamespace(name=name, forward=lambda **k: ran.append((name, k)) or {"status": "ok"})

    orch.tools = [tool("move_file"), tool("create_reminder")]

    async def run_agent(*_a: Any, **_k: Any) -> str:
        raise AssertionError("a prepared offer never runs the agent")

    orch._run_agent = run_agent  # type: ignore[method-assign]
    return orch


def _prepared_event() -> ContextEvent:
    meta = {"tool_name": po.DOCUMENT_IDEA_NAME, "builtin_tool": "move_file", "trigger": po.DOCUMENT_TRIGGER,
            "source": "next_step", "description": "d", po.PREPARED_KEY: OFFER, "card_text": "File it"}
    return ContextEvent(event_type="pattern_detected", metadata=meta)


def _run_prepared(approved: bool, edited: Any = None) -> tuple[Orchestrator, list]:
    ran: list[tuple[str, dict]] = []

    async def scenario() -> Orchestrator:
        orch = _prepared_orch(ran)
        _answer_soon(orch, approved, edited)
        await orch._handle_event(_prepared_event())
        return orch

    return asyncio.run(scenario()), ran


def test_prepared_offer_card_is_the_approval_and_runs_ticked_actions() -> None:
    edited = {"actions": [{"tool": "move_file", "enabled": True}, {"tool": "create_reminder", "enabled": False}]}
    orch, ran = _run_prepared(True, edited)
    [msg] = _cards(orch)
    assert msg.payload.tool_name == po.PREPARED_TOOL_NAME and len(msg.payload.actions) == 2
    assert ran == [("move_file", {"src": "/d/inv.pdf", "dst_dir": "/x"})]
    [offer] = orch.idea_outcomes.offers
    assert offer["one_shot"] and offer["verdict"] == "approved"
    [row] = orch.approval_decisions.decisions
    assert (row["tool_name"], row["trigger_kind"]) == (po.PREPARED_TOOL_NAME, po.DOCUMENT_TRIGGER)


def test_prepared_offer_denied_runs_nothing_but_is_recorded() -> None:
    orch, ran = _run_prepared(False)
    assert ran == []
    assert orch.idea_outcomes.offers[0]["verdict"] == "declined"
    assert orch.approval_decisions.decisions[0]["decision"] == "denied"


def test_prepared_offer_suppressed_shows_no_card(mem) -> None:
    _stop(mem, po.PREPARED_TOOL_NAME, po.DOCUMENT_TRIGGER)
    ran: list[tuple[str, dict]] = []

    async def scenario() -> Orchestrator:
        orch = _prepared_orch(ran)
        orch.memory = mem
        await orch._handle_event(_prepared_event())
        return orch

    orch = asyncio.run(scenario())
    assert _cards(orch) == [] and ran == []


# ---- move_file new_name ---------------------------------------------------------


def test_move_file_new_name(tmp_path) -> None:
    src_dir, dst = tmp_path / "Downloads", tmp_path / "Invoices"
    src_dir.mkdir()
    dst.mkdir()
    (src_dir / "a.pdf").write_text("x")
    assert move_file(src=str(src_dir / "a.pdf"), dst_dir=str(dst), new_name="ACME 2026-11")["new_path"] == str(dst / "ACME 2026-11.pdf")
    for bad in ("../evil.pdf", "x/y.pdf", ".hidden.pdf"):
        (src_dir / "b.pdf").write_text("y")
        out = move_file(src=str(src_dir / "b.pdf"), dst_dir=str(dst), new_name=bad)
        assert out["status"] == "error" and (src_dir / "b.pdf").exists()
    out = move_file(src=str(src_dir / "b.pdf"), dst_dir=str(dst), new_name="ACME 2026-11.pdf")
    assert out["status"] == "error" and "already exists" in out["error"]
    assert (dst / "ACME 2026-11.pdf").read_text() == "x"  # not overwritten
    assert move_file(src=str(src_dir / "b.pdf"), dst_dir=str(dst), new_name="")["new_path"] == str(dst / "b.pdf")


def test_preparer_emits_new_name_and_the_approved_move_renames(tmp_path) -> None:
    downloads, invoices = tmp_path / "Downloads", tmp_path / "Invoices"
    downloads.mkdir()
    invoices.mkdir()
    doc = downloads / "acme_invoice_4471.txt"
    doc.write_text("ACME Hosting\nInvoice 4471\nAmount due: $120.00\nDue date: 2026-11-20\n")
    tools = {
        "move_file": move_file,
        "summarize_file": lambda path, max_chars=0: {"status": "ok", "preview": doc.read_text()},
        "find_files": lambda query, folder="", kind="", limit=20: {"status": "ok", "results": [
            {"path": str(invoices / "old_1.pdf")}, {"path": str(invoices / "old_2.pdf")}]},
        "create_reminder": lambda **k: {"status": "ok"},
    }
    prep = po.Preparer(evaluate=None, tools=tools, read_only={"summarize_file", "find_files"},
                       history=lambda: [], redact=lambda role: True)
    idea = asyncio.run(prep.prepare({"trigger": po.DOCUMENT_TRIGGER}, _doc_event(str(doc))))
    offer = po.PreparedOffer.from_dict(idea[po.PREPARED_KEY])
    move = next(a for a in offer.actions if a.tool == "move_file")
    assert move.args["new_name"] and "suggested name" not in offer.headline
    [result] = [r for r in po.run_approved_actions(offer, {"actions": [
        {"tool": a.tool, "enabled": a.tool == "move_file"} for a in offer.actions]}, tools)]
    assert result["result"]["status"] == "ok"
    assert (invoices / move.args["new_name"]).exists() and not doc.exists()


def test_move_file_card_has_new_name_field() -> None:
    text = (SWIFT / "Views" / "ApprovalAlert.swift").read_text()
    assert 'params: ["src", "dst_dir", "new_name"]' in text and 'key: "new_name"' in text


def test_orchestrator_module_imports_wave14_modules() -> None:
    assert orch_mod.preference_learning is pl and orch_mod.prepared_offers is po
