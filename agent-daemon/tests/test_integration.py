"""End-to-end integration smoke tests for the agent daemon.

These exercise the FastAPI app with the full wiring in ``main.py``:
the event bus, orchestrator, and WebSocket protocol. The MLX-backed
smolagents agent is forced into fallback mode by stubbing
``Orchestrator._build_agent`` so no model is required.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before importing main so lifespan skips OS listeners and
# does not try to eager-load multi-GB MLX models during the test run.
os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from events.event_bus import ContextEvent  # noqa: E402


@pytest.fixture()
def client():
    with patch(
        "ai.orchestrator.Orchestrator._build_agent", return_value=None
    ), patch(
        "ai.mlx_engine.MLXEngine.evaluate_event",
        new_callable=AsyncMock,
        return_value="",
    ):
        from main import app  # noqa: WPS433 - import after env/patch set up

        with TestClient(app) as test_client:
            yield test_client


def test_health_returns_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_ws_connect_receives_status(client) -> None:
    with client.websocket_connect("/ws/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "status"
    assert msg["payload"]["state"] == "ready"


def test_context_event_triggers_thought(client) -> None:
    bus = client.app.state.event_bus
    with client.websocket_connect("/ws/stream") as ws:
        # Drain the initial status frame.
        status = ws.receive_json()
        assert status["type"] == "status"

        event = ContextEvent(
            event_type="app_activated",
            app_name="Google Chrome",
            window_title="Jira",
        )
        bus.push_threadsafe(event)

        first = ws.receive_json()
        assert first["type"] == "thought"
        assert first["payload"]["stage"] == "event_received"
        assert first["payload"]["event_id"]


def test_analysis_engine_is_distinct_from_main_engine(client) -> None:
    main_engine = client.app.state.engine
    analysis_engine = client.app.state.analysis_engine
    orchestrator = client.app.state.orchestrator
    assert analysis_engine is not main_engine
    assert orchestrator.analysis_engine is analysis_engine
    assert orchestrator.engine is main_engine
    assert analysis_engine.model_name != main_engine.model_name


def test_evaluate_event_emits_analysis_thought(client) -> None:
    bus = client.app.state.event_bus
    analysis_engine = client.app.state.analysis_engine
    analysis_engine.evaluate_event = AsyncMock(
        return_value="User appears to be reviewing a Jira ticket."
    )

    with client.websocket_connect("/ws/stream") as ws:
        status = ws.receive_json()
        assert status["type"] == "status"

        bus.push_threadsafe(
            ContextEvent(
                event_type="app_activated",
                app_name="Google Chrome",
                window_title="Jira - BUG-123",
            )
        )

        saw_event_received = False
        saw_analysis_content: str | None = None
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] != "thought":
                continue
            stage = msg["payload"]["stage"]
            if stage == "event_received":
                saw_event_received = True
            elif stage == "analysis":
                saw_analysis_content = msg["payload"]["content"]
                break

        assert saw_event_received, "expected an 'event_received' thought first"
        assert saw_analysis_content == (
            "User appears to be reviewing a Jira ticket."
        ), "expected the analysis thought to carry evaluate_event output"

        analysis_engine.evaluate_event.assert_awaited_once()
        call_args = analysis_engine.evaluate_event.await_args
        assert "Google Chrome" in call_args.args[0]


def test_empty_evaluate_event_skips_analysis_thought(client) -> None:
    """When evaluate_event returns '', no analysis thought should be emitted."""
    bus = client.app.state.event_bus
    with client.websocket_connect("/ws/stream") as ws:
        status = ws.receive_json()
        assert status["type"] == "status"

        bus.push_threadsafe(
            ContextEvent(
                event_type="app_activated",
                app_name="Safari",
                window_title="Docs",
            )
        )

        stages: list[str] = []
        for _ in range(6):
            msg = ws.receive_json()
            if msg["type"] != "thought":
                continue
            stages.append(msg["payload"]["stage"])
            if msg["payload"]["stage"] == "reasoning":
                break

        assert "event_received" in stages
        assert "analysis" not in stages
        assert "reasoning" in stages


def test_approval_request_and_response_roundtrip(client) -> None:
    bus = client.app.state.event_bus
    orchestrator = client.app.state.orchestrator

    def _stub_tool() -> str:
        return "stub-ok"

    original_tools = orchestrator.tools
    orchestrator.tools = [_stub_tool]
    try:
        _roundtrip_body(client, bus)
    finally:
        orchestrator.tools = original_tools


def test_apply_edits_helper() -> None:
    """``_apply_edits`` merges edited args with sensible fall-throughs."""
    from ai.orchestrator import Orchestrator

    # No edits -> originals are returned as fresh copies.
    call_args, call_kwargs = Orchestrator._apply_edits(None, ["a"], {"k": 1})
    assert call_args == ["a"] and call_kwargs == {"k": 1}

    # Edited kwargs override; args fall through to the default.
    edited = {"kwargs": {"message": "new"}}
    call_args, call_kwargs = Orchestrator._apply_edits(edited, [], {"message": "orig"})
    assert call_args == []
    assert call_kwargs == {"message": "new"}

    # Both edited.
    edited = {"args": ["x"], "kwargs": {"y": 2}}
    call_args, call_kwargs = Orchestrator._apply_edits(edited, ["a"], {"y": 1})
    assert call_args == ["x"]
    assert call_kwargs == {"y": 2}


def test_approval_response_with_edited_args_overrides_tool_invocation(client) -> None:
    """Editing a tool arg in the approval UI must change the actual call."""
    bus = client.app.state.event_bus
    orchestrator = client.app.state.orchestrator

    captured: dict[str, object] = {}

    def stub_send(target_number: str, message: str) -> str:
        captured["target_number"] = target_number
        captured["message"] = message
        return "sent"

    stub_send.name = "send_imessage"  # type: ignore[attr-defined]

    original_tools = orchestrator.tools
    orchestrator.tools = [stub_send]
    try:
        with client.websocket_connect("/ws/stream") as ws:
            status = ws.receive_json()
            assert status["type"] == "status"

            bus.push_threadsafe(
                ContextEvent(
                    event_type="imessage_received",
                    metadata={"sender": "+14155551212", "text": "yo"},
                )
            )

            approval_req = None
            for _ in range(10):
                msg = ws.receive_json()
                if msg["type"] == "approval_request":
                    approval_req = msg
                    break
            assert approval_req is not None, "expected an approval_request frame"
            assert approval_req["payload"]["tool_name"] == "send_imessage"

            request_id = approval_req["payload"]["request_id"]
            ws.send_json(
                {
                    "type": "approval_response",
                    "seq": 1,
                    "timestamp": "2026-04-19T12:00:00.000Z",
                    "payload": {
                        "request_id": request_id,
                        "approved": True,
                        "edited_args": {
                            "args": [],
                            "kwargs": {
                                "target_number": "+14155550000",
                                "message": "edited reply",
                            },
                        },
                    },
                }
            )

            saw_complete = False
            for _ in range(15):
                msg = ws.receive_json()
                if msg["type"] == "thought" and msg["payload"]["stage"] == "complete":
                    saw_complete = True
                    break
            assert saw_complete, "expected the loop to close"

        assert captured == {
            "target_number": "+14155550000",
            "message": "edited reply",
        }
    finally:
        orchestrator.tools = original_tools


def test_imessage_event_builds_specialized_prompt() -> None:
    """An ``imessage_received`` event must steer the agent toward send_imessage."""
    from ai.orchestrator import Orchestrator

    event = ContextEvent(
        event_type="imessage_received",
        metadata={"sender": "+14155551212", "text": "are we still on for 7?"},
    )
    prompt = Orchestrator._build_imessage_prompt(event)
    assert "+14155551212" in prompt
    assert "are we still on for 7?" in prompt
    assert "send_imessage" in prompt
    assert "target_number='+14155551212'" in prompt
    assert "no action" in prompt

    description = Orchestrator._build_imessage_prompt.__self__ if False else None
    del description
    assert "iMessage from +14155551212" in Orchestrator._describe_event(event)


def test_imessage_watcher_emits_inbound_messages(tmp_path) -> None:
    """The watcher only emits events for inbound messages after its baseline."""
    import sqlite3
    import time

    from events.event_bus import EventBus
    from events.imessage_watcher import IMessageWatcher

    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            is_from_me INTEGER,
            handle_id INTEGER
        );
        INSERT INTO handle (ROWID, id) VALUES (1, '+14155551212');
        INSERT INTO message (text, is_from_me, handle_id)
            VALUES ('historical', 0, 1);
        """
    )
    conn.commit()

    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    try:
        bus = EventBus()
        bus.bind_loop(loop)
        watcher = IMessageWatcher(bus, db_path=db_path, poll_interval=0.05)
        watcher.start()
        time.sleep(0.15)

        conn.execute(
            "INSERT INTO message (text, is_from_me, handle_id) VALUES (?, 0, 1)",
            ("hello from a friend",),
        )
        conn.execute(
            "INSERT INTO message (text, is_from_me, handle_id) VALUES (?, 1, 1)",
            ("this is me typing — must be ignored",),
        )
        conn.commit()
        time.sleep(0.25)
        watcher.stop()

        events: list = []
        while True:
            try:
                events.append(loop.run_until_complete(
                    _asyncio.wait_for(bus.consume(), timeout=0.05)
                ))
            except _asyncio.TimeoutError:
                break

        texts = [e.metadata.get("text") for e in events]
        assert "hello from a friend" in texts
        assert "historical" not in texts
        assert "this is me typing — must be ignored" not in texts
        assert all(e.event_type == "imessage_received" for e in events)
        assert all(e.metadata.get("sender") == "+14155551212" for e in events)
    finally:
        conn.close()
        loop.close()


