"""Action history and undo (#83).

- ``GET  /api/actions?limit=200``  journaled actions, newest first: what ran,
  when, why, and whether it can still be undone
- ``POST /api/actions/{entry_id}/undo``  reverse one entry. The user's click
  (no approval card, LIVING_SPEC §1); the journal checks the item is
  unchanged first. Answers ``{status, message, entry}``; ``undone`` is 200,
  ``not_found`` is 404, every other refusal is 409 with the same body.

The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(actions.router)`` and
``app.state.action_journal = <ai.action_journal.ActionJournal>``.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/actions", tags=["actions"])


def _journal(request: Request) -> Any:
    journal = getattr(request.app.state, "action_journal", None)
    if journal is None:
        raise HTTPException(status_code=503, detail="action journal not ready")
    return journal


@router.get("")
async def list_actions(request: Request, limit: int = 200) -> list[dict[str, Any]]:
    return _journal(request).rows(limit)


@router.post("/{entry_id}/undo")
async def undo_action(entry_id: str, request: Request) -> JSONResponse:
    journal = _journal(request)
    # The undo may ask the menu bar app (calendar bridge), which must not
    # run on the event loop.
    result = await asyncio.to_thread(journal.undo, entry_id)
    code = {"undone": 200, "not_found": 404}.get(result["status"], 409)
    return JSONResponse(result, status_code=code)
