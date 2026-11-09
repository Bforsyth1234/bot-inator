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


# ---- wave 5: notice (#19), tools_changed (#29), status fields (#19/#31) ----

from schemas.ws_messages import (  # noqa: E402
    FULL_DISK_ACCESS_SETTINGS_URL,
    Notice,
    NoticeAction,
    NoticePayload,
    ToolsChanged,
    ToolsChangedPayload,
    full_disk_access_notice,
)


def test_status_with_health_and_providers_roundtrip() -> None:
    msg = Status(
        seq=7,
        timestamp=_TS,
        payload=StatusPayload(
            state="ready",
            listeners_active=["nsworkspace"],
            listener_health={"nsworkspace": {
                "alive": True, "pid": 123, "restart_count": 2,
                "last_error": None, "uptime_s": 4.5,
            }},
            engine_providers={"main": "groq", "analysis": "local", "drafting": "groq"},
            cloud_personal_text=False,
        ),
    )
    _roundtrip(msg)
    # Old clients' hello shape still validates (new fields default to None).
    bare = _ADAPTER.validate_python({
        "type": "status", "seq": 1, "timestamp": _TS,
        "payload": {"state": "ready"},
    })
    assert bare.payload.listener_health is None
    assert bare.payload.engine_providers is None


def test_notice_roundtrip_with_and_without_action() -> None:
    _roundtrip(Notice(seq=8, timestamp=_TS, payload=full_disk_access_notice(
        "imessage_watcher", "iMessages",
    )))
    _roundtrip(Notice(seq=9, timestamp=_TS, payload=NoticePayload(
        id="x", level="info", title="T", body="B", action=None,
    )))
    raw = json.loads(Notice(seq=10, timestamp=_TS, payload=NoticePayload(
        id="fda", title="T", body="B",
        action=NoticeAction(kind="open_settings", url=FULL_DISK_ACCESS_SETTINGS_URL),
    )).model_dump_json())
    assert raw["type"] == "notice"
    assert raw["payload"]["action"] == {
        "kind": "open_settings",
        "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
    }


def test_tools_changed_roundtrip() -> None:
    _roundtrip(ToolsChanged(seq=11, timestamp=_TS, payload=ToolsChangedPayload(
        reason="loaded", tool_names=["a_tool", "b_tool"],
    )))
    raw = json.loads(ToolsChanged(seq=12, timestamp=_TS, payload=ToolsChangedPayload(
        reason="unloaded",
    )).model_dump_json())
    assert raw == {"seq": 12, "timestamp": _TS, "type": "tools_changed",
                   "payload": {"reason": "unloaded", "tool_names": []}}
