"""Dummy @tool that returns mock active browser tab info.

Used to validate the event → agent → tool → approval loop end-to-end without
depending on real browser automation. Replace with a real implementation in
Phase 2.
"""

from __future__ import annotations

from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        """Fallback no-op decorator when smolagents is unavailable."""
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


@tool
def read_active_tab() -> dict[str, Any]:
    """Return metadata about the currently active browser tab.

    This is a mock implementation that returns a fixed payload so the agent
    loop can be exercised without a real browser bridge.

    Returns:
        A dict with the active tab's ``url``, ``title``, and ``browser``.
    """
    return {
        "url": "https://example.atlassian.net/browse/PROJ-451",
        "title": "PROJ-451: Implement sprint report generator",
        "browser": "Google Chrome",
    }
