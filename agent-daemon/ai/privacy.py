"""Keep personal text out of cloud-engine prompts (todo #31).

When the engine that will read a prompt runs on a cloud provider (Groq),
personal text is replaced by :data:`PLACEHOLDER` unless the user opted in
with ``AGENT_CLOUD_PERSONAL_TEXT=1`` (invariant 7c).

"Personal text" here means words written by or to someone else that the
user never typed to the assistant:

* iMessage text (``imessage_received`` ``metadata.text``),
* email subjects (``mail_received`` ``metadata.subject``),
* email bodies and subjects returned by ``read_email``,
* the same text inside built-in follow-up ``event_details`` (#32).

Not covered (decision scope, unchanged from before #31): sender handles and
addresses (the agent needs them to act), window titles, file paths, app
names, calendar titles, and the user's own chat messages to the assistant.

``role`` is an engine role (``"main"``, ``"analysis"``, ``"drafting"``) or
``"detection"`` for the engine the idea sources use
(``AGENT_PATTERN_ENGINE``). Settings are read on every call so a test or a
settings change takes effect without a restart of this module.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Optional

from events.event_bus import ContextEvent

PLACEHOLDER = "[personal text kept on this Mac]"
OPT_IN_ENV = "AGENT_CLOUD_PERSONAL_TEXT"

# event_type -> metadata keys holding personal text.
PERSONAL_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "imessage_received": ("text",),
    "mail_received": ("subject",),
}
# ``read_email`` result keys holding personal text.
PERSONAL_EMAIL_FIELDS: tuple[str, ...] = ("subject", "body")


def personal_text_allowed() -> bool:
    return os.environ.get(OPT_IN_ENV, "") == "1"


def role_is_cloud(role: str) -> bool:
    from config import settings

    if role == "detection":
        return settings.detection_is_cloud()
    return settings.is_cloud(role)


def should_redact(role: Optional[str]) -> bool:
    """True when prompts for ``role`` must not carry personal text."""
    if role is None or personal_text_allowed():
        return False
    return role_is_cloud(role)


def redact_text(text: Any, role: Optional[str]) -> Any:
    if not text or not should_redact(role):
        return text
    return PLACEHOLDER


def redacted_event(event: ContextEvent) -> ContextEvent:
    """A copy of ``event`` with its personal fields replaced (always)."""
    fields = PERSONAL_EVENT_FIELDS.get(event.event_type)
    meta = event.metadata or {}
    if not fields or not any(meta.get(k) for k in fields):
        return event
    new_meta = dict(meta)
    for key in fields:
        if new_meta.get(key):
            new_meta[key] = PLACEHOLDER
    return dataclasses.replace(event, metadata=new_meta)


def redact_event(event: ContextEvent, role: Optional[str]) -> ContextEvent:
    """``event`` as the ``role`` engine may see it."""
    return redacted_event(event) if should_redact(role) else event


def redact_email_result(result: Any, role: Optional[str]) -> Any:
    """A ``read_email`` result as the ``role`` engine may see it."""
    if not isinstance(result, dict) or not should_redact(role):
        return result
    out = dict(result)
    for key in PERSONAL_EMAIL_FIELDS:
        if out.get(key):
            out[key] = PLACEHOLDER
    if any(k in result for k in PERSONAL_EMAIL_FIELDS):
        out["note"] = (
            "Subject and body were kept on this Mac because this engine "
            f"runs in the cloud ({OPT_IN_ENV}=1 allows sending them)."
        )
    return out
