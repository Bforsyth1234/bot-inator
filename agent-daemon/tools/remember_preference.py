"""Persist a user preference into the agent's long-term memory."""

from __future__ import annotations

from typing import Any

from ai.memory import save_memory

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


@tool
def remember_preference(preference: str) -> dict[str, Any]:
    """Save a short user preference into the agent's long-term memory.

    Use this when the user expresses a stable fact or preference that should
    survive restarts (e.g. "I prefer meetings in the afternoon", "my boss is
    named Sam"). The text is embedded and stored locally in
    ``~/.bot-inator/memory.db``; later events will see the most relevant
    saved preferences injected into the prompt.

    Args:
        preference: A single short sentence describing the preference.

    Returns:
        A dict with ``status`` ("ok" or "error") and the stored ``id`` when
        the write succeeded.
    """
    text = (preference or "").strip()
    if not text:
        return {"status": "error", "error": "empty preference"}
    try:
        rowid = save_memory(text, metadata={"kind": "preference"})
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "id": rowid}
