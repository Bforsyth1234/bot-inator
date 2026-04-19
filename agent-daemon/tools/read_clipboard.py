"""Read the current contents of the macOS clipboard."""

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
def read_clipboard() -> dict[str, Any]:
    """Read the current plain-text contents of the macOS clipboard.

    Returns:
        A dict with ``text`` (clipboard contents, possibly empty) and
        ``status`` ("ok" or "error").
    """
    try:
        result = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5, check=True
        )
    except Exception as exc:
        return {"text": "", "status": "error", "error": str(exc)}
    return {"text": result.stdout, "status": "ok"}
