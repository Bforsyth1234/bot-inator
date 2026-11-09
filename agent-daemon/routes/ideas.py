"""Review and remove remembered tool-idea denials (#56).

Declined tool ideas block reworded repeats (``ai/idea_match.py``). These
routes let the user see what is remembered and un-decline an idea:

- ``GET  /api/ideas/declined``         one row per declined name
- ``DELETE /api/ideas/declined/{name}`` drop every declined entry with it
- ``POST /api/ideas/declined/clear``   drop all declined entries

Approved entries always stay. Writes go through ``IdeaFeedback`` (atomic
tmp + replace). The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(ideas.router)`` and
``app.state.idea_feedback = idea_feedback``. The matcher is read from
``app.state.orchestrator.declined_matcher``; when ``idea_feedback`` is not
on ``app.state`` the matcher's own ``feedback`` is used.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ideas", tags=["ideas"])


def _matcher(request: Request) -> Optional[Any]:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    return getattr(orchestrator, "declined_matcher", None)


def _feedback(request: Request) -> Any:
    feedback = getattr(request.app.state, "idea_feedback", None)
    if feedback is None:
        feedback = getattr(_matcher(request), "feedback", None)
    if feedback is None:
        raise HTTPException(status_code=503, detail="idea feedback not ready")
    return feedback


def _forget(request: Request, removed: list[dict[str, Any]]) -> None:
    """Drop the removed ideas' cached embeddings so they stop matching now."""
    matcher = _matcher(request)
    if matcher is None or not removed:
        return
    try:
        matcher.forget(removed)
    except Exception:
        logger.exception("could not drop removed ideas from the matcher cache")
        try:
            matcher.clear_cache()
        except Exception:
            logger.exception("could not clear the matcher cache")


@router.get("/declined")
async def list_declined(request: Request) -> list[dict[str, Any]]:
    """Declined ideas grouped by name, most recently declined first."""
    return _feedback(request).declined_groups()


@router.post("/declined/clear")
async def clear_declined(request: Request) -> dict[str, Any]:
    """Forget every declined idea. Approved ideas stay."""
    removed = _feedback(request).clear_declined()
    _forget(request, removed)
    logger.info("Cleared %d declined idea entries", len(removed))
    return {"removed": len(removed)}


@router.delete("/declined/{name}")
async def delete_declined(name: str, request: Request) -> dict[str, Any]:
    """Un-decline ``name``: remove every declined entry with that name."""
    removed = _feedback(request).remove_declined(name)
    if not removed:
        raise HTTPException(status_code=404, detail="no declined idea with that name")
    _forget(request, removed)
    logger.info("Un-declined %r (%d entries)", name, len(removed))
    return {"name": name, "removed": len(removed)}
