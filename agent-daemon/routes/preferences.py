"""Review and remove preferences learned from approval cards (#72).

- ``GET    /api/preferences/learned``       learned preferences, newest first
- ``DELETE /api/preferences/learned/{id}``  forget one (leaves a tombstone so
  the same old decisions don't re-learn it; a stopped offer can return)

Only ``kind="preference", source="approval"`` rows are listed or deleted;
preferences the user saved by asking ("remember ...") are not touched
here. The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(preferences.router)``; the store is
``app.state.memory`` (already set).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ai import preference_learning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


def _memory(request: Request) -> Any:
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        raise HTTPException(status_code=503, detail="memory not ready")
    return memory


def _row(r: dict[str, Any]) -> dict[str, Any]:
    meta = r.get("metadata") or {}
    return {
        "id": r["id"],
        "text": r["text"],
        "rule": meta.get("rule", ""),
        "tool_name": meta.get("tool_name", ""),
        "trigger_kind": meta.get("trigger_kind", ""),
        "created_at": str(r.get("created_at") or ""),
    }


@router.get("/learned")
async def list_learned(request: Request) -> list[dict[str, Any]]:
    memory = _memory(request)
    rows = await asyncio.to_thread(preference_learning.learned, memory)
    return [_row(r) for r in rows]


@router.delete("/learned/{pref_id}")
async def delete_learned(pref_id: int, request: Request) -> dict[str, Any]:
    memory = _memory(request)
    row = await asyncio.to_thread(
        memory.delete_preference, pref_id, preference_learning.SOURCE
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no learned preference with that id")
    logger.info("Deleted learned preference %s", (row.get("metadata") or {}).get("key"))
    return {"id": pref_id, "removed": 1}
