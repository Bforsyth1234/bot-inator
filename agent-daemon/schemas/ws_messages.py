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


class ApprovalAction(BaseModel):
    """One action of a bundled approval card (#76).

    ``args`` are the tool's kwargs. The card shows a tick box per action;
    the response sends the list back in ``edited_args["actions"]`` with each
    ``enabled`` as the user left it, and only ticked actions run.
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    label: str = ""


class ApprovalRequestPayload(BaseModel):
    request_id: str
    event_id: str
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    reasoning: str
    timeout_seconds: int = 120
    # Plain-language summary of a suggested tool (#64): set on
    # ``generate_custom_tool`` idea cards, ``None`` otherwise.
    summary: str | None = None
    # A prepared offer (#76): the actions one approval runs, each with a
    # tick box. ``None`` (the default, and older daemons) is a one-call card.
    actions: list[ApprovalAction] | None = None
    # #83: the action journal can undo this tool's result (``ai.action_journal
    # .is_undoable``). The app shows a reversible card as a small panel that
    # does not take focus; it is still a card (invariant 7a). False (the
    # default, and older daemons) keeps the full window.
    reversible: bool = False
    # #81: lines saying the card was shaped by outside text this turn read
    # ("Read this turn: email from ...", "to: from the email, not from
    # you"). ``None`` (the default, and every unmarked turn) shows nothing.
    origin_notes: list[str] | None = None


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
    # Plain-language summary shown above the code (#64): what the tool
    # does, when it runs, what the drafted source touches, that it asks.
    summary: str | None = None
    # #90: a repair's review card shows a diff against the installed source.
    # ``None`` for a new tool (and older daemons).
    previous_code: str | None = None


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
    # Per-listener liveness (#16/#19), e.g. ``{"nsworkspace": {"alive":
    # true, "restart_count": 0, ...}}``. ``None`` when listeners are off.
    listener_health: dict[str, dict[str, Any]] | None = None
    # Provider each engine runs on (#31): ``{"main": "local", ...}``.
    engine_providers: dict[str, str] | None = None
    # True when ``AGENT_CLOUD_PERSONAL_TEXT=1`` lets personal text reach
    # cloud engines; False when it is replaced by a placeholder (#31).
    cloud_personal_text: bool | None = None
    # #87 right-moment delivery: Pause state (``paused_until`` is an ISO
    # time, or "indefinite"), offers held until the user is free, and the
    # Inbox (offers past the daily cap). ``None`` from older daemons.
    paused: bool | None = None
    paused_until: str | None = None
    held_count: int | None = None
    inbox_count: int | None = None


NoticeLevel = Literal["info", "warning", "error"]

# System Settings → Privacy & Security → Full Disk Access.
FULL_DISK_ACCESS_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
)
# ``ContextEvent.event_type`` a listener thread uses to hand a notice to
# the orchestrator through the event bus (``metadata={"notice": {...}}``).
NOTICE_EVENT_TYPE = "system_notice"


class NoticeAction(BaseModel):
    kind: Literal["open_settings"]
    url: str


class NoticePayload(BaseModel):
    """Server→client banner (#19). ``id`` is stable: a client replaces a
    notice with the same id, and the daemon publishes each id once per run
    and replays the active ones to every client that connects."""

    id: str
    level: NoticeLevel = "warning"
    title: str
    body: str
    action: NoticeAction | None = None


def full_disk_access_notice(source: str, feature: str) -> NoticePayload:
    """The "grant Full Disk Access" notice for a listener (``source``)."""
    return NoticePayload(
        id=f"full_disk_access:{source}",
        level="warning",
        title="Full Disk Access needed",
        body=(
            f"{feature} can't be read because the app running the daemon "
            "doesn't have Full Disk Access. Turn it on in System Settings → "
            "Privacy & Security → Full Disk Access, then restart the daemon."
        ),
        action=NoticeAction(
            kind="open_settings", url=FULL_DISK_ACCESS_SETTINGS_URL
        ),
    )


class ToolsChangedPayload(BaseModel):
    """Server→client hint that ``GET /api/tools`` would now differ (#29).

    One frame per change (a load call, an unload, a quarantine), not one
    per tool. ``tool_names`` lists the tools that changed.
    """

    reason: Literal["loaded", "unloaded", "quarantined", "deleted"]
    tool_names: list[str] = Field(default_factory=list)



# ---- Calendar through the menu bar app (#60) ----------------------------------
# The app owns EventKit; the daemon asks it over this WebSocket.
# EKAuthorizationStatus names, as ``tools.calendar_access`` uses them.
CalendarAuthorization = Literal[
    "not_determined", "restricted", "denied", "full_access", "write_only"
]
CalendarOp = Literal["list", "create", "request_access", "get", "delete"]


class CalendarRequestPayload(BaseModel):
    """Server→client: run one EventKit operation in the app.

    ``list`` args: ``{start, end, calendars?}`` (ISO 8601 with offset).
    ``create`` args: ``{title, start, end, calendar?, location?, notes?}``.
    ``request_access`` (a Permissions "Request" click) takes no args.
    ``get`` / ``delete`` (#83, undo only): ``{event_id}``. ``get`` answers
    ``events`` = ``[]`` (no such event) or one event; ``delete`` removes that
    one event (this occurrence) and answers ``ok``. Only the action journal
    sends them, with an id it stored; the model has no delete tool.
    Sent to ONE connection (the app that advertised ``calendar_status``).
    """

    request_id: str
    op: CalendarOp
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 15


class CalendarResponsePayload(BaseModel):
    """Client→server answer to a :class:`CalendarRequestPayload`.

    ``events`` (list) are #42 event dicts; ``event_id`` + ``calendar``
    (create). ``authorization`` is the app's status after the operation.
    """

    request_id: str
    ok: bool
    events: list[dict[str, Any]] | None = None
    event_id: str | None = None
    calendar: str | None = None
    error: str | None = None
    authorization: CalendarAuthorization | None = None


class CalendarStatusPayload(BaseModel):
    """Client→server: the app can serve calendar requests, and its EventKit
    authorization (read without prompting). Sent on connect and whenever it
    changes, so ``ready()`` never has to ask."""

    authorization: CalendarAuthorization


CommandAction = Literal[
    "pause_listeners",
    "resume_listeners",
    "reload_model",
    "clear_memory",
    "reload_dynamic_tools",
]


# #87: how long ``pause_listeners`` pauses (default: until resumed).
PauseDuration = Literal["1h", "tomorrow", "indefinite"]


class CommandPayload(BaseModel):
    action: CommandAction
    # Only for ``pause_listeners`` (#87); ``None`` means "indefinite".
    duration: PauseDuration | None = None


SelectionSource = Literal["selection", "clipboard"]


class ChatContextPayload(BaseModel):
    """What the user was looking at when they opened quick-ask (#74).

    Every field is optional. ``selection`` is text the user chose to
    include for this one message (the app's selected text via
    Accessibility, or the clipboard when the user picked "use what's on
    the clipboard"); ``selection_source`` says which. The daemon renders
    it into that turn's prompt only (:mod:`ai.chat_context`); it is not
    stored in memory.
    """

    front_app: str | None = None
    window_title: str | None = None
    selection: str | None = None
    selection_source: SelectionSource | None = None


class UserMessagePayload(BaseModel):
    """Client→server chat message typed by the user in the chat window.

    ``message_id`` is generated on the client and reused as the
    ``event_id`` on every ``thought`` frame the daemon emits while handling
    the turn, so the chat UI can group the agent's streamed reasoning
    under the originating user bubble.

    ``context`` (#74) is set by the quick-ask panel; absent from the Chat
    window and older clients.
    """

    message_id: str
    text: str
    context: ChatContextPayload | None = None


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


class Notice(_BaseMessage):
    type: Literal["notice"] = "notice"
    payload: NoticePayload


class ToolsChanged(_BaseMessage):
    type: Literal["tools_changed"] = "tools_changed"
    payload: ToolsChangedPayload


class Command(_BaseMessage):
    type: Literal["command"] = "command"
    payload: CommandPayload


class UserMessage(_BaseMessage):
    type: Literal["user_message"] = "user_message"
    payload: UserMessagePayload



class CalendarRequest(_BaseMessage):
    type: Literal["calendar_request"] = "calendar_request"
    payload: CalendarRequestPayload


class CalendarResponse(_BaseMessage):
    type: Literal["calendar_response"] = "calendar_response"
    payload: CalendarResponsePayload


class CalendarStatus(_BaseMessage):
    type: Literal["calendar_status"] = "calendar_status"
    payload: CalendarStatusPayload


WSMessage = Annotated[
    Union[
        Thought,
        ApprovalRequest,
        ApprovalResponse,
        CodeApprovalRequest,
        CodeApprovalResponse,
        Status,
        Notice,
        ToolsChanged,
        Command,
        UserMessage,
        CalendarRequest,
        CalendarResponse,
        CalendarStatus,
    ],
    Field(discriminator="type"),
]
