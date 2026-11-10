"""Agent tools package.

Exports the list of available tools the orchestrator registers with smolagents.
"""

from __future__ import annotations

from .brief import brief_me
from .create_event import create_event
from .email_draft import draft_email
from .find_files import find_files
from .list_events import list_events
from .meta_tool_generator import generate_custom_tool
from .move_file import move_file
from .notes import append_note, create_note
from .open_url import open_url
from .read_active_tab import read_active_tab, read_active_tab_text
from .read_clipboard import read_clipboard
from .read_email import read_email
from .reminders import create_reminder, list_reminders
from .remember_preference import remember_preference
from .send_imessage import send_imessage
from .shortcuts import list_shortcuts, run_shortcut
from .show_notification import show_notification
from .summarize_file import summarize_file
from .windows import list_windows, open_app
from .write_clipboard import write_clipboard

AVAILABLE_TOOLS = [
    read_active_tab,
    read_active_tab_text,
    open_url,
    show_notification,
    read_clipboard,
    write_clipboard,
    summarize_file,
    move_file,
    remember_preference,
    find_files,
    list_shortcuts,
    run_shortcut,
    create_note,
    append_note,
    list_windows,
    open_app,
    read_email,
    draft_email,
    brief_me,
    list_events,
    create_event,
    list_reminders,
    create_reminder,
    generate_custom_tool,
]

# Tools whose side-effects are limited to reading local state (no file
# writes, no network, no notifications). The orchestrator invokes these
# without requesting user approval; see ``Orchestrator._wrap_tool_for_approval``.
# ``remember_preference`` writes only to the user's own local memory DB, so
# it also bypasses the approval gate.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "read_active_tab",
    "read_active_tab_text",
    "read_clipboard",
    "summarize_file",
    "remember_preference",
    "find_files",
    "list_shortcuts",
    "list_windows",
    "read_email",
    "list_events",
    "list_reminders",
    "brief_me",
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
    "find_files",
    "list_shortcuts",
    "run_shortcut",
    "create_note",
    "append_note",
    "list_windows",
    "open_app",
    "read_email",
    "draft_email",
    "brief_me",
    "list_events",
    "create_event",
    "list_reminders",
    "create_reminder",
    "generate_custom_tool",
    "AVAILABLE_TOOLS",
    "READ_ONLY_TOOL_NAMES",
]
