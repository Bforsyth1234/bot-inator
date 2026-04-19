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
