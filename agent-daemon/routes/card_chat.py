"""Chat on the tool-idea and code-review cards (#130).

- ``GET  /api/card-chat/{request_id}``  the open card's thread: ``{request_id,
  kind idea|code, turns [{role, text}], plan, busy, rewriting, turns_left,
  tainted, origin_notes, can_rewrite}``. 404 unknown, 410 already closed.
- ``POST /api/card-chat/{request_id}``  body ``{text}``. Answers like ``GET``
  plus ``result`` (``answer``, ``no_approval``, ``no_deny``, ``plan_changed``,
  ``plan_refused``, ``rewrite_started``, ``rewrite_refused``) and ``reply``.
  Errors are ``{detail, error}``: 404 unknown card, 410 card closed, 409 a
  reply or rewrite is already in flight, 413 too long, 422 empty, 429 the
  thread is full, 502/503/504 the model failed / none loaded / too slow.

Nothing here approves, denies or resolves a card except the code card's
rewrite (``rewrite_started``), which resolves the old card as NOT approved
and sends its code back through the code gate (``ai.card_chat``). HTTP, not
WebSocket frames: a chat turn is request/response, only the Mac app holds
the token (the iPhone remote never sees these cards), and a new code card
still arrives as an ordinary ``code_approval_request`` frame. The app-wide
``AuthMiddleware`` (#27) guards both routes.

Wiring (main.py): ``app.include_router(card_chat_routes.router)`` and
``app.state.card_chat = <ai.card_chat.CardChatStore>`` (503 without it).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai.card_chat import CardChatError

router = APIRouter(prefix="/api/card-chat", tags=["card-chat"])


class CardChatMessage(BaseModel):
    text: str = ""


def _store(request: Request) -> Any:
    store = getattr(request.app.state, "card_chat", None)
    if store is None:
        raise CardChatError(503, "not_ready", "Card chat is not ready.")
    return store


def _error(exc: CardChatError) -> JSONResponse:
    return JSONResponse({"detail": exc.message, "error": exc.error}, status_code=exc.status)


@router.get("/{request_id}")
async def get_thread(request_id: str, request: Request) -> Any:
    try:
        store = _store(request)
        return store.live(request_id).view()
    except CardChatError as exc:
        return _error(exc)


@router.post("/{request_id}")
async def post_message(request_id: str, body: CardChatMessage, request: Request) -> Any:
    try:
        return await _store(request).ask(request_id, body.text)
    except CardChatError as exc:
        return _error(exc)