def test_read_only_tool_bypasses_approval(client) -> None:
    """Read-only tools must run without emitting an approval_request."""
    bus = client.app.state.event_bus
    orchestrator = client.app.state.orchestrator

    def read_clipboard() -> str:
        return "clipboard-content"

    read_clipboard.name = "read_clipboard"  # type: ignore[attr-defined]

    original_tools = orchestrator.tools
    orchestrator.tools = [read_clipboard]
    try:
        with client.websocket_connect("/ws/stream") as ws:
            status = ws.receive_json()
            assert status["type"] == "status"

            bus.push_threadsafe(
                ContextEvent(
                    event_type="file",
                    metadata={"path": "/tmp/report.pdf", "action": "created"},
                )
            )

            saw_tool_result = False
            saw_complete = False
            for _ in range(15):
                msg = ws.receive_json()
                assert msg["type"] != "approval_request", (
                    "read-only tool must not trigger an approval_request"
                )
                if msg["type"] != "thought":
                    continue
                stage = msg["payload"]["stage"]
                content = msg["payload"].get("content", "")
                if stage == "tool_result":
                    assert "read_clipboard" in content
                    assert "read-only" in content
                    assert "clipboard-content" in content
                    saw_tool_result = True
                elif stage == "complete":
                    saw_complete = True
                    break

            assert saw_tool_result, "expected a tool_result thought for read_clipboard"
            assert saw_complete, "expected a complete thought to close the loop"
    finally:
        orchestrator.tools = original_tools


