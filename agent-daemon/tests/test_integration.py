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
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before importing main so lifespan skips OS listeners.
os.environ["AGENT_DISABLE_LISTENERS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from events.event_bus import ContextEvent  # noqa: E402


@pytest.fixture()
def client():
    with patch(
        "ai.orchestrator.Orchestrator._build_agent", return_value=None
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


def test_approval_request_and_response_roundtrip(client) -> None:
    bus = client.app.state.event_bus
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
