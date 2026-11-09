"""List and create reminders in the Mac's Reminders (#45).

Both tools go through :mod:`tools.calendar_access` (EventKit
``EKEntityTypeReminder``, or Reminders.app via AppleScript when pinned or
denied). ``list_reminders`` only reads, so it belongs in
``tools.READ_ONLY_TOOL_NAMES``; ``create_reminder`` changes user data and
must raise an ``approval_request`` first (LIVING_SPEC invariant 1a).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import calendar_access

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


READ_ONLY: frozenset[str] = frozenset({"list_reminders"})
GATED: frozenset[str] = frozenset({"create_reminder"})

MAX_RESULTS = 200


def parse_due(due: str) -> tuple[Optional[datetime], bool]:
    """``(local datetime or None, all_day)``. Empty means no due date; a date
    without a time is an all-day due date. Raises ``ValueError`` otherwise."""
    text = (due or "").strip()
    if not text:
        return None, False
    when = calendar_access.parse_local(text)
    return when, calendar_access.is_date_only(text)


@tool
def list_reminders(list_name: str = "", include_completed: bool = False) -> dict[str, Any]:
    """List reminders from the Mac's Reminders (iCloud and other synced lists).

    Args:
        list_name: Exact name of one reminders list, e.g. "Groceries". Empty
            means every list.
        include_completed: True to include reminders already checked off.
            False (default) returns only open reminders.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``count``, ``truncated``
        and ``reminders`` (each with id, title, due, all_day, list, notes,
        completed; ``due`` is "" for none, "YYYY-MM-DD" for an all-day due
        date, else an ISO datetime), soonest due first, or an ``error``.
    """
    name = (list_name or "").strip()
    try:
        items = calendar_access.list_reminders([name] if name else None, bool(include_completed))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    truncated = len(items) > MAX_RESULTS
    items = items[:MAX_RESULTS]
    return {"status": "ok", "count": len(items), "truncated": truncated, "reminders": items}


@tool
def create_reminder(title: str, due: str = "", list_name: str = "", notes: str = "") -> dict[str, Any]:
    """Add a reminder to the Mac's Reminders. The user approves it first.

    Args:
        title: What to be reminded of, e.g. "Call the vet".
        due: ISO 8601 local date or datetime, e.g. "2026-11-10T09:00" (alerts
            at that time) or "2026-11-14" (all-day, no time). Empty means no
            due date.
        list_name: Exact name of the reminders list to add it to. Empty uses
            the default list.
        notes: Optional notes.

    Returns:
        A dict with ``status`` ("ok" or "error"); on success ``reminder_id``,
        ``list``, ``title``, ``due`` and ``all_day``, else an ``error`` string.
    """
    title = (title or "").strip()
    if not title:
        return {"status": "error", "error": "title is required."}
    try:
        when, all_day = parse_due(due)
    except ValueError as exc:
        return {"status": "error",
                "error": f"Bad due date: {exc}. Use ISO 8601, e.g. 2026-11-10T09:00 or 2026-11-10."}
    try:
        result = calendar_access.create_reminder(
            title, when, all_day, (list_name or "").strip() or None, notes or None,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    shown = calendar_access.make_reminder(due=when, all_day=all_day)
    return {
        "status": "ok",
        "reminder_id": result.get("reminder_id", ""),
        "list": result.get("list", list_name),
        "title": title,
        "due": shown["due"],
        "all_day": shown["all_day"],
    }
