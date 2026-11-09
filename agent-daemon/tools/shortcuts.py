"""List and run the user's macOS Shortcuts via the ``shortcuts`` CLI.

``list_shortcuts`` is read-only. ``run_shortcut`` is approval-gated: a
Shortcut can do anything, so it must never be in ``READ_ONLY_TOOL_NAMES``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

RUN_TIMEOUT_S = 60
LIST_TIMEOUT_S = 15

# Intended approval-gate membership (shared decision 5). The wave runner
# mirrors these into ``tools.READ_ONLY_TOOL_NAMES``; tests check both.
READ_ONLY = frozenset({"list_shortcuts"})
GATED = frozenset({"run_shortcut"})


def _shortcut_names() -> list[str]:
    """Return the exact names printed by ``shortcuts list`` (raises on failure)."""
    result = subprocess.run(
        ["shortcuts", "list"],
        capture_output=True, text=True, timeout=LIST_TIMEOUT_S, check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@tool
def list_shortcuts() -> dict[str, Any]:
    """List the names of the user's macOS Shortcuts, for use with run_shortcut.

    Returns:
        A dict with ``shortcuts`` (list of exact Shortcut names) and
        ``status`` ("ok" or "error").
    """
    try:
        names = _shortcut_names()
    except Exception as exc:
        return {"shortcuts": [], "status": "error", "error": str(exc)}
    return {"shortcuts": names, "status": "ok"}


@tool
def run_shortcut(name: str, input_text: str = "") -> dict[str, Any]:
    """Run one of the user's macOS Shortcuts by its exact name and return its output.

    Args:
        name: Exact Shortcut name as returned by list_shortcuts (no fuzzy match).
        input_text: Optional text passed to the Shortcut as its input.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``output`` (the Shortcut's
        stdout), and on failure ``error`` and possibly ``returncode``.
    """
    try:
        names = _shortcut_names()
    except Exception as exc:
        return {"status": "error", "output": "", "error": f"could not list shortcuts: {exc}"}
    if name not in names:
        return {
            "status": "error",
            "output": "",
            "error": f"no Shortcut named {name!r}; call list_shortcuts for exact names",
        }

    args = ["shortcuts", "run", name]
    input_path = None
    try:
        if input_text:
            # The CLI documents input only as --input-path, so pass a temp file.
            fd, input_path = tempfile.mkstemp(prefix="bot-inator-shortcut-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(input_text)
            args += ["--input-path", input_path]
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "output": "", "error": f"timed out after {RUN_TIMEOUT_S}s"}
    except Exception as exc:
        return {"status": "error", "output": "", "error": str(exc)}
    finally:
        if input_path:
            try:
                os.unlink(input_path)
            except OSError:
                pass

    if result.returncode != 0:
        return {
            "status": "error",
            "output": result.stdout,
            "returncode": result.returncode,
            "error": (result.stderr or "").strip() or f"exit code {result.returncode}",
        }
    return {"status": "ok", "output": result.stdout}
