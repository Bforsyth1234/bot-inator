"""Weekly report: settings, the last four weeks and "Run now" (#128).

- ``GET  /api/weekly-report``        ``{enabled, weekday, time, locked_fields}``
- ``PUT  /api/weekly-report``        body ``{enabled?: bool, weekday?: "mon".."sun", time?: "HH:MM"}``
- ``GET  /api/weekly-report/week``   one week's report (``?weeks_ago=0..3``)
- ``GET  /api/weekly-report/weeks``  ``{"weeks": [...]}``, this week first, four weeks
- ``POST /api/weekly-report/run``    publish a ``weekly_report`` event now;
  ``{status, sent, report}``. An empty week publishes nothing (``sent`` false).

Every report is built by ``events.weekly_report`` from local stores: counts
only, no model call, nothing written. Settings are saved through
``user_settings`` (keys ``AGENT_WEEKLY_REPORT``, ``AGENT_WEEKLY_REPORT_DAY``,
``AGENT_WEEKLY_REPORT_TIME``) and read live, so no restart is needed. The
app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(weekly_report_routes.router)``,
``events.weekly_report.configure(journal=..., outcomes=..., decisions=...,
watches=..., generated_dir=...)`` and ``app.state.weekly_report = <the
WeeklyReportScheduler>``. ``/run`` falls back to ``app.state.event_bus``
when no scheduler is on ``app.state``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

import user_settings
from events import weekly_report

router = APIRouter(prefix="/api/weekly-report", tags=["weekly-report"])

_FIELDS = (
    ("enabled", weekly_report.ENABLED_KEY),
    ("weekday", weekly_report.DAY_KEY),
    ("time", weekly_report.TIME_KEY),
)


class WeeklyReportUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    enabled: Optional[bool] = None
    weekday: Optional[str] = None
    time: Optional[str] = None


def _view() -> dict[str, Any]:
    return {
        "enabled": weekly_report.report_enabled(),
        "weekday": weekly_report.report_weekday_text(),
        "time": weekly_report.report_time_text(),
        "locked_fields": [field for field, key in _FIELDS if key in user_settings.shell_exported],
    }


@router.get("")
def get_weekly_report_settings() -> dict[str, Any]:
    return _view()


@router.put("")
def put_weekly_report_settings(body: WeeklyReportUpdate) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if body.enabled is not None:
        # Stored as "1"/"0" (never removed) so an explicit "off" sticks.
        updates[weekly_report.ENABLED_KEY] = "1" if body.enabled else "0"
    if body.weekday is not None:
        day = weekly_report.parse_weekday(body.weekday)
        if day is None:
            raise HTTPException(status_code=400, detail="weekday must be one of mon, tue, wed, thu, fri, sat, sun")
        updates[weekly_report.DAY_KEY] = weekly_report.WEEKDAYS[day]
    if body.time is not None:
        parsed = weekly_report.parse_time(body.time)
        if parsed is None:
            raise HTTPException(status_code=400, detail="time must be HH:MM (24-hour)")
        updates[weekly_report.TIME_KEY] = parsed.strftime("%H:%M")
    if updates:
        try:
            user_settings.write(updates)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _view()


@router.get("/week")
def get_week(weeks_ago: int = Query(0, ge=0, le=weekly_report.MAX_WEEKS_AGO)) -> dict[str, Any]:
    # Sync def: FastAPI runs it in a worker thread (the stores read files).
    return weekly_report.report_for(weeks_ago)


@router.get("/weeks")
def get_weeks() -> dict[str, Any]:
    return {"weeks": weekly_report.recent_weeks()}


@router.post("/run")
def run_weekly_report(request: Request) -> dict[str, Any]:
    scheduler = getattr(request.app.state, "weekly_report", None)
    if scheduler is not None:
        payload, event = scheduler.run_now()
    else:
        bus = getattr(request.app.state, "event_bus", None)
        if bus is None:
            raise HTTPException(status_code=503, detail="the daemon isn't ready yet")
        payload, event = weekly_report.report_for(0), None
        if not payload["empty"]:
            event = weekly_report.make_event(payload, trigger="manual")
            bus.push_threadsafe(event)
    return {"status": "ok", "sent": event is not None, "report": payload}
