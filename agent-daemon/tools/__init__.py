"""Agent tools package.

Exports the list of available tools the orchestrator registers with smolagents.
"""

from __future__ import annotations

from .meta_tool_generator import generate_custom_tool
from .move_file import move_file
from .open_url import open_url
from .read_active_tab import read_active_tab
from .read_clipboard import read_clipboard
from .remember_preference import remember_preference
from .send_imessage import send_imessage
from .show_notification import show_notification
from .summarize_file import summarize_file
from .write_clipboard import write_clipboard

AVAILABLE_TOOLS = [
    read_active_tab,
    open_url,
    show_notification,
    read_clipboard,
    write_clipboard,
    summarize_file,
    move_file,
    remember_preference,
    generate_custom_tool,
]

# Tools whose side-effects are limited to reading local state (no file
# writes, no network, no notifications). The orchestrator invokes these
# without requesting user approval; see ``Orchestrator._wrap_tool_for_approval``.
# ``remember_preference`` writes only to the user's own local memory DB, so
# it also bypasses the approval gate.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "read_active_tab",
    "read_clipboard",
    "summarize_file",
    "remember_preference",
})

__all__ = [
    "read_active_tab",
    "open_url",
    "show_notification",
    "read_clipboard",
    "write_clipboard",
    "summarize_file",
    "move_file",
    "send_imessage",
    "remember_preference",
    "generate_custom_tool",
    "AVAILABLE_TOOLS",
    "READ_ONLY_TOOL_NAMES",
]
