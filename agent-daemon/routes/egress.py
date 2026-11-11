"""Privacy ledger routes (#82).

- ``GET    /api/egress``        ``{rows, today, keep_text, locked_fields,
  limit_note}``; query ``since`` (ISO time) and ``limit`` (default 200)
- ``GET    /api/egress/stats``  today's totals (``day=YYYY-MM-DD`` for another)
- ``PUT    /api/egress``        body ``{keep_text: bool}``; switching it off
  also erases every kept request text
- ``DELETE /api/egress``        forget every row

The keep-text switch is saved through ``user_settings``
(``AGENT_EGRESS_KEEP_TEXT``, "1"/"0") and mirrored into ``os.environ``,
which :func:`ai.egress_log.keep_text_enabled` reads live. The app-wide
``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(egress_routes.router)``;
``user_settings.ALLOWED_KEYS`` gains ``AGENT_EGRESS_KEEP_TEXT``.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import user_settings
from ai import egress_log

router = APIRouter(prefix="/api/egress", tags=["egress"])

MAX_LIMIT = 1000


class EgressUpdate(BaseModel):
    keep_text: bool


def _locked() -> list[str]:
    return ["keep_text"] if egress_log.KEEP_TEXT_KEY in user_settings.shell_exported else []


def _parse_since(since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    try:
        return datetime.fromisoformat(since)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="since must be an ISO time") from exc


def _view(since: Optional[datetime] = None, limit: int = 200) -> dict[str, Any]:
    log = egress_log.default_log()
    return {
        "rows": log.rows(since=since, limit=limit),
        "today": log.stats(),
        "keep_text": log.keep_text,
        "locked_fields": _locked(),
        "limit_note": egress_log.LIMIT_NOTE,
    }


@router.get("")
def get_egress(
    since: Optional[str] = None, limit: int = Query(200, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    return _view(_parse_since(since), limit)


@router.get("/stats")
def get_stats(day: Optional[str] = None) -> dict[str, Any]:
    if day is not None:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD") from exc
    return egress_log.default_log().stats(day)


@router.put("")
def put_egress(body: EgressUpdate) -> dict[str, Any]:
    key = egress_log.KEEP_TEXT_KEY
    if key in user_settings.shell_exported:
        raise HTTPException(status_code=409, detail=f"{key} is set in the daemon's shell")
    value = "1" if body.keep_text else "0"
    try:
        user_settings.write({key: value})
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    os.environ[key] = value
    if not body.keep_text:
        egress_log.default_log().forget_text()
    return _view()


@router.delete("")
def clear_egress() -> dict[str, Any]:
    removed = egress_log.clear()
    body = _view()
    body["removed"] = removed
    return body
