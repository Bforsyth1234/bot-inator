"""MCP servers and their tools in Tool Manager (#69).

- ``GET    /api/mcp/servers``                      servers, tools, cap
- ``POST   /api/mcp/servers``                      add ``{name, command, args, env}``
- ``DELETE /api/mcp/servers/{name}``               stop and forget a server
- ``POST   /api/mcp/servers/{name}/restart``       restart (e.g. after a crash)
- ``PATCH  /api/mcp/servers/{name}/tools/{tool}``  ``{enabled?, runs_without_asking?}``

``{tool}`` is the server's own tool name (not the prefixed one). Env values
are write-only: responses carry ``env_keys`` only. Errors: 400 invalid
input, 404 unknown server/tool, 409 name taken / tool blocked / cap
reached, 503 when the manager isn't running.

The token check is the app-wide :class:`auth.AuthMiddleware`; this router
adds none of its own. Reads ``app.state.mcp_manager``
(:class:`ai.mcp_client.McpManager`). Wiring (main.py):
``app.include_router(mcp.router)``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ai.mcp_client import (
    McpConfigError,
    McpConflictError,
    McpManager,
    McpNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class AddServer(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class ToolFlags(BaseModel):
    enabled: Optional[bool] = None
    runs_without_asking: Optional[bool] = None


def _manager(request: Request) -> McpManager:
    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="MCP client not running")
    return manager


def _summary(manager: McpManager) -> dict[str, Any]:
    return {
        "servers": manager.list_servers(),
        "max_loaded": manager.max_loaded,
        "enabled_count": manager.enabled_count(),
        "loaded": manager.loaded_names(),
    }


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, McpNotFoundError):
        return HTTPException(status_code=404, detail=f"not found: {exc.args[0] if exc.args else ''}")
    if isinstance(exc, McpConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


_ERRORS = (McpConfigError, McpConflictError, McpNotFoundError)


@router.get("/servers")
async def list_servers(request: Request) -> dict[str, Any]:
    return _summary(_manager(request))


@router.post("/servers", status_code=201)
async def add_server(body: AddServer, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    try:
        return manager.add_server(body.name, body.command, body.args, body.env)
    except _ERRORS as exc:
        raise _http_error(exc)


@router.delete("/servers/{name}")
async def remove_server(name: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    try:
        dropped = await asyncio.to_thread(manager.remove_server, name)
    except _ERRORS as exc:
        raise _http_error(exc)
    return {"removed": name, "unloaded": dropped}


@router.post("/servers/{name}/restart")
async def restart_server(name: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    try:
        return await asyncio.to_thread(manager.restart_server, name)
    except _ERRORS as exc:
        raise _http_error(exc)


@router.patch("/servers/{name}/tools/{tool}")
async def set_tool_flags(name: str, tool: str, body: ToolFlags, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    if body.enabled is None and body.runs_without_asking is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    try:
        # Off the loop: the tools_changed hook rebuilds the agent.
        return await asyncio.to_thread(
            manager.set_tool_flags, name, tool,
            enabled=body.enabled, runs_without_asking=body.runs_without_asking,
        )
    except _ERRORS as exc:
        raise _http_error(exc)
