"""Settings → What the assistant knows (#134): the local knowledge index.

- ``GET  /api/knowledge``                      status: ``{sources: [{source, label,
  enabled, cloud, items, chunks, last_indexed, error}], folders, running,
  blocked_reason, vector, db_bytes, last_pass}``
- ``PUT  /api/knowledge/sources/{source}``     body ``{enabled?, cloud?}``. Off
  deletes that source's rows (and its cloud toggle); answers the status plus
  ``deleted``. 404 unknown source; 400 cloud on for a source that is off.
- ``GET  /api/knowledge/folders``              ``{folders}``
- ``PUT  /api/knowledge/folders``              body ``{folders: [path, ...]}`` replaces
  the list; removed folders lose their rows. 400 for a bad folder.
- ``POST /api/knowledge/index-now``            ``{started, ...status}``; 409 while a
  pass is running
- ``POST /api/knowledge/forget``               deletes the whole index, turns every
  source off; ``{deleted, ...status}``

Nothing here sends anything off the Mac. The app-wide ``AuthMiddleware``
(#27) guards every route.

Wiring (main.py): ``app.include_router(knowledge_routes.router)``,
``app.state.knowledge_index = <ai.knowledge_index.KnowledgeIndex>`` and
``app.state.knowledge_indexer = <ai.knowledge_index.KnowledgeIndexer>``
(503 without them).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ai import knowledge_index as ki

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class SourceUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    cloud: Optional[bool] = None


class FoldersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folders: list[str]


def _index(request: Request) -> ki.KnowledgeIndex:
    index = getattr(request.app.state, "knowledge_index", None)
    if index is None:
        raise HTTPException(status_code=503, detail="knowledge index not ready")
    return index


def _view(request: Request, index: ki.KnowledgeIndex) -> dict[str, Any]:
    status = index.status()
    indexer = getattr(request.app.state, "knowledge_indexer", None)
    if indexer is not None:
        status["running"] = status["running"] or indexer.running
        status["blocked_reason"] = indexer.blocked_reason
    else:
        status["blocked_reason"] = None
    return status


@router.get("")
async def get_knowledge(request: Request) -> dict[str, Any]:
    index = _index(request)
    return await asyncio.to_thread(_view, request, index)


@router.put("/sources/{source}")
async def put_source(source: str, body: SourceUpdate, request: Request) -> dict[str, Any]:
    index = _index(request)
    if source not in ki.SOURCES:
        raise HTTPException(status_code=404, detail=f"unknown source {source!r}")

    def apply() -> int:
        deleted = 0
        if body.enabled is not None:
            deleted = index.set_source_enabled(source, body.enabled)
        if body.cloud is not None:
            index.set_cloud_allowed(source, body.cloud)
        return deleted

    try:
        deleted = await asyncio.to_thread(apply)
    except ki.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**await asyncio.to_thread(_view, request, index), "deleted": deleted}


@router.get("/folders")
def get_folders(request: Request) -> dict[str, Any]:
    return {"folders": _index(request).folders()}


@router.put("/folders")
async def put_folders(body: FoldersUpdate, request: Request) -> dict[str, Any]:
    index = _index(request)
    try:
        deleted = await asyncio.to_thread(index.set_folders, body.folders)
    except ki.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**await asyncio.to_thread(_view, request, index), "deleted": deleted}


@router.post("/index-now")
async def index_now(request: Request) -> dict[str, Any]:
    index = _index(request)
    indexer = getattr(request.app.state, "knowledge_indexer", None)
    if indexer is None:
        raise HTTPException(status_code=503, detail="knowledge indexer not ready")
    if indexer.running or index.running:
        raise HTTPException(status_code=409, detail="indexing is already running")
    started = await indexer.index_now()
    return {**await asyncio.to_thread(_view, request, index), "started": started}


@router.post("/forget")
async def forget(request: Request) -> dict[str, Any]:
    index = _index(request)
    deleted = await asyncio.to_thread(index.forget_everything)
    return {**await asyncio.to_thread(_view, request, index), "deleted": deleted}
