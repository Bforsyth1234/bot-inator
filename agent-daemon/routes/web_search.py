"""Web search settings (#124).

- ``GET /api/web-search``  ``{enabled, provider, has_brave_key,
  no_key_provider, locked_fields}``
- ``PUT /api/web-search``  body ``{enabled?: bool, brave_key?: str}``; an
  empty ``brave_key`` removes the saved key.

The key is write-only: no route returns it. Settings are saved through
``user_settings`` (``AGENT_WEB_SEARCH``, ``AGENT_BRAVE_API_KEY``; the file
is 0600) and mirrored into ``os.environ``, because ``tools.web_search``
reads the environment first on every call (startup copies the file into
it), so a save takes effect at once and an "off" sticks. A key the user
exported in the daemon's shell can't be changed here (``locked_fields``).
The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(web_search_routes.router)``.
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import user_settings

router = APIRouter(prefix="/api/web-search", tags=["web-search"])

MAX_KEY_CHARS = 200


def _ws() -> Any:
    # ``tools.web_search`` as an attribute is the tool once registered; the
    # module is what holds the settings helpers.
    return importlib.import_module("tools.web_search")


class WebSearchUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    enabled: Optional[bool] = None
    brave_key: Optional[str] = None


def _view() -> dict[str, Any]:
    ws = _ws()
    locked = [
        field for field, key in (("enabled", ws.ENABLED_KEY), ("brave_key", ws.BRAVE_KEY_SETTING))
        if key in user_settings.shell_exported
    ]
    return {**ws.status(), "locked_fields": locked}


@router.get("")
def get_web_search() -> dict[str, Any]:
    return _view()


@router.put("")
def put_web_search(body: WebSearchUpdate) -> dict[str, Any]:
    ws = _ws()
    updates: dict[str, str] = {}
    if body.enabled is not None:
        # Stored as "1"/"0" (never removed) so an explicit "off" sticks.
        updates[ws.ENABLED_KEY] = "1" if body.enabled else "0"
    if body.brave_key is not None:
        key = body.brave_key.strip()
        if len(key) > MAX_KEY_CHARS or any(c.isspace() or not c.isprintable() for c in key):
            raise HTTPException(status_code=400, detail="that does not look like an API key")
        updates[ws.BRAVE_KEY_SETTING] = key  # "" removes it
    locked = sorted(k for k in updates if k in user_settings.shell_exported)
    if locked:
        raise HTTPException(
            status_code=409,
            detail=f"{', '.join(locked)} is set in the daemon's shell; unset it there first",
        )
    if updates:
        try:
            user_settings.write(updates)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        for name, value in updates.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
    return _view()
