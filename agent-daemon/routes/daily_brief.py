"""Morning brief settings and "Brief me now" (#65).

- ``GET  /api/daily-brief``      ``{enabled, time, locked_fields}``
- ``PUT  /api/daily-brief``      body ``{enabled?: bool, time?: "HH:MM"}``
- ``POST /api/daily-brief/run``  publish a ``daily_brief`` event now

Settings are saved through ``user_settings`` (keys ``AGENT_DAILY_BRIEF``,
``AGENT_DAILY_BRIEF_TIME``) and read live by ``events.daily_brief``, so no
restart is needed. The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(daily_brief_routes.router)`` and
``app.state.daily_brief = <the DailyBriefScheduler>``. ``/run`` falls back
to ``app.state.event_bus`` when no scheduler is on ``app.state``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import user_settings
from events import daily_brief

router = APIRouter(prefix="/api/daily-brief", tags=["daily-brief"])


class DailyBriefUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    enabled: Optional[bool] = None
    time: Optional[str] = None


def _view() -> dict[str, Any]:
    locked = [
        field for field, key in (("enabled", daily_brief.ENABLED_KEY), ("time", daily_brief.TIME_KEY))
        if key in user_settings.shell_exported
    ]
    return {
        "enabled": daily_brief.brief_enabled(),
        "time": daily_brief.brief_time_text(),
        "locked_fields": locked,
    }


@router.get("")
def get_daily_brief() -> dict[str, Any]:
    return _view()


@router.put("")
def put_daily_brief(body: DailyBriefUpdate) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if body.enabled is not None:
        # Stored as "1"/"0" (never removed) so an explicit "off" sticks.
        updates[daily_brief.ENABLED_KEY] = "1" if body.enabled else "0"
    if body.time is not None:
        parsed = daily_brief.parse_time(body.time)
        if parsed is None:
            raise HTTPException(status_code=400, detail="time must be HH:MM (24-hour)")
        updates[daily_brief.TIME_KEY] = parsed.strftime("%H:%M")
    if updates:
        try:
            user_settings.write(updates)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _view()


@router.post("/run")
def run_daily_brief(request: Request) -> dict[str, Any]:
    # Sync def: FastAPI runs it in a worker thread (the sources block briefly).
    scheduler = getattr(request.app.state, "daily_brief", None)
    if scheduler is not None:
        event = scheduler.run_now()
    else:
        bus = getattr(request.app.state, "event_bus", None)
        if bus is None:
            raise HTTPException(status_code=503, detail="the daemon isn't ready yet")
        event = daily_brief.make_event(daily_brief.gather_brief(), trigger="manual")
        bus.push_threadsafe(event)
    return {"status": "ok", "unavailable": event.metadata.get("unavailable", [])}
