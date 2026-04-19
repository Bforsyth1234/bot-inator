"""Open a URL in the user's default browser."""

from __future__ import annotations

import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


@tool
def open_url(url: str) -> dict[str, Any]:
    """Open a URL in the user's default browser.

    Args:
        url: The absolute URL to open (http:// or https://).

    Returns:
        A dict with ``url`` and ``status`` ("ok" or "error").
    """
    if not url.startswith(("http://", "https://")):
        return {"url": url, "status": "error", "error": "url must start with http(s)://"}
    try:
        subprocess.run(["open", url], check=True, timeout=5)
    except Exception as exc:
        return {"url": url, "status": "error", "error": str(exc)}
    return {"url": url, "status": "ok"}