def _roundtrip_body(client, bus) -> None:
    with client.websocket_connect("/ws/stream") as ws:
        status = ws.receive_json()
        assert status["type"] == "status"

        event = ContextEvent(
            event_type="file",
            metadata={"path": "/tmp/report.pdf", "action": "created"},
        )
        bus.push_threadsafe(event)

        approval_req = None
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "approval_request":
                approval_req = msg
                break
        assert approval_req is not None, "expected an approval_request frame"

        request_id = approval_req["payload"]["request_id"]
        ws.send_json(
            {
                "type": "approval_response",
                "seq": 1,
                "timestamp": "2026-04-17T12:00:00.000Z",
                "payload": {
                    "request_id": request_id,
                    "approved": True,
                },
            }
        )

        saw_tool_result = False
        saw_complete = False
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] != "thought":
                continue
            stage = msg["payload"]["stage"]
            if stage == "tool_result":
                saw_tool_result = True
            elif stage == "complete":
                saw_complete = True
                break

        assert saw_tool_result, "expected a tool_result thought after approval"
        assert saw_complete, "expected a complete thought to close the loop"


def test_memory_save_recall_round_trip(tmp_path) -> None:
    """Saved memories can be recalled by the instance API.

    Exact ordering depends on whether ``sqlite-vec`` is loadable in the
    running Python: when it is we rely on vector similarity, otherwise the
    store falls back to recency. The contract the orchestrator depends on
    is that ``recall_memory`` returns ``list[str]`` bounded by ``top_k``
    and includes previously-saved text.
    """
    from ai.memory import Memory, _fallback_embed

    mem = Memory(
        db_path=tmp_path / "memory.db",
        embedder=lambda t: _fallback_embed(t),
    )
    try:
        mem.save_memory("the user prefers dark mode")
        mem.save_memory("the user lives in Oakland")
        mem.save_memory("the user's dog is named Biscuit")

        one = mem.recall_memory("anything", top_k=1)
        assert len(one) == 1 and isinstance(one[0], str)

        all_three = mem.recall_memory("anything", top_k=3)
        assert len(all_three) == 3
        assert all(isinstance(x, str) for x in all_three)
        assert set(all_three) == {
            "the user prefers dark mode",
            "the user lives in Oakland",
            "the user's dog is named Biscuit",
        }
    finally:
        mem.close()


