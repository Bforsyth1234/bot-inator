"""Installed-tool health and rollback for Tool detail (#90).

- ``GET  /api/tool-health``                     every logged tool's health
- ``GET  /api/tools/{name}/health``             one tool: status, recent
  failures (error class, first line, repairable or why not), ok-run count,
  repair cooldown, and ``can_restore`` (a prior committed version exists)
- ``POST /api/tools/{name}/restore-previous``   put back the file's prior
  committed version, commit, reload. A user click, no card (#90 decision 5)

The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(tool_health.router)``,
``app.state.tool_health = <ai.tool_health.ToolHealthLog>`` (the same object
as ``orchestrator.tool_health``). Reads ``app.state.orchestrator`` and
``app.state.generated_tools_dir`` (optional; defaults to
``config.settings.generated_tools_dir``).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["tool-health"])

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _log(request: Request) -> Any:
    log = getattr(request.app.state, "tool_health", None)
    if log is None:
        log = getattr(getattr(request.app.state, "orchestrator", None), "tool_health", None)
    if log is None:
        raise HTTPException(status_code=503, detail="tool health not ready")
    return log


def _generated_dir(request: Request) -> Path:
    override = getattr(request.app.state, "generated_tools_dir", None)
    if override is not None:
        return Path(override)
    from config import settings

    return Path(settings.generated_tools_dir)


def _checked(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise HTTPException(status_code=404, detail=f"no tool named {name!r}")
    return name


@router.get("/api/tool-health")
async def all_health(request: Request) -> dict[str, Any]:
    return {"tools": _log(request).all_health()}


@router.get("/api/tools/{name}/health")
async def tool_health(name: str, request: Request) -> dict[str, Any]:
    from tools.meta_tool_generator import previous_version

    _checked(name)
    body = dict(_log(request).health(name))
    found = await asyncio.to_thread(previous_version, _generated_dir(request), name)
    body["can_restore"] = found is not None
    return body


@router.post("/api/tools/{name}/restore-previous")
async def restore_previous(name: str, request: Request) -> dict[str, Any]:
    from tools.meta_tool_generator import restore_previous_version

    _checked(name)
    orch = getattr(request.app.state, "orchestrator", None)
    result = await asyncio.to_thread(
        restore_previous_version, name,
        generated_dir=_generated_dir(request), orchestrator=orch,
    )
    if result.get("status") != "ok":
        error = str(result.get("error") or "restore failed")
        code = 404 if error == "no previous version" else 500
        raise HTTPException(status_code=code, detail=error)
    return result
