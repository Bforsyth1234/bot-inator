"""#124: the Settings route for web search, and the wave-26 wiring."""

from __future__ import annotations

import importlib
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
import tools
import user_settings
from routes import web_search as routes_ws

ws = importlib.import_module("tools.web_search")
KEY = "BSA-route-test-key-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    # setenv then delenv: monkeypatch restores (removes) what the route sets.
    for key in (ws.ENABLED_KEY, ws.BRAVE_KEY_SETTING):
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    app = FastAPI()
    app.include_router(routes_ws.router)
    with TestClient(app) as test_client:
        test_client.settings_path = tmp_path / "settings.json"
        yield test_client


def test_off_by_default_and_no_key(client):
    assert client.get("/api/web-search").json() == {
        "enabled": False, "provider": "none", "has_brave_key": False,
        "no_key_provider": False, "locked_fields": [],
    }


def test_switch_saves_takes_effect_at_once_and_off_sticks(client):
    body = client.put("/api/web-search", json={"enabled": True}).json()
    assert body["enabled"] is True and ws.is_enabled() is True
    assert json.loads(client.settings_path.read_text()) == {"AGENT_WEB_SEARCH": "1"}
    # the environment still says "1" from the save above: off must win
    body = client.put("/api/web-search", json={"enabled": False}).json()
    assert body["enabled"] is False and ws.is_enabled() is False
    assert json.loads(client.settings_path.read_text()) == {"AGENT_WEB_SEARCH": "0"}


def test_key_is_saved_used_and_never_returned(client):
    reply = client.put("/api/web-search", json={"brave_key": f"  {KEY}\n"})
    assert reply.status_code == 200
    assert KEY not in reply.text
    body = reply.json()
    assert body["has_brave_key"] is True and body["provider"] == "brave"
    assert json.loads(client.settings_path.read_text())["AGENT_BRAVE_API_KEY"] == KEY
    assert oct(client.settings_path.stat().st_mode & 0o777) == "0o600"
    assert KEY not in client.get("/api/web-search").text
    # an omitted field is left alone
    assert client.put("/api/web-search", json={"enabled": True}).json()["has_brave_key"] is True


def test_empty_key_removes_it(client):
    client.put("/api/web-search", json={"brave_key": KEY})
    body = client.put("/api/web-search", json={"brave_key": ""}).json()
    assert body["has_brave_key"] is False and body["provider"] == "none"
    assert "AGENT_BRAVE_API_KEY" not in json.loads(client.settings_path.read_text())
    assert os.environ.get("AGENT_BRAVE_API_KEY") is None


@pytest.mark.parametrize("bad", ["two words", "line\nbreak", "x" * 201, "tab\tkey"])
def test_a_malformed_key_is_refused_and_not_echoed(client, bad):
    reply = client.put("/api/web-search", json={"brave_key": bad})
    assert reply.status_code == 400
    assert bad not in reply.text
    assert client.get("/api/web-search").json()["has_brave_key"] is False


def test_a_shell_export_locks_the_field(client, monkeypatch):
    monkeypatch.setenv("AGENT_WEB_SEARCH", "1")
    monkeypatch.setattr(user_settings, "shell_exported", frozenset({"AGENT_WEB_SEARCH"}))
    assert client.get("/api/web-search").json()["locked_fields"] == ["enabled"]
    assert client.put("/api/web-search", json={"enabled": False}).status_code == 409
    assert ws.is_enabled() is True
    assert not client.settings_path.exists()


# ── wave 26 wiring ───────────────────────────────────────────────────────

NEW_TOOLS = ("web_search", "fetch_page", "recall_activity", "find_contact", "weekly_report")


def test_new_tools_are_registered_and_read_only():
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.AVAILABLE_TOOLS}
    for name in NEW_TOOLS:
        assert name in names, name
        assert name in tools.READ_ONLY_TOOL_NAMES, name
        assert name in tools.__all__, name


def test_settings_keys_are_writable():
    for key in ("AGENT_WEB_SEARCH", "AGENT_BRAVE_API_KEY", "AGENT_WEEKLY_REPORT",
                "AGENT_WEEKLY_REPORT_DAY", "AGENT_WEEKLY_REPORT_TIME"):
        assert key in user_settings.ALLOWED_KEYS, key


def test_contacts_permission_row_exists():
    from ai import permissions

    row = permissions.get("contacts")
    assert row is not None and row.name == "Contacts"
    assert "contacts" in permissions.SETTINGS_URLS


def test_weekly_report_event_makes_no_model_call(monkeypatch):
    import asyncio

    from ai.mlx_engine import MLXEngine
    from ai.orchestrator import Orchestrator
    from events.event_bus import ContextEvent, EventBus

    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    thoughts: list[tuple[str, str]] = []
    shown: list[tuple[str, str]] = []

    async def emit(event_id, kind, text):
        thoughts.append((kind, text))

    async def no_model(*_, **__):
        raise AssertionError("the weekly report must not run the model")

    monkeypatch.setattr(orch, "_emit_thought", emit)
    monkeypatch.setattr(orch, "_run_agent", no_model)
    monkeypatch.setattr(Orchestrator, "_notify_weekly_report",
                        staticmethod(lambda title, body: shown.append((title, body))))
    event = ContextEvent(event_type="weekly_report",
                         metadata={"text": "This week\nActions run: 3", "week": {}})
    asyncio.run(orch._handle_weekly_report(event, "evt_w"))
    assert thoughts == [("complete", "This week\nActions run: 3")]
    assert len(shown) == 1 and shown[0][0]
    assert Orchestrator._describe_event(event)
