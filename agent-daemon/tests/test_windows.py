"""Tests for #47: list_windows (read-only) and open_app (approval-gated)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from ai import orchestrator as orch_mod
from tools import windows


# ---- fakes -----------------------------------------------------------


def _fake_quartz(infos, preflight=None):
    ns = SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=16,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=mock.Mock(return_value=infos),
    )
    if preflight is not None:
        ns.CGPreflightScreenCaptureAccess = lambda: preflight
    return ns


INFOS_WITH_TITLES = [
    {"kCGWindowLayer": 25, "kCGWindowOwnerName": "Control Center", "kCGWindowName": "Clock"},
    {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Numbers", "kCGWindowName": "Budget.numbers"},
    {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Mail", "kCGWindowName": "Inbox"},
    {"kCGWindowLayer": 24, "kCGWindowOwnerName": "Window Server", "kCGWindowName": "Menubar"},
]
# Without Screen Recording permission Quartz omits kCGWindowName.
INFOS_NO_TITLES = [
    {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Numbers"},
    {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Mail"},
    {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Mail"},
]


# ---- list_windows ----------------------------------------------------


def test_list_windows_normal_layer_with_titles():
    with mock.patch.object(windows, "_quartz", return_value=_fake_quartz(INFOS_WITH_TITLES, True)):
        out = windows.list_windows()
    assert out["status"] == "ok"
    assert out["windows"] == [
        {"app": "Numbers", "title": "Budget.numbers"},
        {"app": "Mail", "title": "Inbox"},
    ]
    assert out["apps"] == ["Mail", "Numbers"]
    assert out["titles_available"] is True
    assert "note" not in out


def test_list_windows_without_permission_says_so():
    with mock.patch.object(windows, "_quartz", return_value=_fake_quartz(INFOS_NO_TITLES, False)):
        out = windows.list_windows()
    assert out["status"] == "ok"
    assert out["titles_available"] is False
    assert "Screen Recording" in out["note"]
    assert all(w["title"] == "" for w in out["windows"])
    assert out["apps"] == ["Mail", "Numbers"]


def test_list_windows_no_preflight_falls_back_to_empty_titles():
    with mock.patch.object(windows, "_quartz", return_value=_fake_quartz(INFOS_NO_TITLES)):
        out = windows.list_windows()
    assert out["titles_available"] is False and "note" in out
    with mock.patch.object(windows, "_quartz", return_value=_fake_quartz(INFOS_WITH_TITLES)):
        out = windows.list_windows()
    assert out["titles_available"] is True


def test_list_windows_quartz_failure_is_an_error():
    with mock.patch.object(windows, "_quartz", side_effect=ImportError("no Quartz")):
        out = windows.list_windows()
    assert out["status"] == "error" and out["windows"] == []


# ---- open_app --------------------------------------------------------


@pytest.fixture
def app_dirs(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    system = tmp_path / "System"
    apps.mkdir()
    system.mkdir()
    (apps / "Numbers.app").mkdir()
    (system / "Calculator.app").mkdir()
    monkeypatch.setattr(windows, "APP_DIRS", (str(apps), str(tmp_path / "missing"), str(system)))
    return tmp_path


def test_open_app_unknown_name_never_runs_open(app_dirs):
    with mock.patch.object(windows.subprocess, "run") as run:
        for bad in ("Nonexistent", "", "../Numbers", "/Applications/Numbers.app", ".app"):
            out = windows.open_app(bad)
            assert out["status"] == "error", bad
    run.assert_not_called()


def test_open_app_known_name_runs_open_with_list_args(app_dirs):
    done = SimpleNamespace(returncode=0, stdout="", stderr="")
    with mock.patch.object(windows.subprocess, "run", return_value=done) as run:
        out = windows.open_app("numbers.app")
    expected = str(app_dirs / "Applications" / "Numbers.app")
    assert out == {"name": "numbers.app", "status": "ok", "path": expected}
    args, kwargs = run.call_args
    assert args[0] == ["open", "-a", expected]
    assert not kwargs.get("shell")


def test_open_app_reports_nonzero_exit(app_dirs):
    failed = SimpleNamespace(returncode=1, stdout="", stderr="LSOpenURLs failed")
    with mock.patch.object(windows.subprocess, "run", return_value=failed):
        out = windows.open_app("Calculator")
    assert out["status"] == "error" and "LSOpenURLs" in out["error"]


# ---- approval gate (shared decision 5) --------------------------------


def _approval_requested(tool_name: str, read_only: frozenset[str]) -> bool:
    """Run the orchestrator's real approval wrapper; report if it asked."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asked: list[str] = []

    async def request_approval(**kw):
        asked.append(kw["tool_name"])
        return False, None

    async def emit_thought(*_a, **_k):
        return None

    fake = SimpleNamespace(
        event_bus=SimpleNamespace(loop=loop),
        _current_event_id="evt_test",
        _turn_tool_calls=set(),
        approval_timeout=1,
        _request_approval=request_approval,
        _emit_thought=emit_thought,
        _apply_edits=orch_mod.Orchestrator._apply_edits,
    )
    stub = SimpleNamespace(name=tool_name, forward=lambda *a, **k: {"status": "ok"})
    try:
        with mock.patch.object(orch_mod, "READ_ONLY_TOOL_NAMES", read_only):
            wrapped = orch_mod.Orchestrator._wrap_tool_for_approval(fake, stub)
            wrapped.forward()
        # Drain the tool_result thought the wrapper scheduled.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.01), loop).result(timeout=2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
    return bool(asked)


def test_gate_constants():
    assert windows.list_windows.name in windows.READ_ONLY
    assert windows.open_app.name in windows.GATED
    assert not windows.READ_ONLY & windows.GATED


def test_gate_via_orchestrator_wrapper():
    read_only = frozenset(tools.READ_ONLY_TOOL_NAMES | windows.READ_ONLY)
    assert _approval_requested("open_app", read_only) is True
    assert _approval_requested("list_windows", read_only) is False


def test_gate_registered_in_tools_package():
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    if "list_windows" not in names:
        pytest.skip("windows tools not registered yet (runner registers at wave close)")
    assert "list_windows" in tools.READ_ONLY_TOOL_NAMES
    assert "open_app" in names
    assert "open_app" not in tools.READ_ONLY_TOOL_NAMES
