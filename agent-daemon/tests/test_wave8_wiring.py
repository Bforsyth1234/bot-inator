"""Wave 8 runner wiring: routers included in main.app, tool registered."""

from __future__ import annotations

import os

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import ai  # noqa: E402,F401  - must precede ``tools`` (circular import)


def test_wave8_routes_answer_through_main_app() -> None:
    """FastAPI 0.162 keeps included routers as one entry in ``app.routes``,
    so check the routes by calling them through the real app and token."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    with patch("ai.orchestrator.Orchestrator._build_agent", return_value=None):
        import main
        from auth import auth_header

        with TestClient(main.app, headers=auth_header()) as client:
            assert client.get("/api/ideas/declined").status_code == 200
            assert client.get("/api/permissions").status_code == 200
            assert client.get("/api/tools/find_files").status_code == 200
            assert client.get("/api/tools/no_such_tool").status_code == 404
        with TestClient(main.app) as anon:
            assert anon.get("/api/permissions").status_code == 401


def test_read_active_tab_text_is_registered_read_only() -> None:
    from tools import AVAILABLE_TOOLS, READ_ONLY_TOOL_NAMES

    assert "read_active_tab_text" in {t.name for t in AVAILABLE_TOOLS}
    assert "read_active_tab_text" in READ_ONLY_TOOL_NAMES
