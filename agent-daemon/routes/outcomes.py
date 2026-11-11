"""Suggestion outcomes for the Debug tab (#79).

- ``GET  /api/outcomes/stats``  per-source and per-kind approval / kept
  rates, each source's current adjustment, and the approval-decision
  summary (when a ``DecisionLog`` is wired)
- ``GET  /api/outcomes/removal-candidates``  tools approved >= 30 days ago
  and never run since
- ``POST /api/outcomes/removal-candidates/{name}/keep``  stop listing one

The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(outcomes.router)``,
``app.state.idea_outcomes = <OutcomeStore>`` and, optionally,
``app.state.approval_decisions = <DecisionLog>``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/outcomes", tags=["outcomes"])


def _store(request: Request) -> Any:
    store = getattr(request.app.state, "idea_outcomes", None)
    if store is None:
        raise HTTPException(status_code=503, detail="idea outcomes not ready")
    return store


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    store = _store(request)
    log = getattr(request.app.state, "approval_decisions", None)
    return {
        "sources": store.stats_by_source(),
        "idea_kinds": store.stats_by_kind(),
        "decisions": log.summary() if log is not None else [],
    }


@router.get("/removal-candidates")
async def removal_candidates(request: Request) -> list[dict[str, Any]]:
    return _store(request).removal_candidates()


@router.post("/removal-candidates/{name}/keep")
async def keep_tool(name: str, request: Request) -> dict[str, Any]:
    if not _store(request).dismiss_removal(name):
        raise HTTPException(status_code=404, detail="no approved tool with that name")
    return {"tool_name": name, "kept": True}
