"""Who may call the daemon's HTTP API and WebSocket (#27).

Two checks, in this order, on every HTTP request and WS handshake:

1. ORIGIN. Browsers attach an ``Origin`` header to every cross-site
   request and to every WebSocket handshake, and a page cannot remove it.
   The menu bar app (URLSession) and scripts send none. So any request
   carrying an ``Origin`` is refused (HTTP 403 / WS close 1008) unless the
   origin is listed in ``AGENT_ALLOWED_ORIGINS`` (comma separated, empty by
   default; exact match only, there is no wildcard).
2. TOKEN. A random per-install secret in ``~/.bot-inator/token`` (0600,
   parent 0700, created on first start; ``AGENT_TOKEN_PATH`` overrides the
   path) must arrive as ``Authorization: Bearer <token>``. Missing or wrong
   → HTTP 401 / WS close 1008. Only ``GET /health`` is exempt.

The token is never logged.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

WS_POLICY_VIOLATION = 1008
_DEFAULT_TOKEN_PATH = Path.home() / ".bot-inator" / "token"
_cache: dict[str, str] = {}

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def token_path() -> Path:
    override = os.environ.get("AGENT_TOKEN_PATH", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_TOKEN_PATH


def load_or_create_token(path: Path) -> str:
    """Read the token at ``path``, creating it (0600, parent 0700) if absent."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
        return existing
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    logger.info("Created API token file at %s", path)
    return token


def get_token() -> str:
    """The token for the current ``token_path()``, loaded once per path."""
    key = str(token_path())
    if key not in _cache:
        _cache[key] = load_or_create_token(Path(key))
    return _cache[key]


def auth_header() -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}`` for local clients and tests."""
    return {"Authorization": f"Bearer {get_token()}"}


def allowed_origins() -> set[str]:
    raw = os.environ.get("AGENT_ALLOWED_ORIGINS", "")
    return {o.strip() for o in raw.split(",") if o.strip()}


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def origin_ok(scope: Scope) -> bool:
    origin = _header(scope, b"origin")
    return origin is None or origin in allowed_origins()


def token_ok(scope: Scope) -> bool:
    value = _header(scope, b"authorization") or ""
    scheme, _, presented = value.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return False
    return hmac.compare_digest(
        presented.strip().encode("utf-8"), get_token().encode("utf-8")
    )


def _exempt(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope.get("method") == "GET"
        and scope.get("path") == "/health"
    )


class AuthMiddleware:
    """Pure ASGI middleware so it covers both HTTP and WebSocket scopes."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if not origin_ok(scope):
            logger.warning("Refused %s %s: Origin not allowed",
                           scope["type"], scope.get("path"))
            await self._refuse(scope, receive, send, 403, "origin not allowed")
            return
        if not _exempt(scope) and not token_ok(scope):
            logger.warning("Refused %s %s: missing or wrong token",
                           scope["type"], scope.get("path"))
            await self._refuse(scope, receive, send, 401, "missing or invalid token")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(
        scope: Scope, receive: Receive, send: Send, status: int, detail: str
    ) -> None:
        if scope["type"] == "websocket":
            # Closing before accept rejects the handshake (HTTP 403 on the
            # wire); in-process clients see close code 1008.
            await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION,
                        "reason": detail})
            return
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        response = JSONResponse({"detail": detail}, status_code=status,
                                headers=headers)
        await response(scope, receive, send)
