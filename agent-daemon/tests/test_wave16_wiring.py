"""Wave 16 close: hooks the runner added (#78 #84 #85 #86 #89)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import delivery as dl
from ai import demonstration as demo_mod
from ai import orchestrator as orch_mod
from ai import prepared_offers
from ai import standing_rules as sr
from ai import taint
from ai.action_journal import ActionJournal
from ai.idea_outcomes import DecisionLog
from ai.orchestrator import Orchestrator
from events import meeting_notes as MN
from events import remote_channel as rc
from events.event_bus import ContextEvent
from schemas.ws_messages import (
    ApprovalRequest,
    ApprovalResolved,
    CommandPayload,
    Notice,
    NoticePayload,
)
from tests.test_integration import client  # noqa: F401  - pytest fixture
from tests.test_wave13_wiring import _approval_orch

ROOT = Path(__file__).resolve().parents[2]
SWIFT = ROOT / "MenuBarUI" / "MenuBarUI"
FIXTURE = Path(__file__).parent / "fixtures" / "meeting_notes_transcript.txt"


# ---- harness -------------------------------------------------------------------------


def _orch(**extra: Any) -> Orchestrator:
    """A real Orchestrator (no __init__) whose cards are answered "approve"."""
    orch = _approval_orch()
    orch.event_bus = SimpleNamespace(loop=None)  # notices are kept, not broadcast
    orch._notices = {}
    orch._turn_tool_calls = set()
    orch._turn_done = {}
    orch._dynamic_tool_names = set()
    orch.mcp_manager = None
    orch.memory = None
    orch.pattern_recognizer = None
    orch._taint = taint.TurnTaint()
    orch.action_journal = None
    orch.tool_health = None
    orch._rerun_tool_name = None
    orch.delivery = None
    orch.demonstration = None
    orch.remote_channel = None
    orch.standing_rules = None
    orch.watch_lookup = None
    orch._current_event_meta = {}
    orch.approve = True  # type: ignore[attr-defined]
    seen: list[str] = []
    orch.seen = seen  # type: ignore[attr-defined]

    async def send(msg: Any) -> None:
        orch.sent.append(msg)  # type: ignore[attr-defined]
        if isinstance(msg, ApprovalRequest) and orch.approve is not None:  # type: ignore[attr-defined]
            rid = msg.payload.request_id
            from schemas.ws_messages import ApprovalResponsePayload

            def answer() -> None:
                fut = orch._pending.pop(rid, None)
                if fut is not None and not fut.done():
                    fut.set_result(ApprovalResponsePayload(request_id=rid, approved=orch.approve))  # type: ignore[attr-defined]

            asyncio.get_running_loop().call_soon(answer)

    async def emit(_eid: str, stage: str, content: str) -> None:
        seen.append(f"thought:{stage}")

    async def run_agent(*_a: Any, **_k: Any) -> str:
        seen.append("agent")
        return "answer"

    orch._send = send  # type: ignore[method-assign]
    orch._emit_thought = emit  # type: ignore[method-assign]
    orch._run_agent = run_agent  # type: ignore[method-assign]
    orch._recall_memories = mock.AsyncMock(return_value=[])  # type: ignore[method-assign]
    for k, v in extra.items():
        setattr(orch, k, v)
    return orch


def _cards(orch: Orchestrator) -> list[Any]:
    return [m.payload for m in orch.sent if isinstance(m, ApprovalRequest)]  # type: ignore[attr-defined]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Documents" / "Invoices").mkdir(parents=True)
    return tmp_path


def _pdf(home: Path, name: str) -> Path:
    p = home / "Downloads" / name
    p.write_bytes(b"%PDF-1.4 test")
    return p


def _file_event(path: Path) -> ContextEvent:
    return ContextEvent(event_type="file", metadata={"path": str(path), "action": "created"})


async def _approve_moves(orch: Orchestrator, home: Path, n: int) -> None:
    """``n`` approved, unedited move_file cards for new PDFs in Downloads."""
    dst = str(home / "Documents" / "Invoices")
    for i in range(n):
        src = _pdf(home, f"inv{i}.pdf")
        orch._current_event_type = "file"
        orch._current_event_meta = {"path": str(src), "action": "created"}
        orch._current_idea = None
        orch._turn_kind = "event"
        ok, _ = await orch._request_approval(
            "evt_x", "move_file", {"args": [], "kwargs": {"src": str(src), "dst_dir": dst}}, "file it",
        )
        assert ok


def _rules_orch(home: Path) -> tuple[Orchestrator, sr.StandingRules, ActionJournal, DecisionLog]:
    log, journal = DecisionLog(None), ActionJournal(None)
    store = sr.StandingRules(None, classify=lambda e: None)
    journal.add_undo_listener(store.on_undo)
    orch = _orch(standing_rules=store, action_journal=journal)
    orch.approval_decisions = log
    return orch, store, journal, log


# ---- #78 standing rules ---------------------------------------------------------------


def test_five_clean_approvals_offer_then_rule_runs_without_a_card_and_is_journaled(home) -> None:
    orch, store, journal, log = _rules_orch(home)
    asyncio.run(_approve_moves(orch, home, 5))
    # #79 recording kept (with the rule's argument pattern), #72 unchanged.
    rows = [d for d in log.decisions if d["tool_name"] == "move_file"]
    assert len(rows) == 5 and all(d.get("arg_pattern") for d in rows)
    [offer] = store.pending_offers()
    assert f"rule_offer:{offer['id']}" in orch._notices
    assert store.list_rules() == []  # an offer is never a rule
    rule = store.accept(offer["id"], log)
    cards_before = len(_cards(orch))
    src = _pdf(home, "new.pdf")
    with mock.patch("tools.show_notification.show_notification") as notify, \
            mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(_file_event(src)))
    assert len(_cards(orch)) == cards_before  # no card
    assert not src.exists() and (home / "Documents" / "Invoices" / "new.pdf").exists()
    [entry] = [r for r in journal.rows() if r.get("source") == "rule"]
    assert entry["rule_id"] == rule["id"] and entry["can_undo"]
    note = orch._notices[f"rule_run:{entry['id']}"]
    assert note.title == "Done automatically"
    assert note.action is not None and note.action.kind == "undo" and note.action.entry_id == entry["id"]
    assert notify.call_count == 1 and "agent" not in orch.seen  # type: ignore[attr-defined]


def test_edited_approval_raises_no_offer(home) -> None:
    orch, store, _journal, log = _rules_orch(home)
    asyncio.run(_approve_moves(orch, home, 4))
    src = _pdf(home, "edited.pdf")
    orch._current_event_meta = {"path": str(src), "action": "created"}
    from schemas.ws_messages import ApprovalResponsePayload

    async def scenario() -> None:
        orch.approve = None  # type: ignore[attr-defined]  # answer by hand

        async def answer() -> None:
            while not orch._pending:
                await asyncio.sleep(0.01)
            rid, fut = next(iter(orch._pending.items()))
            fut.set_result(ApprovalResponsePayload(
                request_id=rid, approved=True,
                edited_args={"kwargs": {"src": str(src), "dst_dir": str(home / "Documents")}}))

        asyncio.get_running_loop().create_task(answer())
        await orch._request_approval("evt", "move_file", {"args": [], "kwargs": {
            "src": str(src), "dst_dir": str(home / "Documents" / "Invoices")}}, "r")

    asyncio.run(scenario())
    assert store.pending_offers() == []
    assert log.decisions[-1]["decision"] == "edited"


def test_paused_event_never_reaches_a_rule(home) -> None:
    orch, store, journal, log = _rules_orch(home)
    asyncio.run(_approve_moves(orch, home, 5))
    store.accept(store.pending_offers()[0]["id"], log)
    delivery = dl.Delivery(None, settings=dl.DeliverySettings(None),
                           gate=dl.BusyGate(calendar=lambda n: [], processes=set,
                                            seconds_since_key=lambda: 1e9))
    delivery.pause("1h")
    orch.delivery = delivery
    src = _pdf(home, "paused.pdf")
    with mock.patch.object(store, "run_for_event", wraps=store.run_for_event) as run, \
            mock.patch("tools.show_notification.show_notification"):
        asyncio.run(orch._handle_event(_file_event(src)))
    assert not run.called and src.exists()
    assert [r for r in journal.rows() if r.get("source") == "rule"] == []
    # The store's own Pause check (main.py's lambda) holds too.
    paused_store = sr.StandingRules(None, paused=lambda: True)
    assert paused_store.run_for_event(_file_event(src)) == []


def test_a_failed_rule_run_falls_back_to_the_normal_path(home) -> None:
    orch, store, _journal, _log = _rules_orch(home)
    with mock.patch.object(store, "run_for_event", return_value=[
            {"rule_id": "r", "tool": "move_file", "result": {"status": "error"}, "entry": None}]), \
            mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(_file_event(home / "Downloads" / "x.pdf")))
    assert "agent" in orch.seen  # type: ignore[attr-defined]


def test_rules_wired_in_main(client) -> None:  # noqa: F811
    state = client.app.state
    assert isinstance(state.standing_rules, sr.StandingRules)
    assert state.orchestrator.standing_rules is state.standing_rules
    assert state.orchestrator.watch_lookup == state.watches.get
    assert state.standing_rules.on_undo in state.action_journal._listeners
    assert state.standing_rules.path == Path(os.environ["AGENT_STANDING_RULES_PATH"])
    assert client.get("/api/rules").status_code == 200
    assert client.post("/api/rules/runs/nope/undo").status_code == 404
    state.delivery.pause("1h")
    try:
        assert state.standing_rules.paused() is True
    finally:
        state.delivery.resume()
    assert state.standing_rules.paused() is False


def test_undo_notice_action_wire_shape() -> None:
    plain = NoticePayload(id="n", title="t", body="b",
                          action={"kind": "open_settings", "url": "x-apple.systempreferences:x"})
    assert "entry_id" not in plain.model_dump()["action"]
    undo = NoticePayload(id="n2", title="t", body="b", action={"kind": "undo", "entry_id": "act_1"})
    assert undo.model_dump()["action"] == {"kind": "undo", "url": "", "entry_id": "act_1"}


# ---- #84 watch me ----------------------------------------------------------------------


def test_recording_swallows_a_file_event_before_delivery_and_rules(home) -> None:
    published: list[ContextEvent] = []

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    demo = demo_mod.DemonstrationController(publish=publish, enrich=None)
    rules = mock.Mock(run_for_event=mock.Mock(return_value=[]))
    delivery = mock.Mock(decide=mock.Mock(return_value=None))
    orch = _orch(demonstration=demo, standing_rules=rules, delivery=delivery)

    async def scenario() -> None:
        await demo.start()
        assert demo.recording
        with mock.patch.object(orch_mod.activity_store, "record") as record:
            await orch._handle_event(_file_event(home / "Downloads" / "a.pdf"))
            await orch._handle_event(ContextEvent(event_type="mail_received", metadata={"message_id": "m1"}))
        assert not record.called
        await demo.cancel()

    asyncio.run(scenario())
    assert not rules.run_for_event.called and not delivery.decide.called
    assert orch.seen == []  # type: ignore[attr-defined]  # no thought, no turn
    assert [e.event_type for e in published] == ["mail_received"]  # released on cancel


def test_demonstration_idea_skips_learned_refusal() -> None:
    orch = _orch()
    orch._learned_refusal = mock.Mock(return_value={"x": 1})  # type: ignore[method-assign]
    idea = {"tool_name": "file_invoices", "source": demo_mod.SOURCE, "description": "d",
            "expected_logic": "e", "builtin_tool": "move_file", "trigger": "demonstration"}
    with mock.patch("tools.meta_tool_generator.set_current_idea"):
        asyncio.run(orch._handle_event(ContextEvent(event_type="pattern_detected", metadata=idea)))
    assert not orch._learned_refusal.called and "agent" in orch.seen  # type: ignore[attr-defined]


def test_demonstration_wired_and_commands_drive_it(client) -> None:  # noqa: F811
    import main
    from schemas.ws_messages import Command

    state = client.app.state
    assert state.orchestrator.demonstration is state.demonstration
    assert orch_mod._PATTERN_SOURCE_INTROS["demonstration"] == demo_mod.PATTERN_SOURCE_INTRO

    def cmd(action: str) -> None:
        asyncio.run(main._handle_command(client.app, Command(
            seq=1, timestamp="t", payload=CommandPayload(action=action))))

    cmd("demonstration_start")
    assert state.demonstration.recording
    cmd("demonstration_stop")
    assert not state.demonstration.recording and state.demonstration.recorder.state == demo_mod.STOPPED
    cmd("demonstration_cancel")
    assert state.demonstration.recorder.state == demo_mod.IDLE


# ---- #85 iPhone remote --------------------------------------------------------------------


@pytest.fixture()
def remote_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rc.ENABLED_KEY, "1")
    monkeypatch.setenv(rc.HANDLE_KEY, "+15555550100")


def test_phone_code_reply_resolves_the_real_future(remote_on) -> None:
    sent: list[tuple[str, str]] = []
    remote = rc.RemoteChannel(None, db_path="/nonexistent/chat.db",
                              sender=lambda h, b: sent.append((h, b)) or True,
                              idle_seconds=lambda: 1e9,
                              egress=SimpleNamespace(record=lambda *a, **k: True))
    orch = _orch(remote_channel=remote)
    orch.approve = None  # type: ignore[attr-defined]  # only the phone answers

    async def scenario() -> tuple[bool, Any]:
        remote.attach_orchestrator(orch, asyncio.get_running_loop())
        task = asyncio.create_task(orch._request_approval(
            "evt", "create_reminder", {"args": [], "kwargs": {"title": "Pay rent"}}, "deadline"))
        async def mirrored() -> None:
            while not remote.pending_codes():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(mirrored(), 3)
        [code] = remote.pending_codes()
        await asyncio.to_thread(remote._handle_reply, True, code)
        return await asyncio.wait_for(task, 5)

    approved, _ = asyncio.run(scenario())
    assert approved is True
    assert all(h == "+15555550100" for h, _ in sent)
    assert "Approved" in sent[-1][1]
    [card] = _cards(orch)
    assert card.timeout_seconds == int(rc.REMOTE_TIMEOUT_S)
    [resolved] = [m.payload for m in orch.sent if isinstance(m, ApprovalResolved)]  # type: ignore[attr-defined]
    assert resolved.request_id == card.request_id and resolved.outcome == "approved"
    assert resolved.note == "answered from your phone"
    assert remote.pending_codes() == {}  # the code died with the request


def test_never_mirrored_cards_keep_the_mac_timeout(remote_on) -> None:
    sent: list[Any] = []
    remote = rc.RemoteChannel(None, sender=lambda h, b: sent.append(b) or True,
                              idle_seconds=lambda: 1e9,
                              egress=SimpleNamespace(record=lambda *a, **k: True))
    orch = _orch(remote_channel=remote)
    orch.approval_timeout = 0.05
    orch.approve = None  # type: ignore[attr-defined]

    async def scenario() -> None:
        await orch._request_approval("evt", "run_shortcut", {"kwargs": {"name": "x"}}, "r")
        await orch._request_approval("evt", "create_reminder", {"kwargs": {"title": "t"}}, "r",
                                     origin_notes=["Read this turn: email from x"])

    asyncio.run(scenario())
    assert sent == [] and all(c.timeout_seconds == 0 for c in _cards(orch))
    outcomes = [m.payload.outcome for m in orch.sent if isinstance(m, ApprovalResolved)]  # type: ignore[attr-defined]
    assert outcomes == ["timed_out", "timed_out"]


def test_gated_forward_outlives_the_remote_timeout() -> None:
    from tests.test_wave13_wiring import _Loop

    waited: list[float] = []

    class Fut:
        def result(self, timeout: float = 0.0) -> tuple[bool, None]:
            waited.append(timeout)
            return False, None

    with _Loop() as loop:
        orch = _orch(remote_channel=SimpleNamespace(timeout_s=900.0))
        orch.event_bus = SimpleNamespace(loop=loop)
        orch._current_event_id = "evt"
        tool = SimpleNamespace(name="create_reminder", forward=lambda **k: "ok", inputs={})
        wrapped = Orchestrator._wrap_tool_for_approval(orch, tool)

        def fake_rcts(coro: Any, _loop: Any) -> Fut:
            coro.close()
            return Fut()

        with mock.patch.object(orch_mod.asyncio, "run_coroutine_threadsafe", side_effect=fake_rcts):
            wrapped.forward(title="x")
    assert waited == [915.0]


def test_phone_turn_is_answered_in_the_thread_and_a_mac_turn_is_not() -> None:
    remote = mock.Mock()
    orch = _orch(remote_channel=remote)
    with mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(ContextEvent(event_type="user_message", metadata={
            "event_id": "remote_7", "text": "what's on tomorrow?", "source": rc.REMOTE_SOURCE})))
        assert orch._turn_from_phone is False  # reset after the turn
        asyncio.run(orch._handle_event(ContextEvent(event_type="user_message", metadata={
            "event_id": "msg_1", "text": "hi"})))
    remote.send_reply.assert_called_once_with("remote_7", "answer")


def test_remote_wired_and_settings(client) -> None:  # noqa: F811
    import user_settings
    from ai import egress_log, permissions

    state = client.app.state
    assert isinstance(state.orchestrator.remote_channel, rc.RemoteChannel)
    assert state.remote_channel is state.orchestrator.remote_channel
    assert client.get("/api/remote").json()["enabled"] is False  # off by default (7c)
    for key in ("AGENT_REMOTE", "AGENT_REMOTE_HANDLE", "AGENT_REMOTE_IDLE_MINUTES"):
        assert key in user_settings.ALLOWED_KEYS
    assert rc.KIND_IMESSAGE == egress_log.KIND_IMESSAGE and egress_log.KIND_IMESSAGE in egress_log.KINDS
    assert permissions.get("automation_messages").used_by == ("events/remote_channel.py",)
    import tools
    assert "send_imessage" not in {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}


def test_ws_user_message_never_carries_a_source(client) -> None:  # noqa: F811
    from auth import auth_header

    pushed: list[ContextEvent] = []
    bus = client.app.state.event_bus
    with mock.patch.object(bus, "push", new=mock.AsyncMock(side_effect=lambda e: pushed.append(e))):
        with client.websocket_connect("/ws/stream", headers=auth_header()) as ws:
            ws.receive_text()
            ws.send_text('{"type":"user_message","seq":1,"timestamp":"t","payload":'
                         '{"message_id":"m1","text":"hi","source":"remote"}}')
            _until(lambda: pushed)
    assert pushed and "source" not in pushed[0].metadata


# ---- #89 front window -------------------------------------------------------------------


def _until(cond: Any, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_read_front_window_registered_read_only_and_unskipped() -> None:
    import tools
    from tools import read_front_window as rfw

    assert hasattr(rfw, "bridge")  # the package attribute stays the module
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    assert "read_front_window" in names and "read_front_window" in tools.READ_ONLY_TOOL_NAMES
    tool = next(t for t in tools.AVAILABLE_TOOLS if getattr(t, "name", "") == "read_front_window")
    stub = SimpleNamespace(name=tool.name, forward=lambda **k: {"ok": True, "text": "x"}, inputs={})
    from tests.test_wave13_wiring import _Loop

    with _Loop() as loop:
        orch = _orch()
        orch.event_bus = SimpleNamespace(loop=loop)
        orch._current_event_id = "evt"
        orch._taint.begin_turn("chat", user_text="summarize this")
        Orchestrator._wrap_tool_for_approval(orch, stub).forward(max_chars=100)
    assert _cards(orch) == []
    assert orch._taint.is_tainted()  # the window's text marks the turn (#81)


def test_quick_ask_window_context_is_read_by_the_daemon_and_marks_the_turn() -> None:
    from tools import read_front_window as rfw

    orch = _orch()
    prompts: list[str] = []

    async def run_agent(_eid: str, prompt: str, **_k: Any) -> str:
        prompts.append(prompt)
        return "summary"

    orch._run_agent = run_agent  # type: ignore[method-assign]
    orch._build_chat_prompt = lambda event, memories=None: "PROMPT"  # type: ignore[method-assign]

    def fake(ctx: dict) -> dict:
        return {**ctx, "selection": "Quarterly plan body", "window_title": "Plan.pages"}

    with mock.patch.object(rfw, "context_with_window_text", side_effect=fake) as read, \
            mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(ContextEvent(event_type="user_message", metadata={
            "event_id": "m", "text": "summarize what I'm looking at",
            "context": {"front_app": "Pages", "selection_source": "window"}})))
    assert read.called and "Quarterly plan body" in prompts[0]
    assert "text of the window in front" in prompts[0]
    assert orch._taint.is_tainted() and orch._taint._reads[0].kind == "window"


def test_screen_text_ws_routing(client) -> None:  # noqa: F811
    from auth import auth_header
    from tools import read_front_window as rfw

    with client.websocket_connect("/ws/stream", headers=auth_header()) as ws:
        ws.receive_text()
        ws.send_text('{"type":"screen_text_status","seq":1,"timestamp":"t",'
                     '"payload":{"accessibility_trusted":true}}')
        _until(lambda: rfw.bridge.accessibility_trusted() is True)
        assert rfw.bridge.connected()
    _until(lambda: not rfw.bridge.connected())


# ---- #86 meeting notes --------------------------------------------------------------------


def test_meeting_audio_ready_gives_one_bundle_card_and_deletes_the_audio(monkeypatch, tmp_path) -> None:
    import main

    monkeypatch.setattr(MN.Path, "home", classmethod(lambda cls: tmp_path))
    published: list[ContextEvent] = []

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    notes = MN.MeetingNotes(publish=publish, enabled=lambda: True, evaluators=[],
                            transcriber=lambda path: FIXTURE.read_text("utf-8"), full_name="")
    audio = MN.audio_dir() / "m1.m4a"
    audio.write_bytes(b"\0" * 32)
    with mock.patch("tools.show_notification.show_notification") as notify:
        out = asyncio.run(main._process_meeting_audio(notes, {
            "meeting_id": "m1", "title": "Design sync", "audio_path": str(audio)}))
    assert out["status"] == "offered" and not audio.exists() and not notify.called
    [event] = published
    orch = _orch()
    orch.approve = False  # type: ignore[attr-defined]  # deny: nothing runs
    ran: list[Any] = []
    with mock.patch.object(prepared_offers, "run_approved_actions", side_effect=lambda *a, **k: ran.append(1)), \
            mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(event))
    [card] = _cards(orch)
    assert card.actions and len(card.actions) >= 2 and card.tool_name == "prepared_offer"
    assert ran == []
    # A failed recording is told as a notification, and its audio still goes.
    audio.write_bytes(b"\0")
    with mock.patch("tools.show_notification.show_notification") as notify:
        out = asyncio.run(main._process_meeting_audio(notes, {
            "meeting_id": "m2", "audio_path": str(audio), "error": "mic lost"}))
    assert out["status"] == "error" and not audio.exists() and notify.call_count == 1


def test_meeting_frames_ws_routing(client) -> None:  # noqa: F811
    from auth import auth_header

    state = client.app.state
    assert isinstance(state.meeting_notes, MN.MeetingNotes)
    assert state.meeting_notes in state.orchestrator.idea_sources
    assert client.get("/api/meeting-notes").json()["enabled"] is False
    assert client.put("/api/meeting-notes", json={"engine": "nope"}).status_code == 400
    calls: list[str] = []

    async def process(payload: dict) -> dict:
        calls.append("audio:" + payload["meeting_id"])
        return {"status": "offered"}

    fake = SimpleNamespace(
        link=MN.RecorderLink(),
        on_record_state=lambda conn, p: calls.append("state:" + p["state"]),
        process=process,
    )
    real = state.meeting_notes
    state.meeting_notes = fake
    try:
        with client.websocket_connect("/ws/stream", headers=auth_header()) as ws:
            ws.receive_text()
            _until(lambda: bool(fake.link._conns))
            ws.send_text('{"type":"meeting_record_state","seq":1,"timestamp":"t","payload":'
                         '{"state":"idle","microphone":"authorized","speech_recognition":"denied"}}')
            ws.send_text('{"type":"meeting_audio_ready","seq":2,"timestamp":"t","payload":'
                         '{"meeting_id":"m9","transcript_path":"/x"}}')
            _until(lambda: len(calls) == 2)
        _until(lambda: not fake.link._conns)
    finally:
        state.meeting_notes = real
    assert calls == ["state:idle", "audio:m9"]


def test_meeting_settings_keys_and_optional_whisper() -> None:
    import user_settings

    assert {"AGENT_MEETING_NOTES", "AGENT_MEETING_NOTES_ENGINE"} <= user_settings.ALLOWED_KEYS
    req = (ROOT / "agent-daemon" / "requirements.txt").read_text("utf-8")
    assert "# mlx-whisper>=0.4" in req
    assert not any(line.strip().startswith("mlx-whisper") for line in req.splitlines())


# ---- schema, conftest, Swift, LIVING_SPEC ---------------------------------------------------


def test_new_frames_roundtrip() -> None:
    from pydantic import TypeAdapter

    from schemas.ws_messages import WSMessage

    adapter = TypeAdapter(WSMessage)
    for frame in (
        {"type": "approval_resolved", "payload": {"request_id": "r", "outcome": "declined"}},
        {"type": "demonstration_trace", "payload": {"state": "recording", "steps": [
            {"id": "s1", "kind": "file", "text": "Saved a.pdf"}]}},
        {"type": "demonstration_draft", "payload": {"sentence": "File invoices"}},
        {"type": "meeting_record_offer", "payload": {"meeting_id": "m", "title": "T", "stop_at": "x"}},
        {"type": "meeting_record_stop", "payload": {"meeting_id": "m"}},
        {"type": "meeting_record_state", "payload": {"state": "started", "microphone": "authorized",
                                                     "speech_recognition": "authorized"}},
        {"type": "meeting_audio_ready", "payload": {"meeting_id": "m"}},
    ):
        msg = adapter.validate_python({"seq": 1, "timestamp": "t", **frame})
        assert msg.type == frame["type"]
    for action in ("demonstration_start", "demonstration_stop", "demonstration_cancel"):
        CommandPayload(action=action)
    assert isinstance(Notice(seq=1, timestamp="t", payload=NoticePayload(id="i", title="t", body="b")), Notice)


def test_conftest_isolates_the_rules_store() -> None:
    path = os.environ.get("AGENT_STANDING_RULES_PATH", "")
    assert path and not path.startswith(str(Path.home() / ".bot-inator"))
    assert str(sr.default_path()) == path


def _swift(name: str) -> str:
    return next(SWIFT.rglob(name)).read_text(encoding="utf-8")


def test_swift_embeds_wave16() -> None:
    settings = _swift("SettingsView.swift")
    for view in ("RulesView()", "RemoteSettingsView()", "MeetingNotesSettingsView()", "RecordShortcut"):
        assert view in settings, view
    app = _swift("MenuBarUIApp.swift")
    for part in ("DemonstrationPanelController(", "RecordHotKeyController.shared.start",
                 "Watch me…", "record.circle.fill", "MeetingRecorderMenuItems()",
                 ".demonstrationTrace"):
        assert part in app, part
    ws = _swift("WebSocketManager.swift")
    for part in ("sendDemonstrationDraft", "MeetingRecorder.shared.receiveOffer",
                 "MeetingRecorder.shared.receiveStop", "reportIdle()", ".approvalResolved",
                 ".meetingAudioReady(", ".meetingRecordState("):
        assert part in ws, part
    msg = _swift("WSMessage.swift")
    for key in ('"approval_resolved"', '"demonstration_trace"', '"demonstration_draft"',
                '"meeting_record_offer"', '"meeting_record_stop"', '"meeting_record_state"',
                '"meeting_audio_ready"', '"demonstration_start"', '"entry_id"'):
        assert key in msg, key
    assert "struct DemonstrationTracePayload" not in msg and "struct MeetingRecordOfferPayload" not in msg
    assert 'action.kind == "undo"' in _swift("NoticeBanner.swift")
    assert "includeWindowText" in _swift("QuickAskPanel.swift")
    assert '"imessage"' in _swift("PrivacyLedgerView.swift")


def test_living_spec_has_wave16_rows() -> None:
    spec = " ".join((ROOT / "LIVING_SPEC.md").read_text(encoding="utf-8").split())
    assert "Until #78" not in spec and "Built (#78" in spec
    for needle in ("/api/rules", "standing_rules.json", "AGENT_STANDING_RULES_PATH",
                   "/api/remote", "AGENT_REMOTE", "approval_resolved", "kind `imessage`",
                   "yes <code>", "/api/meeting-notes", "meeting_record_offer", "meeting_audio_ready",
                   "demonstration_trace", "demonstration_draft", "screen_text_request",
                   "`read_front_window`|", "never goes to a cloud engine"):
        assert needle in spec, needle
