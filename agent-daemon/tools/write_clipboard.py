"""Write plain text to the macOS clipboard."""

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
def write_clipboard(text: str) -> dict[str, Any]:
    """Replace the macOS clipboard with the given plain text.

    Args:
        text: The text to place on the clipboard.

    Returns:
        A dict with ``chars_written`` and ``status`` ("ok" or "error").
    """
    try:
        subprocess.run(
            ["pbcopy"], input=text, text=True, timeout=5, check=True
        )
    except Exception as exc:
        return {"chars_written": 0, "status": "error", "error": str(exc)}
    return {"chars_written": len(text), "status": "ok"}
