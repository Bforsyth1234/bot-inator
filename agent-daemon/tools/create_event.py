"""Create an event in the Mac's Calendar (approval-gated, #42).

Writes through :mod:`tools.calendar_access`. It changes user data, so it
must NOT be in ``tools.READ_ONLY_TOOL_NAMES``: every call raises an
``approval_request`` first (LIVING_SPEC invariant 1a).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import calendar_access

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


GATED: frozenset[str] = frozenset({"create_event"})
READ_ONLY: frozenset[str] = frozenset()

DEFAULT_DURATION = timedelta(hours=1)


@tool
def create_event(
    title: str, start: str, end: str = "", calendar: str = "",
    location: str = "", notes: str = "",
) -> dict[str, Any]:
    """Add an event to the Mac's Calendar. The user approves it first.

    Args:
        title: Event title, e.g. "Lunch with Sam".
        start: ISO 8601 local datetime, e.g. "2026-11-10T12:00".
        end: ISO 8601 local datetime. Empty means one hour after start.
        calendar: Name of the calendar to add it to. Empty uses the default
            calendar for new events.
        location: Optional location text (an address or a meeting link).
        notes: Optional notes.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``event_id`` and
        ``calendar`` on success, or an ``error`` string.
    """
    title = (title or "").strip()
    if not title:
        return {"status": "error", "error": "title is required."}
    try:
        begin = calendar_access.parse_local(start)
        finish = calendar_access.parse_local(end) if (end or "").strip() else begin + DEFAULT_DURATION
    except ValueError as exc:
        return {"status": "error", "error": f"Bad date: {exc}. Use ISO 8601, e.g. 2026-11-10T12:00."}
    if finish <= begin:
        return {"status": "error", "error": "end must be after start."}
    try:
        result = calendar_access.create(
            title, begin, finish, calendar or None, location or None, notes or None,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {
        "status": "ok",
        "event_id": result.get("event_id", ""),
        "calendar": result.get("calendar", calendar),
        "title": title,
        "start": calendar_access.iso(begin),
        "end": calendar_access.iso(finish),
    }