def test_remember_preference_tool_writes_to_default_memory(tmp_path) -> None:
    """The ``remember_preference`` tool persists via the module-level helper."""
    from ai.memory import Memory, _fallback_embed, set_default_memory
    from tools.remember_preference import remember_preference

    mem = Memory(
        db_path=tmp_path / "memory.db",
        embedder=lambda t: _fallback_embed(t),
    )
    set_default_memory(mem)
    try:
        underlying = getattr(remember_preference, "__wrapped__", remember_preference)
        result = underlying("the user prefers meetings after 2pm")
        assert result["status"] == "ok"
        assert isinstance(result["id"], int)

        empty = underlying("   ")
        assert empty["status"] == "error"

        recalled = mem.recall_memory("when does the user like meetings?", top_k=1)
        assert recalled and "meetings after 2pm" in recalled[0]
    finally:
        set_default_memory(None)
        mem.close()


def test_list_tools_includes_builtins_and_generated(client, tmp_path, monkeypatch) -> None:
    """GET /api/tools surfaces built-ins and stub generated modules."""
    from config import settings
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    (tmp_path / "cold_tool.py").write_text(
        '"""Module doc."""\n\n'
        'def cold_tool(x: str) -> dict:\n'
        '    """A cold tool that has not been loaded yet."""\n'
        '    return {"status": "ok"}\n'
    )

    response = client.get("/api/tools")
    assert response.status_code == 200
    rows = response.json()
    names = {r["name"]: r for r in rows}

    assert "show_notification" in names and names["show_notification"]["is_generated"] is False
    assert "generate_custom_tool" in names
    assert "cold_tool" in names and names["cold_tool"]["is_generated"] is True
    assert "cold tool" in names["cold_tool"]["description"].lower()


def test_delete_tool_rejects_builtins(client) -> None:
    assert client.delete("/api/tools/show_notification").status_code == 400
    assert client.delete("/api/tools/BadName").status_code == 400
    assert client.delete("/api/tools/nonexistent_tool").status_code == 404


def test_delete_tool_removes_file_and_unloads(client, tmp_path, monkeypatch) -> None:
    """DELETE removes the file, detaches the tool, and best-effort commits."""
    from config import settings
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    orchestrator = client.app.state.orchestrator
    path = tmp_path / "tempo_tool.py"
    path.write_text(
        '"""Temp."""\n\n'
        'from smolagents import tool\n\n'
        '@tool\n'
        'def tempo_tool() -> dict:\n'
        '    """Return a sentinel."""\n'
        '    return {"status": "ok"}\n'
    )
    orchestrator.load_dynamic_tools(tmp_path)
    assert "tempo_tool" in orchestrator._dynamic_tool_names

    response = client.delete("/api/tools/tempo_tool")
    assert response.status_code == 200
    assert response.json()["unloaded"] is True
    assert not path.exists()
    assert "tempo_tool" not in orchestrator._dynamic_tool_names


def test_load_dynamic_tools_skips_dotfiles(tmp_path) -> None:
    """`.gitkeep` and the nested `.git/` folder must never be imported."""
    import asyncio as _asyncio
    from unittest.mock import patch as _patch

    from ai.mlx_engine import MLXEngine
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus

    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "wibble.py").write_text(
        '"""Wibble."""\n'
        'from smolagents import tool\n\n'
        '@tool\n'
        'def wibble() -> dict:\n'
        '    """Return nothing."""\n'
        '    return {"status": "ok"}\n'
    )

    loop = _asyncio.new_event_loop()
    try:
        bus = EventBus()
        bus.bind_loop(loop)
        with _patch.object(Orchestrator, "_build_agent", return_value=None):
            orch = Orchestrator(
                engine=MLXEngine(), event_bus=bus, tools=[],
                generated_tools_dir=tmp_path,
            )
            loaded = orch.load_dynamic_tools()
    finally:
        loop.close()
    assert loaded == ["wibble"]


