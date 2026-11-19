"""The agent never runs the same tool call twice in one turn (#129).

Small local models (Qwen3.5-4B in the under-24 GB set) often pick the
right tool, get its result, then emit the identical call again instead of
answering: ``open_url > open_url``. Each side-effecting repeat would be a
second approval card. :class:`NoRepeatToolCallingAgent` skips a call whose
tool and arguments match one already made in this run, and tells the model
to answer from the result it already has. ``final_answer`` is never
skipped. The approval gate is untouched: a skipped call never reaches it,
and every call that does run still goes through it (invariant 7a).
"""
from __future__ import annotations

import json
from typing import Any

REPEAT_NOTE = (
    "You already called {tool} with these arguments in this turn; its result "
    "is above. Do not call it again. Give your final answer now."
)


def call_key(tool_name: str, arguments: Any) -> str:
    """A stable key for one call: tool name plus its arguments."""
    try:
        args = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = repr(arguments)
    return f"{tool_name}:{args}"


def _make_class() -> type:
    from smolagents import ToolCallingAgent  # type: ignore

    class NoRepeatToolCallingAgent(ToolCallingAgent):
        """``ToolCallingAgent`` that skips identical repeat calls in a run."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._calls_made: set[str] = set()
            self.repeats_skipped = 0

        def run(self, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("reset", True):
                self._calls_made = set()
                self.repeats_skipped = 0
            return super().run(*args, **kwargs)

        def execute_tool_call(self, tool_name: str, arguments: Any) -> Any:
            if tool_name == "final_answer":
                return super().execute_tool_call(tool_name, arguments)
            key = call_key(tool_name, arguments)
            if key in self._calls_made:
                self.repeats_skipped += 1
                return REPEAT_NOTE.format(tool=tool_name)
            result = super().execute_tool_call(tool_name, arguments)
            # Recorded only once the call ran: a failed call may be retried.
            self._calls_made.add(key)
            return result

    return NoRepeatToolCallingAgent


_cls: type | None = None


def agent_class() -> type:
    """The agent class (built lazily so smolagents stays an optional import)."""
    global _cls
    if _cls is None:
        _cls = _make_class()
    return _cls
