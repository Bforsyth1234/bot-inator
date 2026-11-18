"""Settings → Always allowed (#116): list and revoke.

- ``GET    /api/always-allow``        ``{"entries": [...]}``
- ``DELETE /api/always-allow/{key}``  revoke one (``tool|trigger``)

There is no route that ADDS an entry: only the user's "Always allow" click
on an approval card does (``Orchestrator._request_approval``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/always-allow", tags=["always-allow"])


def _store(request: Request) -> Any:
    store = getattr(request.app.state, "always_allow", None)
    if store is None:
        raise HTTPException(status_code=503, detail="always allow not ready")
    return store


@router.get("")
async def list_entries(request: Request) -> dict[str, Any]:
    return {"entries": _store(request).list()}


@router.delete("/{key:path}")
async def revoke_entry(key: str, request: Request) -> dict[str, Any]:
    if not _store(request).revoke(key):
        raise HTTPException(status_code=404, detail="no such entry")
    return {"ok": True}
