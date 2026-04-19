"""Agent tools package.

Exports the list of available tools the orchestrator registers with smolagents.
"""

from __future__ import annotations

from .move_file import move_file
from .open_url import open_url
from .read_active_tab import read_active_tab
from .read_clipboard import read_clipboard
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
]

__all__ = [
    "read_active_tab",
    "open_url",
    "show_notification",
    "read_clipboard",
    "write_clipboard",
    "summarize_file",
    "move_file",
    "AVAILABLE_TOOLS",
]
