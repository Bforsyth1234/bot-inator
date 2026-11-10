"""On-demand morning brief tool (#65).

The gathering lives in :mod:`events.daily_brief`; it is imported inside
the function because that module's imports lead back to this package.
"""

from __future__ import annotations

from typing import Any

try:
    from smolagents import tool  # type: ignore
except Exception:  # pragma: no cover - smolagents is a hard dependency
    def tool(func):  # type: ignore
        return func


@tool
def brief_me() -> dict[str, Any]:
    """Get today's brief: today's meetings, mail from the last 24 hours and reminders due or overdue.

    Use when the user asks for their brief, their day, or "brief me".
    Read-only. Summarise the returned ``brief`` for the user; keep any
    "unavailable" line.

    Returns:
        A dict with ``status`` ("ok") and ``brief`` (plain text).
    """
    from events import daily_brief

    return daily_brief.brief_me()