def test_pattern_recognizer_publishes_on_match(tmp_path) -> None:
    """A parseable JSON suggestion becomes a pattern_detected ContextEvent."""
    import asyncio as _asyncio

    from ai.pattern_recognizer import PatternRecognizer
    from events.event_bus import EventBus

    async def scenario():
        loop = _asyncio.get_running_loop()
        bus = EventBus()
        bus.bind_loop(loop)

        async def fake_eval(_prompt: str) -> str:
            return (
                '{"tool_name": "open_jira_ticket", '
                '"description": "Open the Jira ticket for the active branch.", '
                '"expected_logic": "shell git rev-parse, extract key, open URL"}'
            )

        rec = PatternRecognizer(
            event_bus=bus, evaluate=fake_eval,
            trigger_every=2, trigger_interval=3600, cooldown_seconds=0.01,
        )
        for _ in range(2):
            task = rec.observe(ContextEvent(event_type="app_activated", app_name="X"))
        assert task is not None
        await task
        emitted = await _asyncio.wait_for(bus.consume(), timeout=0.5)
        return emitted

    event = _asyncio.run(scenario())
    assert event.event_type == "pattern_detected"
    assert event.metadata["tool_name"] == "open_jira_ticket"


def test_pattern_recognizer_ignores_no_pattern(tmp_path) -> None:
    """NO_PATTERN replies never leak onto the bus."""
    import asyncio as _asyncio

    from ai.pattern_recognizer import PatternRecognizer
    from events.event_bus import EventBus

    async def scenario() -> bool:
        bus = EventBus()
        bus.bind_loop(_asyncio.get_running_loop())

        async def fake_eval(_prompt: str) -> str:
            return "NO_PATTERN"

        rec = PatternRecognizer(
            event_bus=bus, evaluate=fake_eval,
            trigger_every=1, trigger_interval=3600, cooldown_seconds=0.01,
        )
        task = rec.observe(ContextEvent(event_type="file"))
        assert task is not None
        await task
        return bus.qsize() == 0

    assert _asyncio.run(scenario())


def test_orchestrator_injects_recalled_memories_into_prompt() -> None:
    """A populated memory must surface as a 'memory' thought and feed the prompt.

    Drives the orchestrator directly (rather than via the FastAPI app) so
    the assertion doesn't depend on the full lifespan teardown while a
    ``_fallback_run`` task is blocked waiting on an approval response.
    """
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, patch as _patch

    from ai.memory import Memory, _fallback_embed
    from ai.mlx_engine import MLXEngine
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus

    async def _scenario() -> list:
        loop = _asyncio.get_running_loop()
        bus = EventBus()
        bus.bind_loop(loop)

        engine = MLXEngine()
        engine.evaluate_event = AsyncMock(return_value="")

        mem = Memory(db_path=":memory:", embedder=lambda t: _fallback_embed(t))
        mem.save_memory("the user prefers dark mode in Safari")

        collected: list = []

        async def _sub(message):
            collected.append(message)

        with _patch.object(Orchestrator, "_build_agent", return_value=None):
            orch = Orchestrator(
                engine=engine, event_bus=bus, memory=mem, tools=[]
            )
            orch.add_subscriber(_sub)
            await orch.start()
            bus.push_threadsafe(
                ContextEvent(
                    event_type="app_activated",
                    app_name="Safari",
                    window_title="Docs",
                )
            )
            await _asyncio.sleep(0.3)
            await orch.stop()

        mem.close()
        return collected

    received = _asyncio.run(_scenario())
    stages = {
        m.payload.stage: m.payload.content
        for m in received
        if getattr(m, "type", None) == "thought"
    }

    assert "memory" in stages, f"expected a 'memory' thought; got {list(stages)}"
    assert "dark mode" in stages["memory"]
    assert "reasoning" in stages
    assert "Relevant memories" in stages["reasoning"]


# ---------------------------------------------------------------------------
# Phase 5 — Chat
# ---------------------------------------------------------------------------


def test_chat_prompt_includes_user_text_and_memories() -> None:
    """``_build_chat_prompt`` folds the typed message + recalled memories."""
    from ai.orchestrator import Orchestrator

    event = ContextEvent(
        event_type="user_message",
        metadata={"event_id": "msg_abc", "text": "what's on my calendar?"},
    )
    prompt = Orchestrator._build_chat_prompt(
        event, memories=["User prefers morning meetings"]
    )
    assert "what's on my calendar?" in prompt
    assert "direct chat" in prompt
    assert "User prefers morning meetings" in prompt


