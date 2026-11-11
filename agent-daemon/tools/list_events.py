"""List events from the Mac's calendars (read-only, #42).

Reads through :mod:`tools.calendar_access` (EventKit, or AppleScript when
pinned/denied). It changes nothing, so it belongs in
``tools.READ_ONLY_TOOL_NAMES`` and runs without an approval prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import calendar_access

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


READ_ONLY: frozenset[str] = frozenset({"list_events"})
GATED: frozenset[str] = frozenset()

MAX_RANGE_DAYS = 31


def _today() -> datetime:
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_range(start: str, end: str) -> tuple[datetime, datetime]:
    """Apply the defaults: empty start = today 00:00; empty end = the day after
    start's date; a date-only end includes that whole day."""
    begin = calendar_access.parse_local(start) if (start or "").strip() else _today()
    if (end or "").strip():
        finish = calendar_access.parse_local(end)
        if calendar_access.is_date_only(end):
            finish += timedelta(days=1)
    else:
        finish = begin.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return begin, finish


def _with_when(event: dict[str, Any]) -> dict[str, Any]:
    """Add a readable ``when`` (weekday, date, time) to an event.

    Models guessed weekdays from ISO dates and got them wrong (live check:
    "Wednesday Nov 9" for Monday 2026-11-09), so state the day outright.
    """
    out = dict(event)
    try:
        start = calendar_access.parse_local(str(event.get("start") or ""))
        if event.get("all_day"):
            out["when"] = start.strftime("%A %B %-d (all day)")
        else:
            end_raw = str(event.get("end") or "")
            end = calendar_access.parse_local(end_raw) if end_raw else None
            span = start.strftime("%A %B %-d, %-I:%M %p")
            if end is not None:
                span += end.strftime(" – %-I:%M %p")
            out["when"] = span
    except Exception:
        pass
    if isinstance(event.get("attendees"), list):
        # #80: names only. Addresses are personal data the model doesn't
        # need to answer "who is in it" (and the main engine may be a cloud one).
        out["attendees"] = [
            str(p.get("name") or "").strip() for p in event["attendees"]
            if isinstance(p, dict) and not p.get("is_self") and str(p.get("name") or "").strip()
        ]
    return out


@tool
def list_events(start: str = "", end: str = "") -> dict[str, Any]:
    """List events on the Mac's calendars (iCloud, Google, Exchange, ...).

    Args:
        start: ISO 8601 local date or datetime, e.g. "2026-11-10" or
            "2026-11-10T09:00". Empty means today at midnight.
        end: ISO 8601 local date or datetime. Empty means the end of the
            start day. A date without a time includes that whole day. Ranges
            longer than 31 days are cut to 31 days.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``start``, ``end``,
        ``count``, ``truncated`` and ``events`` (each with title, start, end,
        calendar, location, notes, url, all_day, and ``attendees`` (other
        people's names) when the calendar reports them), or an ``error`` string.
    """
    try:
        begin, finish = resolve_range(start, end)
    except ValueError as exc:
        return {"status": "error", "error": f"Bad date: {exc}. Use ISO 8601, e.g. 2026-11-10T09:00."}
    if finish <= begin:
        return {"status": "error", "error": "end must be after start."}
    try:
        backend = calendar_access.get_backend()
        cap = min(MAX_RANGE_DAYS, getattr(backend, "max_range_days", MAX_RANGE_DAYS))
        truncated = finish - begin > timedelta(days=cap)
        if truncated:
            finish = begin + timedelta(days=cap)
        events = backend.list(begin, finish)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    events = [_with_when(e) for e in events]
    return {
        "status": "ok",
        "start": calendar_access.iso(begin),
        "end": calendar_access.iso(finish),
        "count": len(events),
        "truncated": truncated,
        "events": events,
    }
