"""Tests for #89: read_front_window (read-only) through the menu bar app."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from ai import orchestrator as orch_mod
from schemas import ws_messages as wsm
from tools import read_front_window as rfw


class FakeBridge:
    """Stands in for the WS bridge: records requests, returns ``reply``."""

    def __init__(self, reply: Optional[dict[str, Any]] = None, *,
                 connected: bool = True, trusted: Optional[bool] = True,
                 raises: Optional[Exception] = None) -> None:
        self.reply = reply or {}
        self._connected = connected
        self._trusted = trusted
        self.raises = raises
        self.calls: list[tuple[int, list[str]]] = []

    def connected(self) -> bool:
        return self._connected

    def accessibility_trusted(self) -> Optional[bool]:
        return self._trusted if self._connected else None

    def request(self, max_chars: int, never_watch: list[str], timeout: float = 2.0):
        self.calls.append((max_chars, list(never_watch)))
        if self.raises is not None:
            raise self.raises
        return {"request_id": "x", **self.reply}


def _ok(text: str = "Quarterly plan\nRevenue is up.", **extra: Any) -> dict[str, Any]:
    return {"ok": True, "app": "Pages", "bundle_id": "com.apple.iWork.Pages",
            "title": "Plan.pages", "text": text, "truncated": False, **extra}


@pytest.fixture
def chat_turn():
    rfw.set_turn_kind_provider(lambda: "chat")
    yield
    rfw.set_turn_kind_provider(None)


@pytest.fixture
def use(monkeypatch, chat_turn):
    def install(fake: FakeBridge) -> FakeBridge:
        monkeypatch.setattr(rfw, "bridge", fake)
        return fake
    return install


# ---- text returned and capped ----------------------------------------------

def test_text_returned(use) -> None:
    fake = use(FakeBridge(_ok()))
    out = rfw.read_front_window_impl()
    assert out == {"app": "Pages", "title": "Plan.pages",
                   "text": "Quarterly plan\nRevenue is up.", "truncated": False}
    assert fake.calls[0][0] == rfw.DEFAULT_MAX_CHARS


def test_text_capped_at_8000(use) -> None:
    fake = use(FakeBridge(_ok("x" * 12_000)))
    out = rfw.read_front_window_impl(max_chars=50_000)
    assert len(out["text"]) == 8000 and out["truncated"] is True
    assert fake.calls[0][0] == 8000  # the request asks for the cap too


def test_smaller_limit_and_app_truncation_flag(use) -> None:
    use(FakeBridge(_ok("abcdefghij")))
    out = rfw.read_front_window_impl(max_chars=4)
    assert out["text"] == "abcd" and out["truncated"] is True
    use(FakeBridge(_ok("short", truncated=True)))
    assert rfw.read_front_window_impl()["truncated"] is True


def test_tool_object_runs(use) -> None:
    use(FakeBridge(_ok()))
    out = rfw.read_front_window(max_chars=100)
    assert out["text"].startswith("Quarterly")


def test_empty_text_is_nothing_readable(use) -> None:
    use(FakeBridge(_ok("   \n  ")))
    out = rfw.read_front_window_impl()
    assert out["text"] == "" and out["reason"] == "nothing_readable"
    assert out["error"] == rfw.NOTHING_READABLE
    assert out["app"] == "Pages"


# ---- distinct errors -------------------------------------------------------

def test_app_not_connected_and_accessibility_off_are_distinct(use) -> None:
    not_connected = use(FakeBridge(connected=False))
    a = rfw.read_front_window_impl()
    assert not_connected.calls == []
    off = use(FakeBridge(trusted=False))
    b = rfw.read_front_window_impl()
    assert off.calls == []  # known off: the app isn't asked
    assert a["reason"] == "app_not_connected" and a["error"] == rfw.APP_NOT_CONNECTED
    assert b["reason"] == "accessibility_off" and b["error"] == rfw.ACCESSIBILITY_OFF
    assert a["error"] != b["error"]
    assert a["text"] == b["text"] == ""


def test_accessibility_off_reported_by_app(use) -> None:
    use(FakeBridge({"ok": False, "error_code": "accessibility_off",
                    "error": "AX off", "accessibility_trusted": False}))
    out = rfw.read_front_window_impl()
    assert out["reason"] == "accessibility_off" and out["error"] == rfw.ACCESSIBILITY_OFF


def test_timeout_is_its_own_error(use) -> None:
    use(FakeBridge(raises=rfw.ScreenTextError("timeout", rfw.TIMEOUT)))
    out = rfw.read_front_window_impl()
    assert out["reason"] == "timeout" and out["text"] == ""


@pytest.mark.parametrize("code", ["secure_field", "no_window", "nothing_readable",
                                  "own_app", "browser"])
def test_app_refusals_map_to_messages(use, code) -> None:
    use(FakeBridge({"ok": False, "error_code": code, "app": "Notes",
                    "bundle_id": "com.apple.Notes", "title": "t"}))
    out = rfw.read_front_window_impl()
    assert out["reason"] == code and out["text"] == ""
    assert out["error"] == rfw._CODE_MESSAGES[code]


# ---- never read ---------------------------------------------------------------

def test_never_watch_list_goes_in_the_request(use) -> None:
    fake = use(FakeBridge(_ok()))
    rfw.read_front_window_impl()
    sent = [e.lower() for e in fake.calls[0][1]]
    assert "com.1password.1password" in sent and "com.apple.keychainaccess" in sent


def test_never_watch_app_returns_nothing_even_if_app_sent_text(use) -> None:
    use(FakeBridge({"ok": True, "app": "1Password", "bundle_id": "com.1password.1password",
                    "title": "Vault", "text": "hunter2 secret", "truncated": False}))
    out = rfw.read_front_window_impl()
    assert out["reason"] == "never_watch"
    assert out["text"] == "" and out["app"] == "" and out["title"] == ""
    assert "hunter2" not in repr(out)


def test_never_watch_refusal_from_app_blanks_app_and_title(use) -> None:
    use(FakeBridge({"ok": False, "error_code": "never_watch", "app": "Keychain Access",
                    "bundle_id": "", "title": "login"}))
    out = rfw.read_front_window_impl()
    assert out["reason"] == "never_watch" and out["app"] == "" and out["title"] == ""


def test_browser_window_is_refused_here(use) -> None:
    use(FakeBridge(_ok("bank balance", app="Google Chrome", bundle_id="com.google.Chrome")))
    out = rfw.read_front_window_impl()
    assert out["reason"] == "browser" and out["text"] == ""
    assert "read_active_tab_text" in out["error"]
    for bid in ("com.apple.Safari", "org.mozilla.firefox", "company.thebrowser.Browser",
                "com.brave.Browser", "com.microsoft.edgemac"):
        assert rfw.is_browser(bid), bid
    assert not rfw.is_browser("com.tinyspeck.slackmacgap")  # Slack is readable


def test_own_window_is_refused(use) -> None:
    use(FakeBridge(_ok("chat history", app="MenuBarUI", bundle_id="com.botinator.MenuBarUI")))
    out = rfw.read_front_window_impl()
    assert out["reason"] == "own_app" and out["text"] == ""


# ---- who may call it (decision 4) --------------------------------------------

def test_only_chat_turns_read(monkeypatch) -> None:
    fake = FakeBridge(_ok())
    monkeypatch.setattr(rfw, "bridge", fake)
    rfw.set_turn_kind_provider(None)
    try:
        assert rfw.read_front_window_impl()["reason"] == "not_user_turn"
        for kind in ("event", "pattern", ""):
            rfw.set_turn_kind_provider(lambda k=kind: k)
            assert rfw.read_front_window_impl()["reason"] == "not_user_turn"
        assert fake.calls == []
        rfw.set_turn_kind_provider(lambda: "chat")
        assert rfw.read_front_window_impl()["text"]
    finally:
        rfw.set_turn_kind_provider(None)


# ---- privacy (#31 / 7c) ---------------------------------------------------------

def test_cloud_main_without_opt_in_gets_placeholder(use, monkeypatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    use(FakeBridge(_ok("private doc body")))
    out = rfw.read_front_window_impl()
    assert out["text"] == privacy.PLACEHOLDER and out["note"] == rfw.CLOUD_NOTE
    assert out["app"] == "Pages" and out["title"] == "Plan.pages"


def test_cloud_main_with_opt_in_gets_text(use, monkeypatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    use(FakeBridge(_ok("private doc body")))
    out = rfw.read_front_window_impl()
    assert out["text"] == "private doc body" and "note" not in out


def test_local_main_gets_text(use) -> None:
    use(FakeBridge(_ok("private doc body")))
    out = rfw.read_front_window_impl()
    assert out["text"] == "private doc body" and "note" not in out


def test_marks_turn_as_outside_text() -> None:
    from ai import taint

    assert taint.OUTSIDE_TEXT_TOOLS.get("read_front_window") == "window"


# ---- the real bridge over a loop ---------------------------------------------------

@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


def test_bridge_round_trip_and_status(loop_thread) -> None:
    b = rfw.ScreenTextBridge()
    sent: list[tuple] = []

    async def send(request_id, max_chars, never_watch, timeout_seconds):
        sent.append((request_id, max_chars, never_watch, timeout_seconds))
        # The app answers on the same connection.
        loop_thread.call_soon(b.submit_response, "c1",
                              {"request_id": request_id, "ok": True, "text": "hi"})

    b.connect("c1", send, loop_thread)
    assert not b.connected()  # no screen_text_status yet
    b.set_status("c1", True)
    assert b.connected() and b.accessibility_trusted() is True
    reply = b.request(100, ["com.x.y"])
    assert reply["text"] == "hi"
    assert sent[0][1:] == (100, ["com.x.y"], 2)
    b.set_status("c1", False)
    assert b.accessibility_trusted() is False
    b.disconnect("c1")
    assert not b.connected()
    with pytest.raises(rfw.ScreenTextError) as err:
        b.request(1, [])
    assert err.value.code == "app_not_connected"


def test_bridge_times_out_and_ignores_other_connections(loop_thread) -> None:
    b = rfw.ScreenTextBridge()
    ids: list[str] = []

    async def send(request_id, *_a):
        ids.append(request_id)
        # A different connection answering must not resolve it.
        loop_thread.call_soon(b.submit_response, "other", {"request_id": request_id, "ok": True})

    b.connect("c1", send, loop_thread)
    b.set_status("c1", True)
    with pytest.raises(rfw.ScreenTextError) as err:
        b.request(1, [], timeout=0.3)
    assert err.value.code == "timeout"


# ---- WS schema (additive) ------------------------------------------------------------

def test_ws_frames_parse() -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(wsm.WSMessage)
    resp = adapter.validate_json(
        '{"type":"screen_text_response","seq":1,"timestamp":"2026-11-10T10:00:00Z",'
        '"payload":{"request_id":"scr_1","ok":false,"error_code":"secure_field"}}')
    assert isinstance(resp, wsm.ScreenTextResponse)
    assert resp.payload.error_code == "secure_field" and resp.payload.truncated is False
    status = adapter.validate_json(
        '{"type":"screen_text_status","seq":2,"timestamp":"2026-11-10T10:00:00Z",'
        '"payload":{"accessibility_trusted":true}}')
    assert isinstance(status, wsm.ScreenTextStatus)
    req = wsm.ScreenTextRequest(seq=3, timestamp="2026-11-10T10:00:00Z",
                                payload=wsm.ScreenTextRequestPayload(request_id="scr_2"))
    assert '"screen_text_request"' in req.model_dump_json()
    # Quick-ask context keeps its shape; "window" is a new source value.
    assert set(wsm.ChatContextPayload(front_app="Pages").model_dump()) == {
        "front_app", "window_title", "selection", "selection_source"}


# ---- approval gate (shared decision 5) ------------------------------------------------

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
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.01), loop).result(timeout=2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
    return bool(asked)


def test_gate_constant() -> None:
    assert rfw.READ_ONLY == frozenset({"read_front_window"})
    assert getattr(rfw.read_front_window, "name", "read_front_window") == "read_front_window"


def test_gate_via_orchestrator_wrapper() -> None:
    read_only = frozenset(tools.READ_ONLY_TOOL_NAMES | rfw.READ_ONLY)
    assert _approval_requested("read_front_window", read_only) is False
    # Non-vacuity flip: off the read-only side it asks for approval.
    assert _approval_requested("read_front_window", read_only - rfw.READ_ONLY) is True


def test_gate_registered_in_tools_package() -> None:
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    if "read_front_window" not in names:
        pytest.skip("read_front_window not registered yet (runner registers at wave close)")
    assert "read_front_window" in tools.READ_ONLY_TOOL_NAMES


# ---- quick-ask "include this window's text" ------------------------------------------

def test_quick_ask_window_context_reads_without_a_chat_turn(monkeypatch) -> None:
    # The ticked box is the request: no turn-kind provider needed.
    rfw.set_turn_kind_provider(None)
    monkeypatch.setattr(rfw, "bridge", FakeBridge(_ok("Doc body")))
    ctx = rfw.context_with_window_text({"front_app": "Pages", "selection_source": "window"})
    assert ctx["selection"] == "Doc body" and ctx["selection_source"] == "window"
    assert ctx["window_title"] == "Plan.pages"
    parsed = wsm.ChatContextPayload(**ctx)  # the new source value validates
    assert parsed.selection_source == "window"


def test_quick_ask_window_context_never_watch_and_untouched(monkeypatch) -> None:
    monkeypatch.setattr(rfw, "bridge", FakeBridge(
        {"ok": True, "app": "1Password", "bundle_id": "com.1password.1password",
         "title": "Vault", "text": "hunter2"}))
    ctx = rfw.context_with_window_text({"front_app": "1Password", "window_title": "Vault",
                                        "selection_source": "window"})
    assert "hunter2" not in repr(ctx)
    assert "front_app" not in ctx and "window_title" not in ctx
    assert ctx["selection"].startswith("[the window's text was not read")
    plain = {"front_app": "Notes", "selection": "x", "selection_source": "selection"}
    assert rfw.context_with_window_text(plain) is plain
    assert rfw.context_with_window_text(None) is None


def test_quick_ask_window_context_redacted_under_cloud(monkeypatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    monkeypatch.setattr(rfw, "bridge", FakeBridge(_ok("private doc body")))
    ctx = rfw.context_with_window_text({"selection_source": "window"})
    assert ctx["selection"] == privacy.PLACEHOLDER
