"""Screen watching: the Settings switch and timed sessions (todo #147).

- ``GET  /api/screen-watch``        :func:`events.screen_watcher.status` plus
  ``locked_fields`` and ``route`` (which vision model would see the images,
  or why none may)
- ``PUT  /api/screen-watch``        body ``{enabled: bool}``; turning it off
  also stops a running session
- ``POST /api/screen-watch/start``  body ``{minutes}`` (15, 60 or 240)
- ``POST /api/screen-watch/stop``

The switch is saved through ``user_settings`` (``AGENT_SCREEN_WATCH``) and
mirrored into ``os.environ``. Sessions live in the daemon's memory only.
The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(screen_watch_routes.router)``.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import user_settings
from events import screen_watcher

router = APIRouter(prefix="/api/screen-watch", tags=["screen-watch"])


class ScreenWatchUpdate(BaseModel):
    enabled: bool


class ScreenWatchStart(BaseModel):
    minutes: int


def _route_view() -> dict[str, Any]:
    try:
        from ai import vision_engine

        r = vision_engine.route()
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "kind": "", "describe": "", "refused": str(exc)}
    return {"ok": r.ok, "kind": r.kind, "describe": r.describe() if r.ok else "",
            "refused": r.refused}


def _view() -> dict[str, Any]:
    body = screen_watcher.status()
    body["locked_fields"] = (
        ["enabled"] if screen_watcher.ENABLED_KEY in user_settings.shell_exported else []
    )
    body["route"] = _route_view()
    return body


@router.get("")
def get_screen_watch() -> dict[str, Any]:
    return _view()


@router.put("")
def put_screen_watch(body: ScreenWatchUpdate) -> dict[str, Any]:
    key = screen_watcher.ENABLED_KEY
    if key in user_settings.shell_exported:
        raise HTTPException(status_code=409, detail=f"{key} is set in the daemon's shell")
    value = "1" if body.enabled else "0"
    try:
        user_settings.write({key: value})
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    os.environ[key] = value
    if not body.enabled:
        screen_watcher.stop_session()
    return _view()


@router.post("/start")
def start_screen_watch(body: ScreenWatchStart) -> dict[str, Any]:
    try:
        screen_watcher.start_session(body.minutes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view()


@router.post("/stop")
def stop_screen_watch() -> dict[str, Any]:
    screen_watcher.stop_session()
    return _view()