def test_user_message_frame_is_routed_to_event_bus(client) -> None:
    """Inbound ``user_message`` WS frames must become ``ContextEvent``s."""
    message_id = "msg_test_route"
    try:
        with client.websocket_connect("/ws/stream") as ws:
            status = ws.receive_json()
            assert status["type"] == "status"

            ws.send_json(
                {
                    "type": "user_message",
                    "seq": 1,
                    "timestamp": "2026-04-19T12:00:00.000Z",
                    "payload": {
                        "message_id": message_id,
                        "text": "hello agent",
                    },
                }
            )

            saw_event_received = False
            saw_user_desc = False
            for _ in range(12):
                msg = ws.receive_json()
                if msg["type"] != "thought":
                    continue
                if msg["payload"]["stage"] == "event_received":
                    saw_event_received = True
                    if "User said: hello agent" in msg["payload"]["content"]:
                        saw_user_desc = True
                    assert msg["payload"]["event_id"] == message_id
                    break
            assert saw_event_received, "expected an 'event_received' thought"
            assert saw_user_desc, "expected the description to echo the user's text"
    finally:
        # The ``client`` fixture shares the production memory DB; scrub any
        # rows this test wrote so the user's live transcript stays clean.
        mem = client.app.state.memory
        conn = mem.connect()
        conn.execute("DELETE FROM chat_log WHERE event_id = ?", (message_id,))
        conn.commit()


def test_chat_log_persists_user_and_assistant_turn(tmp_path) -> None:
    """User + final assistant text are appended to ``chat_log``."""
    import asyncio as _asyncio

    from ai.memory import Memory
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus
    from ai.mlx_engine import MLXEngine

    mem = Memory(db_path=tmp_path / "chat.db")

    async def _scenario() -> None:
        bus = EventBus()
        bus.bind_loop(_asyncio.get_running_loop())
        engine = MLXEngine("stub")
        orch = Orchestrator(engine=engine, event_bus=bus, memory=mem, tools=[])
        # Short-circuit the agent: the fallback path returns ``"done"`` and
        # the test is only asserting persistence, not tool dispatch.
        orch._build_agent = lambda: None  # type: ignore[assignment]
        orch._agent = None
        await orch.start()
        bus.push_threadsafe(
            ContextEvent(
                event_type="user_message",
                metadata={"event_id": "msg_t1", "text": "remember this"},
            )
        )
        await _asyncio.sleep(0.4)
        await orch.stop()

    _asyncio.run(_scenario())
    rows = mem.get_chat_log(limit=50)
    mem.close()

    roles = [(r["event_id"], r["role"]) for r in rows]
    assert ("msg_t1", "user") in roles
    assert any(r == "assistant" for _, r in roles), (
        f"expected an assistant row; got {roles}"
    )


def test_api_chats_returns_recent_log(client) -> None:
    """``GET /api/chats`` surfaces rows persisted via :meth:`Memory.log_chat`."""
    mem = client.app.state.memory
    event_id = "msg_test_api"
    try:
        mem.log_chat(event_id, "user", "ping")
        mem.log_chat(event_id, "assistant", "pong")

        response = client.get("/api/chats")
        assert response.status_code == 200
        body = response.json()
        pairs = [(r["event_id"], r["role"], r["text"]) for r in body]
        assert (event_id, "user", "ping") in pairs
        assert (event_id, "assistant", "pong") in pairs
    finally:
        conn = mem.connect()
        conn.execute("DELETE FROM chat_log WHERE event_id = ?", (event_id,))
        conn.commit()


def test_user_message_ws_roundtrip() -> None:
    """Schema round-trip for the new ``user_message`` frame."""
    import json as _json

    from pydantic import TypeAdapter

    from schemas.ws_messages import (
        UserMessage,
        UserMessagePayload,
        WSMessage,
    )

    msg = UserMessage(
        seq=7,
        timestamp="2026-04-19T12:00:00.000Z",
        payload=UserMessagePayload(
            message_id="msg_abc123", text="hi there"
        ),
    )
    raw = msg.model_dump_json()
    parsed = TypeAdapter(WSMessage).validate_python(_json.loads(raw))
    assert parsed == msg
    assert type(parsed) is UserMessage



