"""iPhone remote settings (#85).

- ``GET /api/remote``  ``{enabled, handle, idle_minutes, min_idle_minutes,
  max_idle_minutes, timeout_minutes, max_per_hour, locked_fields}``
- ``PUT /api/remote``  body ``{enabled?: bool, handle?: str, idle_minutes?: int}``

Saved through ``user_settings`` (keys ``AGENT_REMOTE``, ``AGENT_REMOTE_HANDLE``,
``AGENT_REMOTE_IDLE_MINUTES``; the runner adds them to ``ALLOWED_KEYS``) and
read live by ``events.remote_channel``. Off by default (invariant 7c). It
cannot be turned on without a valid own handle.

Wiring (main.py): ``app.include_router(remote_routes.router)``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import user_settings
from events import remote_channel as rc

router = APIRouter(prefix="/api/remote", tags=["remote"])


class RemoteUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    enabled: Optional[bool] = None
    handle: Optional[str] = None
    idle_minutes: Optional[int] = None


def _view() -> dict[str, Any]:
    locked = [
        field for field, key in (
            ("enabled", rc.ENABLED_KEY), ("handle", rc.HANDLE_KEY),
            ("idle_minutes", rc.IDLE_MINUTES_KEY),
        )
        if key in user_settings.shell_exported
    ]
    return {
        "enabled": rc.remote_enabled() and bool(rc.own_handle()),
        "handle": rc.own_handle(),
        "idle_minutes": rc.idle_minutes(),
        "min_idle_minutes": rc.MIN_IDLE_MINUTES,
        "max_idle_minutes": rc.MAX_IDLE_MINUTES,
        "timeout_minutes": int(rc.REMOTE_TIMEOUT_S // 60),
        "max_per_hour": rc.MAX_PER_HOUR,
        "locked_fields": locked,
    }


@router.get("")
def get_remote() -> dict[str, Any]:
    return _view()


@router.put("")
def put_remote(body: RemoteUpdate) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if body.handle is not None:
        if body.handle.strip() == "":
            updates[rc.HANDLE_KEY] = ""
            updates[rc.ENABLED_KEY] = "0"
        else:
            handle = rc.normalize_handle(body.handle)
            if not handle:
                raise HTTPException(
                    status_code=400,
                    detail="handle must be your phone number (+15551234567) or Apple ID email",
                )
            updates[rc.HANDLE_KEY] = handle
    if body.idle_minutes is not None:
        minutes = rc.parse_idle_minutes(body.idle_minutes)
        if minutes is None:
            raise HTTPException(
                status_code=400,
                detail=f"idle_minutes must be {rc.MIN_IDLE_MINUTES}-{rc.MAX_IDLE_MINUTES}",
            )
        updates[rc.IDLE_MINUTES_KEY] = str(minutes)
    if body.enabled is not None:
        handle = updates.get(rc.HANDLE_KEY, rc.own_handle())
        if body.enabled and not handle:
            raise HTTPException(status_code=400, detail="set your own handle first")
        # Stored as "1"/"0" (never removed) so an explicit "off" sticks.
        updates[rc.ENABLED_KEY] = "1" if body.enabled else "0"
    if updates:
        try:
            user_settings.write(updates)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _view()
