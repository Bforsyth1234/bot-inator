"""Pydantic v2 models for the WebSocket protocol.

All messages share a `type` discriminator, a monotonic `seq`, and an
ISO-8601 `timestamp`. The `WSMessage` type alias is a discriminated union
over the `type` field for validation of inbound payloads.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

ThoughtStage = Literal[
    "event_received", "analysis", "reasoning", "plan", "tool_result", "complete"
]


class ThoughtPayload(BaseModel):
    event_id: str
    stage: ThoughtStage
    content: str


class ApprovalRequestPayload(BaseModel):
    request_id: str
    event_id: str
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    reasoning: str
    timeout_seconds: int = 120


class ApprovalResponsePayload(BaseModel):
    request_id: str
    approved: bool
    user_note: str | None = None


StatusState = Literal[
    "starting", "loading_model", "ready", "processing", "error"
]


class StatusPayload(BaseModel):
    state: StatusState
    model_loaded: str | None = None
    listeners_active: list[str] = Field(default_factory=list)
    version: str = "0.1.0"


CommandAction = Literal[
    "pause_listeners", "resume_listeners", "reload_model", "clear_memory"
]


class CommandPayload(BaseModel):
    action: CommandAction


# ---------------------------------------------------------------------------
# Messages (discriminated on `type`)
# ---------------------------------------------------------------------------


class _BaseMessage(BaseModel):
    seq: int
    timestamp: str


class Thought(_BaseMessage):
    type: Literal["thought"] = "thought"
    payload: ThoughtPayload


class ApprovalRequest(_BaseMessage):
    type: Literal["approval_request"] = "approval_request"
    payload: ApprovalRequestPayload


class ApprovalResponse(_BaseMessage):
    type: Literal["approval_response"] = "approval_response"
    payload: ApprovalResponsePayload


class Status(_BaseMessage):
    type: Literal["status"] = "status"
    payload: StatusPayload


class Command(_BaseMessage):
    type: Literal["command"] = "command"
    payload: CommandPayload


WSMessage = Annotated[
    Union[Thought, ApprovalRequest, ApprovalResponse, Status, Command],
    Field(discriminator="type"),
]
