"""Tests for the built-in quit_app (todo #102). NSRunningApplication is faked:
no real app is ever asked to quit."""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from tests.test_notices_privacy import _LoopThread, _drain

qa = importlib.import_module("tools.quit_app")


class FakeApp:
    def __init__(self, name: str, bundle_id: str, pid: int, policy: int = 0,
                 path: str = "", terminate_ok: bool = True) -> None:
        self._name, self._bundle_id, self._pid, self._policy = name, bundle_id, pid, policy
        self._path = path or f"file:///Applications/{name}.app/"
        self._ok = terminate_ok
        self.terminated = 0

    def localizedName(self) -> str:
        return self._name

    def bundleIdentifier(self) -> str:
        return self._bundle_id

    def processIdentifier(self) -> int:
        return self._pid

    def activationPolicy(self) -> int:
        return self._policy

    def bundleURL(self) -> str:
        return self._path

    def terminate(self) -> bool:
        self.terminated += 1
        return self._ok


@pytest.fixture()
def apps(monkeypatch) -> dict[str, FakeApp]:
    running = {
        "spotify": FakeApp("Spotify", "com.spotify.client", 501),
        "finder": FakeApp("Finder", "com.apple.finder", 502),
        "loginwindow": FakeApp("loginwindow", "com.apple.loginwindow", 503),
        "menubar": FakeApp("bot-inator", "com.botinator.MenuBarUI", 504,
                           path="file:///Applications/MenuBarUI.app/"),
        "daemon": FakeApp("Python", "org.python.python", os.getpid()),
        "helper": FakeApp("Spotify Helper", "com.spotify.helper", 505, policy=2),
        "dock": FakeApp("Dock", "com.apple.dock", 506),
    }
    monkeypatch.setattr(qa, "_running_apps", lambda: list(running.values()))
    monkeypatch.delenv("TMPDIR", raising=False)
    return running


def test_quits_a_running_app_politely(apps) -> None:
    result = qa.quit_app("spotify.app")
    assert result["status"] == "ok", result
    assert (result["app"], result["bundle_id"]) == ("Spotify", "com.spotify.client")
    assert apps["spotify"].terminated == 1


def test_matches_bundle_id_and_file_name(apps) -> None:
    assert qa.quit_app("com.spotify.client")["status"] == "ok"
    assert qa.resolve_running_app("MenuBarUI")[1]["bundle_id"] == "com.botinator.MenuBarUI"


def test_refuses_an_app_that_is_not_running(apps) -> None:
    result = qa.quit_app("Numbers")
    assert result["status"] == "error" and "no running app" in result["error"]
    assert all(a.terminated == 0 for a in apps.values())


def test_background_processes_are_not_apps(apps) -> None:
    assert qa.quit_app("Spotify Helper")["status"] == "error"
    assert apps["helper"].terminated == 0


@pytest.mark.parametrize("key,name", [
    ("finder", "Finder"),
    ("loginwindow", "loginwindow"),
    ("menubar", "bot-inator"),
    ("menubar", "MenuBarUI"),
    ("daemon", "Python"),
    ("dock", "Dock"),
])
def test_refuses_protected_apps(apps, key: str, name: str) -> None:
    result = qa.quit_app(name)
    assert result["status"] == "error" and result["error"].startswith("refused"), result
    assert apps[key].terminated == 0


def test_protected_by_bundle_id_whatever_the_name(apps) -> None:
    apps["finder"]._name = "Some Finder"
    assert qa.quit_app("Some Finder")["error"].startswith("refused")
    assert apps["finder"].terminated == 0


def test_terminate_not_delivered_is_an_error(apps) -> None:
    apps["spotify"]._ok = False
    assert qa.quit_app("Spotify")["status"] == "error"


def test_inside_the_tool_sandbox_it_says_to_call_it_directly(apps, monkeypatch) -> None:
    monkeypatch.setenv("TMPDIR", "/private/var/folders/x/tool-sandbox-abc/tmp/")
    result = qa.quit_app("Spotify")
    assert result["status"] == "error" and "directly" in result["error"]
    assert apps["spotify"].terminated == 0


def test_card_note_names_the_app_and_bundle_id(apps) -> None:
    note = qa.card_note("Spotify")
    assert "Spotify" in note and "com.spotify.client" in note and "save" in note
    assert "refuse" in qa.card_note("Finder")
    assert "not running" in qa.card_note(app_name="Numbers")


def test_registered_and_approval_gated() -> None:
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    assert "quit_app" in names
    assert "quit_app" not in tools.READ_ONLY_TOOL_NAMES
    assert qa.GATED.isdisjoint(tools.READ_ONLY_TOOL_NAMES)


def test_the_card_carries_the_note_and_the_call_waits_for_approval(apps) -> None:
    """Through the real approval gate: one card, its reasoning names the
    app, and a denied card never calls terminate()."""
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus
    from ai.mlx_engine import MLXEngine

    with _LoopThread() as loop:
        bus = EventBus()
        bus.bind_loop(loop)
        orch = Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[])
        cards: list[dict[str, Any]] = []
        decision = {"approve": False}

        async def request_approval(**kwargs: Any) -> tuple[bool, None]:
            cards.append(kwargs)
            return decision["approve"], None

        orch._request_approval = request_approval  # type: ignore[method-assign]
        wrapped = orch._wrap_tool_for_approval(tools.quit_app)

        def call() -> Any:
            return asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(wrapped.forward, "Spotify"), loop).result(5)

        denied = call()
        decision["approve"] = True
        orch._turn_done = {}
        approved = call()
        _drain(loop)
    assert denied == {"skipped": True, "reason": "user_denied"}
    assert approved["status"] == "ok"
    assert len(cards) == 2 and cards[0]["tool_name"] == "quit_app"
    assert "com.spotify.client" in cards[0]["reasoning"]
    assert apps["spotify"].terminated == 1
