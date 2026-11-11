"""Right-moment delivery (#87): Pause, held offers, the Inbox, the cap.

- ``GET    /api/delivery``                 ``{paused, paused_until, held_count,
  held_reasons, held, inbox_count, inbox, cards_today, daily_cap, checks,
  debug_line}`` (``checks``: each busy source's last result, for Debug)
- ``POST   /api/delivery/pause``           body ``{duration: "1h" | "tomorrow"
  | "indefinite"}`` or ``{minutes: int}``
- ``POST   /api/delivery/resume``
- ``GET    /api/delivery/inbox``           offers over today's cap
- ``POST   /api/delivery/inbox/{id}/open`` show it now (pushed back on the
  bus marked user-caused, so neither the cap nor the busy gate applies)
- ``DELETE /api/delivery/inbox/{id}``      dismiss it
- ``GET    /api/delivery/settings``        ``{daily_cap, never_watch,
  never_watch_seed}``
- ``PUT    /api/delivery/settings``        body ``{daily_cap?, never_watch?}``
  (``never_watch`` is the whole list; a seed left out is removed)

The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(delivery_routes.router)``,
``app.state.delivery = <ai.delivery.Delivery>`` and ``app.state.event_bus``.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ai import delivery as delivery_mod

router = APIRouter(prefix="/api/delivery", tags=["delivery"])


class PauseBody(BaseModel):
    duration: Optional[Literal["1h", "tomorrow", "indefinite"]] = None
    minutes: Optional[int] = Field(default=None, ge=1, le=7 * 24 * 60)


class SettingsBody(BaseModel):
    daily_cap: Optional[int] = Field(default=None, ge=0, le=delivery_mod.MAX_DAILY_CAP)
    never_watch: Optional[list[str]] = Field(default=None, max_length=delivery_mod.MAX_NEVER_WATCH)


def _delivery(request: Request) -> Any:
    found = getattr(request.app.state, "delivery", None)
    if found is None:
        raise HTTPException(status_code=503, detail="delivery not ready")
    return found


def _view(d: Any) -> dict[str, Any]:
    body = d.status()
    body["debug_line"] = d.debug_line()
    return body


@router.get("")
def get_delivery(request: Request) -> dict[str, Any]:
    return _view(_delivery(request))


@router.post("/pause")
def pause(body: PauseBody, request: Request) -> dict[str, Any]:
    d = _delivery(request)
    d.pause(body.duration or "indefinite", minutes=body.minutes)
    return _view(d)


@router.post("/resume")
def resume(request: Request) -> dict[str, Any]:
    d = _delivery(request)
    d.resume()
    return _view(d)


@router.get("/inbox")
def inbox(request: Request) -> list[dict[str, Any]]:
    return _delivery(request).status()["inbox"]


@router.post("/inbox/{offer_id}/open")
async def open_offer(offer_id: str, request: Request) -> dict[str, Any]:
    d = _delivery(request)
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="event bus not ready")
    event = d.open_inbox(offer_id)
    if event is None:
        raise HTTPException(status_code=404, detail="no such offer in the inbox")
    await bus.push(event)
    return {"id": offer_id, "opened": True}


@router.delete("/inbox/{offer_id}")
def dismiss_offer(offer_id: str, request: Request) -> dict[str, Any]:
    if not _delivery(request).dismiss_inbox(offer_id):
        raise HTTPException(status_code=404, detail="no such offer in the inbox")
    return {"id": offer_id, "dismissed": True}


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return _delivery(request).settings.view()


@router.put("/settings")
def put_settings(body: SettingsBody, request: Request) -> dict[str, Any]:
    settings = _delivery(request).settings
    try:
        settings.update(daily_cap=body.daily_cap, never_watch=body.never_watch)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return settings.view()
