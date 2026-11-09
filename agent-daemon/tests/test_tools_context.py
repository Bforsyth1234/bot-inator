"""The drafting prompt's AVAILABLE TOOLS list must give importable lines.

Regression (wave 1): the import path was built as ``tools.<tool name>``,
so tools living in a shared module (``tools.windows`` holds
``list_windows`` and ``open_app``) were listed with an import that fails.
"""

from __future__ import annotations

import importlib
import re

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools.meta_tool_generator as mtg
from tools import AVAILABLE_TOOLS


def test_every_builtin_import_line_imports_that_tool(monkeypatch) -> None:
    monkeypatch.setattr(mtg, "_ORCHESTRATOR", None)
    context = mtg._format_available_tools_context()
    lines = re.findall(r"\(import: from (\S+) import (\w+)\)", context)
    listed = {name for _, name in lines}
    assert {"list_windows", "open_app", "create_note", "run_shortcut"} <= listed
    by_name = {t.name: t for t in AVAILABLE_TOOLS}
    for module_path, name in lines:
        module = importlib.import_module(module_path)
        assert getattr(module, name) is by_name[name], (module_path, name)


def test_shared_module_paths() -> None:
    by_name = {t.name: t for t in AVAILABLE_TOOLS}
    assert mtg._builtin_import_path("open_app", by_name["open_app"]) == "tools.windows"
    assert mtg._builtin_import_path("append_note", by_name["append_note"]) == "tools.notes"
    assert (
        mtg._builtin_import_path("show_notification", by_name["show_notification"])
        == "tools.show_notification"
    )
