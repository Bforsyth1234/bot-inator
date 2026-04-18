"""Round-trip tests for every WebSocket message type."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schemas.ws_messages import (  # noqa: E402
    ApprovalRequest,
    ApprovalRequestPayload,
    ApprovalResponse,
    ApprovalResponsePayload,
    Command,
    CommandPayload,
    Status,
    StatusPayload,
    Thought,
    ThoughtPayload,
    WSMessage,
)

_ADAPTER: TypeAdapter = TypeAdapter(WSMessage)
_TS = "2026-04-17T12:00:00.000Z"


def _roundtrip(message) -> None:
    raw = message.model_dump_json()
    parsed = _ADAPTER.validate_python(json.loads(raw))
    assert parsed == message
    assert type(parsed) is type(message)


def test_thought_roundtrip() -> None:
    msg = Thought(
        seq=1,
        timestamp=_TS,
        payload=ThoughtPayload(
            event_id="evt_abc123",
            stage="reasoning",
            content="Detected Jira opened in Chrome.",
        ),
    )
    _roundtrip(msg)


def test_approval_request_roundtrip() -> None:
    msg = ApprovalRequest(
        seq=2,
        timestamp=_TS,
        payload=ApprovalRequestPayload(
            request_id="req_xyz789",
            event_id="evt_abc123",
            tool_name="run_in_sandbox",
            tool_args={"image": "python:3.11-slim", "command": "python run.py"},
            reasoning="Need to generate the sprint report.",
            timeout_seconds=120,
        ),
    )
    _roundtrip(msg)


def test_approval_response_roundtrip() -> None:
    msg = ApprovalResponse(
        seq=3,
        timestamp=_TS,
        payload=ApprovalResponsePayload(
            request_id="req_xyz789",
            approved=True,
            user_note="Looks good, proceed.",
        ),
    )
    _roundtrip(msg)


def test_status_roundtrip() -> None:
    msg = Status(
        seq=0,
        timestamp=_TS,
        payload=StatusPayload(
            state="ready",
            model_loaded="mlx-community/Qwen2.5-Coder-7B-4bit",
            listeners_active=["nsworkspace", "file_watcher"],
            version="0.1.0",
        ),
    )
    _roundtrip(msg)


def test_command_roundtrip() -> None:
    msg = Command(
        seq=4,
        timestamp=_TS,
        payload=CommandPayload(action="pause_listeners"),
    )
    _roundtrip(msg)
