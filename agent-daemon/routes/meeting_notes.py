"""Meeting notes settings (#86).

- ``GET /api/meeting-notes``  ``{enabled, engine, engine_in_use, engines,
  whisper_available, notes_dir, locked_fields}``
- ``PUT /api/meeting-notes``  body ``{enabled?: bool, engine?: "apple"|"whisper"}``
  (400 for an unknown engine)

Saved through ``user_settings`` (``AGENT_MEETING_NOTES``,
``AGENT_MEETING_NOTES_ENGINE``) and read live by ``events.meeting_notes``.
Off by default. The app-wide ``AuthMiddleware`` (#27) guards both routes.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from events import meeting_notes

router = APIRouter(prefix="/api/meeting-notes", tags=["meeting-notes"])


class MeetingNotesUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    engine: Optional[str] = None


@router.get("")
async def get_meeting_notes() -> dict[str, Any]:
    return await asyncio.to_thread(meeting_notes.settings_view)


@router.put("")
async def put_meeting_notes(body: MeetingNotesUpdate) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            meeting_notes.update_settings, body.enabled, body.engine,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
