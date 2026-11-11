"""Meeting prep settings (#71).

- ``GET /api/meeting-prep``  ``{enabled, minutes_before, min_minutes, max_minutes, locked_fields}``
- ``PUT /api/meeting-prep``  body ``{enabled?: bool, minutes_before?: int}``

Saved through ``user_settings`` (keys ``AGENT_MEETING_PREP``,
``AGENT_MEETING_PREP_MINUTES``) and read live by ``events.meeting_prep``,
so no restart is needed. The app-wide ``AuthMiddleware`` (#27) guards it.

Wiring (main.py): ``app.include_router(meeting_prep_routes.router)``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import user_settings
from events import meeting_prep

router = APIRouter(prefix="/api/meeting-prep", tags=["meeting-prep"])


class MeetingPrepUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    enabled: Optional[bool] = None
    minutes_before: Optional[int] = None


def _view() -> dict[str, Any]:
    locked = [
        field for field, key in (
            ("enabled", meeting_prep.ENABLED_KEY), ("minutes_before", meeting_prep.MINUTES_KEY),
        )
        if key in user_settings.shell_exported
    ]
    return {
        "enabled": meeting_prep.prep_enabled(),
        "minutes_before": meeting_prep.minutes_before(),
        "min_minutes": meeting_prep.MIN_MINUTES,
        "max_minutes": meeting_prep.MAX_MINUTES,
        "locked_fields": locked,
    }


@router.get("")
def get_meeting_prep() -> dict[str, Any]:
    return _view()


@router.put("")
def put_meeting_prep(body: MeetingPrepUpdate) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if body.enabled is not None:
        # Stored as "1"/"0" (never removed) so an explicit "off" sticks.
        updates[meeting_prep.ENABLED_KEY] = "1" if body.enabled else "0"
    if body.minutes_before is not None:
        minutes = meeting_prep.parse_minutes(body.minutes_before)
        if minutes is None:
            raise HTTPException(
                status_code=400,
                detail=f"minutes_before must be {meeting_prep.MIN_MINUTES}-{meeting_prep.MAX_MINUTES}",
            )
        updates[meeting_prep.MINUTES_KEY] = str(minutes)
    if updates:
        try:
            user_settings.write(updates)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _view()
