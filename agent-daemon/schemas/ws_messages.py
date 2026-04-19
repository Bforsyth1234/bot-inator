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
    "event_received",
    "analysis",
    "memory",
    "reasoning",
    "plan",
    "tool_result",
    "complete",
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
    # Optional args the user edited in the approval UI. Mirrors the shape of
    # ``ApprovalRequestPayload.tool_args`` (``{"args": [...], "kwargs": {...}}``).
    # When present, the orchestrator invokes the tool with these values
    # instead of the ones the agent originally proposed.
    edited_args: dict[str, Any] | None = None


class CodeApprovalRequestPayload(BaseModel):
    """Server→client request to review AI-generated Python tool source.

    Sent by the meta-tool generator after it has drafted a module and passed
    its static safety checks. The orchestrator blocks until a matching
    :class:`CodeApprovalResponsePayload` arrives.
    """

    request_id: str
    event_id: str
    tool_name: str
    description: str
    code: str
    timeout_seconds: int = 300


class CodeApprovalResponsePayload(BaseModel):
    """Client→server response to a :class:`CodeApprovalRequestPayload`."""

    request_id: str
    approved: bool
    # Full source text, if the user hand-edited the draft in the review
    # panel. When present, the meta-tool writes this content instead of the
    # originally-proposed code.
    edited_code: str | None = None
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
    "pause_listeners",
    "resume_listeners",
    "reload_model",
    "clear_memory",
    "reload_dynamic_tools",
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


class CodeApprovalRequest(_BaseMessage):
    type: Literal["code_approval_request"] = "code_approval_request"
    payload: CodeApprovalRequestPayload


class CodeApprovalResponse(_BaseMessage):
    type: Literal["code_approval_response"] = "code_approval_response"
    payload: CodeApprovalResponsePayload


class Status(_BaseMessage):
    type: Literal["status"] = "status"
    payload: StatusPayload


class Command(_BaseMessage):
    type: Literal["command"] = "command"
    payload: CommandPayload


WSMessage = Annotated[
    Union[
        Thought,
        ApprovalRequest,
        ApprovalResponse,
        CodeApprovalRequest,
        CodeApprovalResponse,
        Status,
        Command,
    ],
    Field(discriminator="type"),
]
