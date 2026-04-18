"""Agent tools package.

Exports the list of available tools the orchestrator registers with smolagents.
"""

from __future__ import annotations

from .read_active_tab import read_active_tab

AVAILABLE_TOOLS = [read_active_tab]

__all__ = ["read_active_tab", "AVAILABLE_TOOLS"]
