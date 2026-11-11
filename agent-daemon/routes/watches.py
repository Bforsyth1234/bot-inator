""""Watch for…" routes (#88). Creating or changing a watch is a USER action
through these routes; no tool the model can call reaches them.

- ``POST   /api/watches/preview``  ``{"sentence": "..."}`` (one model call)
  or ``{"spec": {...}}`` -> ``{spec, words, backtest}`` or ``{error, problems}``.
  Nothing is saved.
- ``POST   /api/watches``          ``{"spec", "sentence"?, "daily_limit"?,
  "cooldown_minutes"?}`` -> the saved watch (409 at the 20-watch limit,
  422 when the spec is outside the allow-lists)
- ``GET    /api/watches``          saved watches (watches denied 3 times in a
  row are switched off first)
- ``PATCH  /api/watches/{id}``     ``{"enabled"?, "daily_limit"?, "cooldown_minutes"?}``
- ``DELETE /api/watches/{id}``

The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(watches.router)``;
``app.state.watches = <ai.watches.WatchStore>``,
``app.state.watch_compiler = <ai.watches.Compiler>`` and optionally
``app.state.approval_decisions = <DecisionLog>`` (already set) and
``app.state.watch_backtest_sources = <BacktestSources>`` (tests).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ai import watches

router = APIRouter(prefix="/api/watches", tags=["watches"])


class PreviewBody(BaseModel):
    sentence: str = ""
    spec: Optional[dict[str, Any]] = None


class SaveBody(BaseModel):
    spec: dict[str, Any]
    sentence: str = ""
    daily_limit: int = watches.DEFAULT_DAILY_LIMIT
    cooldown_minutes: int = watches.DEFAULT_COOLDOWN_MINUTES


class PatchBody(BaseModel):
    enabled: Optional[bool] = None
    daily_limit: Optional[int] = None
    cooldown_minutes: Optional[int] = None


def _store(request: Request) -> watches.WatchStore:
    store = getattr(request.app.state, "watches", None)
    if store is None:
        raise HTTPException(status_code=503, detail="watches not ready")
    return store


async def _backtest(request: Request, spec: dict[str, Any]) -> dict[str, Any]:
    sources = getattr(request.app.state, "watch_backtest_sources", None)
    return await asyncio.to_thread(watches.backtest, spec, sources)


@router.post("/preview")
async def preview(body: PreviewBody, request: Request) -> dict[str, Any]:
    if body.spec is not None:
        try:
            spec = watches.validate_spec(body.spec)
        except watches.SpecError as exc:
            return {"error": "That watch is outside what a watch may do.", "problems": exc.problems}
    else:
        compiler = getattr(request.app.state, "watch_compiler", None)
        if compiler is None:
            raise HTTPException(status_code=503, detail="watch compiler not ready")
        got = await compiler.compile(body.sentence)
        if "spec" not in got:
            return got
        spec = got["spec"]
    return {"spec": spec, "words": watches.describe(spec), "backtest": await _backtest(request, spec)}


@router.get("")
async def list_watches(request: Request) -> list[dict[str, Any]]:
    store = _store(request)
    store.apply_denials(getattr(request.app.state, "approval_decisions", None))
    return [store.public(w) for w in store.list()]


@router.post("")
async def save_watch(body: SaveBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    try:
        watch = store.add(body.spec, body.sentence, daily_limit=body.daily_limit,
                          cooldown_minutes=body.cooldown_minutes)
    except watches.SpecError as exc:
        raise HTTPException(status_code=422, detail={"problems": exc.problems})
    except watches.WatchLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return store.public(watch)


@router.patch("/{watch_id}")
async def patch_watch(watch_id: str, body: PatchBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    watch = store.update(watch_id, enabled=body.enabled, daily_limit=body.daily_limit,
                         cooldown_minutes=body.cooldown_minutes)
    if watch is None:
        raise HTTPException(status_code=404, detail="no watch with that id")
    return store.public(watch)


@router.delete("/{watch_id}")
async def delete_watch(watch_id: str, request: Request) -> dict[str, Any]:
    if not _store(request).delete(watch_id):
        raise HTTPException(status_code=404, detail="no watch with that id")
    return {"id": watch_id, "removed": 1}