# ---------------------------------------------------------------------------
# MLX stability — lock + periodic cache clear
# ---------------------------------------------------------------------------


def test_generate_sync_holds_lock_and_bumps_counter() -> None:
    """``_generate_sync`` acquires :attr:`generation_lock` for the call.

    A sentinel stub replaces ``mlx_lm.generate`` with a function that
    observes the lock state mid-call. Verifies that (a) the lock is held
    while MLX runs and (b) the per-engine counter is incremented so the
    periodic cache clear has a trigger.
    """
    import sys
    import types

    from ai.mlx_engine import MLXEngine

    eng = MLXEngine("stub")
    eng._model = object()
    eng._tokenizer = object()
    eng._loaded = True

    observed: dict[str, bool] = {"locked": False}

    fake_mod = types.ModuleType("mlx_lm")
    def _fake_generate(model, tokenizer, prompt, max_tokens, verbose):  # noqa: D401
        observed["locked"] = eng.generation_lock.locked()
        return "ok"
    fake_mod.generate = _fake_generate  # type: ignore[attr-defined]
    sys.modules["mlx_lm"] = fake_mod
    try:
        before = eng._gen_count
        out = eng._generate_sync("p", 8)
    finally:
        sys.modules.pop("mlx_lm", None)

    assert out == "ok"
    assert observed["locked"] is True
    assert eng._gen_count == before + 1
    assert eng.generation_lock.locked() is False


def test_concurrent_sync_generations_serialize(monkeypatch) -> None:
    """Two threads calling :meth:`_generate_sync` never overlap inside MLX.

    Tracks the count of in-flight fake generations; the lock contract
    guarantees the maximum observed overlap is ``1``.
    """
    import sys
    import threading as _threading
    import time
    import types

    from ai.mlx_engine import MLXEngine

    eng = MLXEngine("stub")
    eng._model = object()
    eng._tokenizer = object()
    eng._loaded = True

    inflight = 0
    peak = 0
    gate = _threading.Lock()

    fake_mod = types.ModuleType("mlx_lm")
    def _fake_generate(model, tokenizer, prompt, max_tokens, verbose):  # noqa: D401
        nonlocal inflight, peak
        with gate:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with gate:
            inflight -= 1
        return "ok"
    fake_mod.generate = _fake_generate  # type: ignore[attr-defined]
    sys.modules["mlx_lm"] = fake_mod

    try:
        threads = [
            _threading.Thread(target=lambda: eng._generate_sync("p", 8))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.modules.pop("mlx_lm", None)

    assert peak == 1, f"expected serialized generations, saw peak overlap {peak}"


def test_metal_cache_clear_fires_on_interval(monkeypatch) -> None:
    """``_maybe_clear_metal_cache_locked`` invokes ``mx.metal.clear_cache``.

    Patches ``AGENT_MLX_CACHE_EVERY`` to 2 via the module constant and
    asserts the stub is called on the 2nd and 4th generation, never in
    between.
    """
    import sys
    import types

    from ai import mlx_engine as _mlx_mod
    from ai.mlx_engine import MLXEngine

    monkeypatch.setattr(_mlx_mod, "_CACHE_CLEAR_EVERY_N", 2)

    eng = MLXEngine("stub")
    eng._model = object()
    eng._tokenizer = object()
    eng._loaded = True

    calls = {"n": 0}
    fake_metal = types.SimpleNamespace(clear_cache=lambda: calls.__setitem__("n", calls["n"] + 1))
    fake_mx = types.ModuleType("mlx.core")
    fake_mx.metal = fake_metal  # type: ignore[attr-defined]
    fake_mx_pkg = types.ModuleType("mlx")
    fake_mx_pkg.core = fake_mx  # type: ignore[attr-defined]
    sys.modules["mlx"] = fake_mx_pkg
    sys.modules["mlx.core"] = fake_mx

    fake_mlx_lm = types.ModuleType("mlx_lm")
    fake_mlx_lm.generate = lambda *a, **kw: "ok"  # type: ignore[attr-defined]
    sys.modules["mlx_lm"] = fake_mlx_lm

    try:
        for _ in range(5):
            eng._generate_sync("p", 8)
    finally:
        for k in ("mlx", "mlx.core", "mlx_lm"):
            sys.modules.pop(k, None)

    # Ran 5 times with interval=2 → triggers at gen 2 and gen 4.
    assert calls["n"] == 2, f"expected 2 cache clears, got {calls['n']}"
