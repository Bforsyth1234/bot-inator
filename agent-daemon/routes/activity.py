"""Activity history for routine detection (#75).

- ``GET    /api/activity``             ``{enabled, count, first_seen, last_seen,
  by_type, retention_days, max_rows, locked_fields, candidates}``
- ``PUT    /api/activity``             body ``{enabled: bool}``
- ``DELETE /api/activity``             forget every stored row

``candidates`` are the routines :func:`ai.routines.find_candidates` finds
now (title, evidence and the tool each maps to; no content is stored, so
none is returned). The switch is saved through ``user_settings``
(``AGENT_ACTIVITY_HISTORY``, "1"/"0") and mirrored into ``os.environ``,
which :func:`ai.activity_store.history_enabled` reads live. The app-wide
``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(activity_routes.router)``.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import user_settings
from ai import activity_store, routines

router = APIRouter(prefix="/api/activity", tags=["activity"])

_CANDIDATE_FIELDS = ("id", "kind", "title", "evidence", "evidence_text", "tool", "tool_args")


class ActivityUpdate(BaseModel):
    enabled: bool


def _view() -> dict[str, Any]:
    store = activity_store.default_store()
    body = store.stats()
    body["locked_fields"] = (
        ["enabled"] if activity_store.ENABLED_KEY in user_settings.shell_exported else []
    )
    try:
        cands = routines.find_candidates(store)
    except Exception:
        cands = []
    body["candidates"] = [{k: c.get(k) for k in _CANDIDATE_FIELDS} for c in cands]
    return body


@router.get("")
def get_activity() -> dict[str, Any]:
    return _view()


@router.put("")
def put_activity(body: ActivityUpdate) -> dict[str, Any]:
    key = activity_store.ENABLED_KEY
    if key in user_settings.shell_exported:
        raise HTTPException(status_code=409, detail=f"{key} is set in the daemon's shell")
    value = "1" if body.enabled else "0"
    try:
        user_settings.write({key: value})
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    os.environ[key] = value
    return _view()


@router.delete("")
def clear_activity() -> dict[str, Any]:
    removed = activity_store.clear()
    body = _view()
    body["removed"] = removed
    return body
