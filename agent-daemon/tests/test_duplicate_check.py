"""Duplicate-tool guard in ``generate_custom_tool``.

The old rule matched on any single shared word, so every idea from the
2026-11-09 live run was blocked against an unrelated tool.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools.meta_tool_generator as mtg
from tools import AVAILABLE_TOOLS


@pytest.fixture
def real_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = SimpleNamespace(
        name="generate_custom_tool",
        description="Generate a new custom tool from a description and expected logic.",
    )
    monkeypatch.setattr(
        mtg, "_ORCHESTRATOR", SimpleNamespace(tools=[*AVAILABLE_TOOLS, meta])
    )


@pytest.mark.parametrize("name,desc", [
    ("create_invoice_template",
     "Create a template invoice file with the current date and "
     "'INVOICE_NUMBER' placeholder for the invoice number."),
    ("invoice_number_generator",
     "Automatically generate sequential invoice numbers for downloaded invoices."),
    ("file_watcher",
     "Automate the process of watching a specific file for changes and "
     "triggering actions when modified."),
    ("install_dmg", "Mount a downloaded .dmg and copy the app to /Applications."),
])
def test_live_run_ideas_are_not_duplicates(real_tools, name: str, desc: str) -> None:
    assert mtg._find_similar_existing_tool(name, desc) is None


@pytest.mark.parametrize("name,desc,expected", [
    ("clipboard_reader", "Read the clipboard", "read_clipboard"),
    ("auto_read_clipboard", "", "read_clipboard"),
    ("open_url_helper", "Open a URL in the browser", "open_url"),
])
def test_real_duplicates_are_caught(real_tools, name: str, desc: str, expected: str) -> None:
    assert mtg._find_similar_existing_tool(name, desc) == expected


def test_meta_tool_is_never_a_match(real_tools) -> None:
    assert mtg._find_similar_existing_tool(
        "generate_custom_tool", "Generate a new custom tool"
    ) is None


def test_no_orchestrator_means_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mtg, "_ORCHESTRATOR", None)
    assert mtg._find_similar_existing_tool("read_clipboard", "x") is None
