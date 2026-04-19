"""Post a macOS user notification via AppleScript."""

from __future__ import annotations

import json
import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


@tool
def show_notification(title: str, body: str) -> dict[str, Any]:
    """Post a macOS user notification shown in Notification Center.

    Args:
        title: Short title for the notification.
        body: Main message body.

    Returns:
        A dict with ``status`` ("ok" or "error").
    """
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=5)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok"}
