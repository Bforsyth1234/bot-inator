"""Wave 13 close: registrations the runner added (#69 #70 #71 #74 #75 #79)."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import orchestrator as orch_mod
from ai.idea_outcomes import DecisionLog, OutcomeStore
from ai.idea_sources import IdeaState
from ai.orchestrator import Orchestrator
from ai.routine_ideas import RoutineIdeaSource
from events.event_bus import ContextEvent
from schemas.ws_messages import ApprovalResponsePayload
from tests.test_integration import client  # noqa: F401  - pytest fixture


# ---- helpers ---------------------------------------------------------------


def _approval_orch() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._pending = {}
    orch.approval_timeout = 2.0
    orch._seq = 0
    orch._next_seq = lambda: 1  # type: ignore[method-assign]
    orch.sent = []  # type: ignore[attr-defined]

    async def send(msg: Any) -> None:
        orch.sent.append(msg)  # type: ignore[attr-defined]

    orch._send = send  # type: ignore[method-assign]
    orch.idea_sources = []
    orch._current_builtin_idea = None
    orch._current_idea = None
    orch._current_event_type = "pattern_detected"
    orch._turn_kind = "pattern"
    orch.idea_outcomes = OutcomeStore(None)
    orch.approval_decisions = DecisionLog(None)
    return orch


def _answer_soon(orch: Orchestrator, approved: bool, edited: Any = None) -> None:
    async def later() -> None:
        while not orch._pending:
            await asyncio.sleep(0.01)
        rid, fut = next(iter(orch._pending.items()))
        fut.set_result(ApprovalResponsePayload(request_id=rid, approved=approved, edited_args=edited))
    asyncio.get_running_loop().create_task(later())


class _Loop:
    """A background event loop for driving ``gated_forward`` from a thread."""

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc: Any) -> None:
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.01), self.loop).result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def _fake_for_wrapper(loop: asyncio.AbstractEventLoop, asked: list[dict], **extra: Any) -> Any:
    async def request_approval(**kw: Any) -> tuple[bool, None]:
        asked.append(kw)
        return True, None

    async def emit_thought(*_a: Any, **_k: Any) -> None:
        return None

    fields = dict(
        event_bus=SimpleNamespace(loop=loop),
        _current_event_id="evt_test",
        _turn_tool_calls=set(),
        _turn_done={},
        approval_timeout=2,
        _request_approval=request_approval,
        _emit_thought=emit_thought,
        _apply_edits=Orchestrator._apply_edits,
        _dynamic_tool_names=set(),
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


# ---- #69 MCP ---------------------------------------------------------------


class _FakeMcp:
    def __init__(self, marked: set[str]) -> None:
        self.marked = marked

    def is_user_marked_read_only(self, name: str) -> bool:
        return name in self.marked

    def server_of(self, name: str) -> str | None:
        return name.split("__")[0] if "__" in name else None

    def is_mcp_tool(self, name: str) -> bool:
        return "__" in name


def test_mcp_user_flag_bypasses_gate_and_unflagged_is_gated() -> None:
    asked: list[dict] = []
    with _Loop() as loop:
        fake = _fake_for_wrapper(loop, asked, mcp_manager=_FakeMcp({"srv__free"}))
        for name in ("srv__free", "srv__gated"):
            stub = SimpleNamespace(name=name, forward=lambda *a, **k: "ok")
            Orchestrator._wrap_tool_for_approval(fake, stub).forward()
    assert [a["tool_name"] for a in asked] == ["srv__gated"]
    assert "MCP server 'srv'" in asked[0]["reasoning"]


def test_mcp_manager_started_attached_and_routes_mounted(client) -> None:  # noqa: F811
    state = client.app.state
    assert state.mcp_manager is not None
    assert state.orchestrator.mcp_manager is state.mcp_manager
    assert client.get("/api/mcp/servers").status_code == 200


def test_api_tools_labels_mcp_tools(client) -> None:  # noqa: F811
    orch = client.app.state.orchestrator
    fake_tool = SimpleNamespace(name="srv__echo", description="Echo.")
    with mock.patch.object(orch, "mcp_manager", _FakeMcp(set())), \
            mock.patch.object(orch, "tools", [*orch.tools, fake_tool]):
        rows = {r["name"]: r for r in client.get("/api/tools").json()}
    assert rows["srv__echo"]["kind"] == "mcp"
    assert "kind" not in rows["read_email"]


# ---- #79 outcomes ----------------------------------------------------------


def test_idea_card_records_offer_and_decision() -> None:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        orch._current_idea = {"source": "open_help", "trigger": "pdf_download"}
        _answer_soon(orch, True)
        await orch._request_approval(
            "evt", "generate_custom_tool", {"kwargs": {"tool_name": "pdf_sorter"}}, "r",
        )
        return orch

    orch = asyncio.run(scenario())
    [offer] = orch.idea_outcomes.offers
    assert (offer["source"], offer["tool"], offer["idea_kind"], offer["verdict"]) == (
        "open_help", "pdf_sorter", "pdf_download", "approved")
    assert orch._last_offer_id == offer["id"]
    [row] = orch.approval_decisions.decisions
    assert (row["tool_name"], row["trigger_kind"], row["decision"]) == (
        "generate_custom_tool", "pdf_download", "approved")


def test_builtin_idea_is_one_shot_and_edits_are_logged_as_facts() -> None:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        idea = {"source": "next_step", "trigger": "mail_invite", "builtin_tool": "create_event"}
        orch._current_builtin_idea = orch._current_idea = idea
        _answer_soon(orch, True, {"kwargs": {"title": "Sync with Bo"}})
        await orch._request_approval("evt", "create_event", {"kwargs": {"title": "Sync"}}, "r")
        return orch

    orch = asyncio.run(scenario())
    [offer] = orch.idea_outcomes.offers
    assert offer["one_shot"] is True and offer["source"] == "next_step"
    [row] = orch.approval_decisions.decisions
    assert row["decision"] == "edited" and row["edit_facts"] == {"title": "longer"}
    assert "Sync with Bo" not in str(row)  # never the values


def test_plain_gated_call_logs_denial_but_no_offer() -> None:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        orch._current_event_type = "file"
        _answer_soon(orch, False)
        await orch._request_approval("evt", "move_file", {"kwargs": {"src": "a"}}, "r")
        return orch

    orch = asyncio.run(scenario())
    assert orch.idea_outcomes.offers == []
    [row] = orch.approval_decisions.decisions
    assert (row["tool_name"], row["trigger_kind"], row["decision"]) == ("move_file", "file", "denied")


def test_timeout_records_timed_out_and_no_decision() -> None:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        orch.approval_timeout = 0.05
        await orch._request_approval(
            "evt", "generate_custom_tool", {"kwargs": {"tool_name": "x_tool"}}, "r",
        )
        return orch

    orch = asyncio.run(scenario())
    assert orch.idea_outcomes.offers[0]["verdict"] == "timed_out"
    assert orch.approval_decisions.decisions == []


def test_install_links_tool_and_runs_are_counted_except_the_rerun() -> None:
    outcomes = OutcomeStore(None)
    asked: list[dict] = []
    with _Loop() as loop:
        fake = _fake_for_wrapper(
            loop, asked, idea_outcomes=outcomes, _last_offer_id=None,
            _quarantined={}, _pending_rerun_tool=None, _rerun_tool_name=None,
            _previously_declined=lambda a, k: "", _flag_quarantined_install=lambda r: r,
        )
        offer_id = outcomes.record_offer("open_help", "pdf_sorter", "approved")

        async def request_approval(**kw: Any) -> tuple[bool, None]:
            fake._last_offer_id = offer_id  # what _request_approval records
            return True, None

        fake._request_approval = request_approval
        gen = SimpleNamespace(
            name="generate_custom_tool",
            forward=lambda *a, **k: {"status": "ok", "tool_name": "pdf_sorter_v2"},
        )
        Orchestrator._wrap_tool_for_approval(fake, gen).forward(tool_name="pdf_sorter")
        assert outcomes.offers[0]["tool"] == "pdf_sorter_v2"

        fake._dynamic_tool_names = {"pdf_sorter_v2"}
        run = SimpleNamespace(name="pdf_sorter_v2", forward=lambda *a, **k: "ok")
        wrapped = Orchestrator._wrap_tool_for_approval(fake, run)
        fake._rerun_tool_name = "pdf_sorter_v2"
        wrapped.forward(path="a")  # the post-install auto-rerun
        assert outcomes.offers[0]["run_count"] == 0
        fake._rerun_tool_name = None
        wrapped.forward(path="b")
        assert outcomes.offers[0]["run_count"] == 1


def test_disable_and_delete_routes_record_outcomes(client, tmp_path, monkeypatch) -> None:  # noqa: F811
    from config import settings

    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    store = client.app.state.idea_outcomes
    assert client.app.state.orchestrator.idea_outcomes is store
    assert client.app.state.orchestrator.approval_decisions is client.app.state.approval_decisions
    for name in ("lamp_tool", "desk_tool"):
        store.record_offer("open_help", name, "approved")
        (tmp_path / f"{name}.py").write_text('"""x."""\n')
    assert client.post("/api/tools/lamp_tool/disable").status_code == 200
    assert client.delete("/api/tools/desk_tool").status_code == 200
    by_tool = {o["tool"]: o for o in store.offers}
    assert by_tool["lamp_tool"]["disabled"] is True
    assert by_tool["desk_tool"]["deleted"] is True


def test_outcomes_route_mounted(client) -> None:  # noqa: F811
    body = client.get("/api/outcomes/stats")
    assert body.status_code == 200
    assert {"sources", "idea_kinds", "decisions"} <= set(body.json())


# ---- #75 activity history + routines -----------------------------------------


def test_handle_event_records_activity() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    seen: list[str] = []

    async def emit(*_a: Any) -> None:
        return None

    orch._emit_thought = emit  # type: ignore[method-assign]
    orch.pattern_recognizer = None
    orch.idea_sources = []
    event = ContextEvent(event_type="app_activated", app_name="Slack")
    with mock.patch.object(orch_mod.activity_store, "record", side_effect=lambda e: seen.append(e.event_type)):
        asyncio.run(orch._handle_event(event))
    assert seen == ["app_activated"]


def test_activity_setting_key_and_routes(client) -> None:  # noqa: F811
    import user_settings

    assert "AGENT_ACTIVITY_HISTORY" in user_settings.ALLOWED_KEYS
    body = client.get("/api/activity")
    assert body.status_code == 200
    assert {"enabled", "count", "candidates", "locked_fields"} <= set(body.json())
    assert client.delete("/api/activity").json()["count"] == 0


def test_clear_memory_also_clears_activity(client) -> None:  # noqa: F811
    import main
    from schemas.ws_messages import Command, CommandPayload

    with mock.patch.object(main.activity_store, "clear", return_value=3) as cleared:
        cmd = Command(seq=1, timestamp="2026-11-10T00:00:00Z",
                      payload=CommandPayload(action="clear_memory"))
        asyncio.run(main._handle_command(client.app, cmd))
    cleared.assert_called_once()


TIME_CAND = {
    "id": "c1", "kind": "time_routine", "title": "Open Slack on Mondays around 09:00",
    "trigger": {"type": "time", "weekdays": ["Monday"], "hour": 9},
    "action": {"type": "app_launch", "apps": ["Slack"]},
    "evidence": {"days": 5, "count": 5, "of": 6},
    "evidence_text": "seen on 5 of the last 6 Mondays",
    "tool": "open_app", "tool_args": {"name": "Slack"},
    "summary": "Routine: opens Slack on Mondays around 09:00. Evidence: 5 days, 5 times out of 6.",
}
PAIR_CAND = {
    "id": "c2", "kind": "trigger_action",
    "title": "When a .pdf file landed in ~/Downloads, you moved it to ~/Invoices",
    "trigger": {"type": "file_arrival"}, "action": {"type": "file_move"},
    "evidence": {"days": 6, "count": 7, "of": 8},
    "evidence_text": "seen on 6 separate days (7 of 8 times)",
    "tool": "move_file", "tool_args": {"dst_dir": "/x/Invoices"},
    "summary": "Trigger: a .pdf file lands in ~/Downloads. Then: the user moves it to ~/Invoices.",
}
MONDAY_9 = datetime(2026, 11, 9, 9, 15)
TUESDAY_9 = datetime(2026, 11, 10, 9, 15)


def _routine_source(cands: list[dict], published: list[ContextEvent], **kw: Any) -> RoutineIdeaSource:
    async def publish(event: ContextEvent) -> None:
        published.append(event)

    return RoutineIdeaSource(
        event_bus=SimpleNamespace(push=publish), state=IdeaState(None), publish=publish,
        find_candidates=lambda **_k: [dict(c) for c in cands], enabled=lambda: True, **kw,
    )


def test_routine_tick_publishes_builtin_idea_in_slot_once() -> None:
    published: list[ContextEvent] = []
    src = _routine_source([TIME_CAND], published)
    assert asyncio.run(src.tick(TUESDAY_9)) is None  # not its slot
    idea = asyncio.run(src.tick(MONDAY_9))
    assert idea is not None and len(published) == 1
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert (meta["source"], meta["builtin_tool"], meta["routine_id"]) == ("routine", "open_app", "c1")
    assert "seen on 5 of the last 6 Mondays" in meta["card_text"]
    assert asyncio.run(src.tick(MONDAY_9)) is None  # cooldown on candidate id
    assert len(published) == 1


def test_routine_trigger_action_becomes_generate_idea_and_respects_switch() -> None:
    published: list[ContextEvent] = []
    src = _routine_source([PAIR_CAND], published)
    asyncio.run(src.tick(TUESDAY_9))
    meta = published[0].metadata
    assert "builtin_tool" not in meta and meta["tool_name"].startswith("routine_")
    off = _routine_source([PAIR_CAND], [], )
    off._enabled = lambda: False
    assert asyncio.run(off.tick(TUESDAY_9)) is None


def test_routine_declined_before_is_dropped() -> None:
    published: list[ContextEvent] = []
    feedback = SimpleNamespace(resembles_rejected=lambda n, d: True)
    src = _routine_source([PAIR_CAND], published, feedback=feedback)
    assert asyncio.run(src.tick(TUESDAY_9)) is None
    assert published == []


def test_routine_cloud_prompt_sees_only_the_summary() -> None:
    for cand in (TIME_CAND, PAIR_CAND):
        meta = RoutineIdeaSource.idea_for(cand)
        with mock.patch.object(orch_mod.privacy, "should_redact", return_value=True):
            prompt = Orchestrator._build_pattern_prompt(
                ContextEvent(event_type="pattern_detected", metadata=meta)
            )
        assert cand["summary"][:40] in prompt
        assert cand["evidence_text"] not in prompt
        assert "card_text" not in prompt and "Routine: Open" not in prompt


def test_routine_card_text_reaches_the_approval_card_only_for_its_tool() -> None:
    asked: list[dict] = []
    meta = RoutineIdeaSource.idea_for(TIME_CAND)
    with _Loop() as loop:
        fake = _fake_for_wrapper(loop, asked, _current_idea=meta, _last_offer_id=None)
        for name in ("open_app", "move_file"):
            stub = SimpleNamespace(name=name, forward=lambda *a, **k: "ok")
            Orchestrator._wrap_tool_for_approval(fake, stub).forward(name="Slack")
    assert "seen on 5 of the last 6 Mondays" in asked[0]["summary"]
    assert "summary" not in asked[1]


def test_main_starts_routine_ideas_task() -> None:
    import inspect

    import main

    src = inspect.getsource(main.lifespan)
    assert "RoutineIdeaSource(" in src and "routine_ideas.start()" in src


# ---- #70 reply drafts --------------------------------------------------------


def test_next_step_suggester_gets_reply_judge(client) -> None:  # noqa: F811
    from ai.idea_sources import NextStepSuggester

    [nss] = [s for s in client.app.state.orchestrator.idea_sources if isinstance(s, NextStepSuggester)]
    assert nss._reply_judge is not None


def test_user_emails_documented() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "AGENT_USER_EMAILS" in (root / "agent-daemon" / ".env.example").read_text()
    assert "AGENT_USER_EMAILS" in (root / "LIVING_SPEC.md").read_text()


# ---- #71 meeting prep --------------------------------------------------------


def test_meeting_prep_scheduler_is_idea_source_and_route_mounted(client) -> None:  # noqa: F811
    from events.meeting_prep import MeetingPrepScheduler

    state = client.app.state
    assert any(isinstance(s, MeetingPrepScheduler) for s in state.orchestrator.idea_sources)
    assert any(isinstance(s, MeetingPrepScheduler) for s in state.listeners)
    assert client.get("/api/meeting-prep").status_code == 200


def _prep_event() -> ContextEvent:
    return ContextEvent(event_type="meeting_prep", metadata={
        "title": "Design review", "when": "in 10 min", "text": "Design review at 10:00",
        "attendees": {"status": "ok", "items": [{}, {}]},
        "mail": {"status": "unavailable", "reason": "x"},
        "files": {"status": "ok", "items": []},
    })


def test_handle_event_routes_meeting_prep_and_notifies() -> None:
    thoughts: list[tuple[str, str]] = []
    notes: list[tuple[str, str]] = []

    async def emit(event_id: str, stage: str, content: str) -> None:
        thoughts.append((stage, content))

    async def run_agent(event_id: str, prompt: str, max_steps: int = 2) -> str:
        assert "prep card" in prompt and max_steps == 2
        return ""  # falls back to the gathered text

    orch = Orchestrator.__new__(Orchestrator)
    orch._emit_thought = emit  # type: ignore[method-assign]
    orch._run_agent = run_agent  # type: ignore[method-assign]
    orch._notify_meeting_prep = lambda t, c: notes.append((t, c))  # type: ignore[method-assign]
    orch.pattern_recognizer = None
    orch.idea_sources = []
    asyncio.run(orch._handle_event(_prep_event()))
    assert ("complete", "Design review at 10:00") in thoughts
    assert notes == [("Design review", "Design review at 10:00")]
    assert thoughts[0][1].startswith("Meeting prep for Design review")


def test_event_starting_soon_still_skips_the_agent() -> None:
    assert "event_starting_soon" in orch_mod._AGENT_TRIGGER_EXCLUDED_EVENT_TYPES


# ---- #74 quick ask -----------------------------------------------------------


def _chat_orch(prompts: list[str]) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.memory = None
    orch.idea_sources = []

    async def emit(*_a: Any) -> None:
        return None

    async def run_agent(event_id: str, prompt: str, max_steps: int = 6) -> str:
        prompts.append(prompt)
        return "ok"

    orch._emit_thought = emit  # type: ignore[method-assign]
    orch._run_agent = run_agent  # type: ignore[method-assign]
    orch._recall_memories = lambda e: asyncio.sleep(0, [])  # type: ignore[method-assign]
    return orch


def test_user_message_context_reaches_the_prompt() -> None:
    prompts: list[str] = []
    orch = _chat_orch(prompts)
    ctx = {"front_app": "Mail", "window_title": "Inbox", "selection": "the quarterly numbers",
           "selection_source": "selection"}
    event = ContextEvent(event_type="user_message",
                         metadata={"event_id": "m1", "text": "summarize this", "context": ctx})
    with mock.patch.object(orch_mod.privacy, "should_redact", return_value=False):
        asyncio.run(orch._handle_user_message(event, "m1"))
    assert "Mail" in prompts[0] and "the quarterly numbers" in prompts[0]
    with mock.patch.object(orch_mod.privacy, "should_redact", return_value=True):
        asyncio.run(orch._handle_user_message(event, "m1"))
    assert "the quarterly numbers" not in prompts[1] and "kept on this Mac" in prompts[1]


def test_ws_user_message_carries_context(client) -> None:  # noqa: F811
    from auth import auth_header

    bus = client.app.state.event_bus
    pushed: list[ContextEvent] = []

    async def push(event: ContextEvent) -> None:
        pushed.append(event)

    with mock.patch.object(bus, "push", push):
        with client.websocket_connect("/ws/stream", headers=auth_header()) as ws:
            ws.receive_text()  # hello status
            ws.send_json({"type": "user_message", "seq": 1, "timestamp": "2026-11-10T00:00:00Z",
                          "payload": {"message_id": "m1", "text": "hi",
                                      "context": {"front_app": "Mail"}}})
            ws.send_json({"type": "user_message", "seq": 2, "timestamp": "2026-11-10T00:00:00Z",
                          "payload": {"message_id": "m2", "text": "hi"}})
            for _ in range(100):
                if len(pushed) >= 2:
                    break
                asyncio.run(asyncio.sleep(0.02))
    assert pushed[0].metadata["context"] == {"front_app": "Mail"}
    assert pushed[1].metadata["context"] is None


# ---- #79 adjustments in the judged sources ------------------------------------


class _Bumped:
    def judge_threshold(self, source: str, base: int, kind: Any = None) -> int:
        return base + 2

    def cooldown_multiplier(self, source: str, kind: Any = None) -> float:
        return 4.0


def test_judged_sources_use_outcome_adjustments(client) -> None:  # noqa: F811
    from ai.friction import FrictionDetector
    from ai.open_help import OpenHelpSuggester

    for cls in (OpenHelpSuggester, FrictionDetector):
        src = cls.__new__(cls)
        assert src._adjusted(7, 100.0) == (7, 100.0)  # no store: unchanged
        src.outcomes = _Bumped()
        assert src._adjusted(7, 100.0) == (9, 400.0)
    sources = client.app.state.orchestrator.idea_sources
    friction = next(s for s in sources if isinstance(s, FrictionDetector))
    assert friction.outcomes is client.app.state.idea_outcomes
