"""Permissions checklist routes (#59).

- ``GET  /api/permissions``               — every row's status (never prompts)
- ``POST /api/permissions/{id}/request``  — prompt / open Settings for one row

Both sit behind the app-wide ``AuthMiddleware`` (Origin check + bearer
token, #27) once ``main.py`` includes this router.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ai import permissions

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


@router.get("")
def list_permissions() -> dict[str, Any]:
    # Sync def: FastAPI runs it in a worker thread (checks shell out briefly).
    return {"permissions": permissions.check_all(),
            "process": permissions.process_info()}


@router.post("/{perm_id}/request")
def request_permission(perm_id: str) -> dict[str, Any]:
    # Sync def: an Automation request blocks until the user answers the dialog.
    if permissions.get(perm_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown permission {perm_id!r}")
    return permissions.request(perm_id)
