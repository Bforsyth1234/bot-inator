"""Tests for the built-in look_at_screen (todo #107).

The capture (``screencapture``), the window list, secure input and the
vision model are all faked: no real screen is ever captured and no model
is loaded. Cloud calls go to a fake client; nothing leaves the Mac.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from tests.test_notices_privacy import _LoopThread, _drain

las = importlib.import_module("tools.look_at_screen")
from ai import egress_log, vision_engine  # noqa: E402

OWN = {"id": 11, "pid": 100, "owner": "bot-inator", "title": "Chat", "layer": 0,
       "alpha": 1.0, "width": 400, "height": 500}
NUMBERS = {"id": 22, "pid": 200, "owner": "Numbers", "title": "Budget.numbers",
           "layer": 0, "alpha": 1.0, "width": 1200, "height": 800}
MENU = {"id": 5, "pid": 300, "owner": "Control Center", "title": "", "layer": 25,
        "alpha": 1.0, "width": 30, "height": 30}
BUNDLES = {100: "com.botinator.MenuBarUI", 200: "com.apple.iWork.Numbers",
           300: "com.apple.controlcenter", 400: "com.1password.1password",
           500: "com.google.Chrome"}
VISION_KEYS = ("AGENT_VISION_PROVIDER", "MLX_VISION_MODEL", "OPENAI_VISION_MODEL",
               "OPENAI_API_KEY", "GEMINI_VISION_MODEL")


def _png(path: Path, size: tuple[int, int] = (1200, 800)) -> None:
    from PIL import Image

    Image.new("RGB", size, "white").save(path)


class Screen:
    def __init__(self) -> None:
        self.windows: list[dict[str, Any]] = [MENU, OWN, NUMBERS]
        self.front = ("com.botinator.MenuBarUI", "bot-inator")
        self.secure = False
        self.tab_url: str | None = None
        self.captures: list[list[str]] = []
        self.captured_paths: list[Path] = []
        self.capture_size: tuple[int, int] = (1200, 800)
        self.fail_capture = False


@pytest.fixture()
def screen(monkeypatch, tmp_path) -> Screen:
    s = Screen()
    for key in VISION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "0")
    monkeypatch.setattr(las, "_secure_input_enabled", lambda: s.secure)
    monkeypatch.setattr(las, "_frontmost_app", lambda: s.front)
    monkeypatch.setattr(las, "_window_list", lambda: list(s.windows))
    monkeypatch.setattr(las, "_bundle_id_for_pid", lambda pid: BUNDLES.get(pid, ""))
    monkeypatch.setattr(las, "_browser_tab_url", lambda app: s.tab_url)
    monkeypatch.setattr(las, "_screen_recording_granted", lambda: True)
    monkeypatch.setattr(las, "_user_asked", lambda: True)
    monkeypatch.delenv("TMPDIR", raising=False)

    def fake_capture(args: list[str]) -> Any:
        s.captures.append(list(args))
        path = Path(args[-1])
        s.captured_paths.append(path)
        if s.fail_capture:
            return SimpleNamespace(returncode=1, stdout="", stderr="could not create image")
        _png(path, s.capture_size)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(las, "_run_screencapture", fake_capture)
    las._pin(None)
    return s


class FakeModel:
    def __init__(self, answer: str = "The chart shows sales by quarter.") -> None:
        self.answer = answer
        self.loads: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.raise_on_generate: Exception | None = None
        self.seen_sizes: list[tuple[int, int]] = []


@pytest.fixture()
def model(monkeypatch) -> FakeModel:
    fm = FakeModel()
    vision_engine.reset_local_engine()

    def load(name: str) -> tuple[Any, Any]:
        fm.loads.append(name)
        return object(), object()

    def generate(m: Any, p: Any, image_path: str, prompt: str, max_tokens: int) -> str:
        from PIL import Image

        assert Path(image_path).exists()
        with Image.open(image_path) as im:
            fm.seen_sizes.append(im.size)
        fm.calls.append((image_path, prompt))
        if fm.raise_on_generate is not None:
            raise fm.raise_on_generate
        return fm.answer

    monkeypatch.setattr(vision_engine, "_load_mlx_vlm", load)
    monkeypatch.setattr(vision_engine, "_generate_mlx_vlm", generate)
    yield fm
    vision_engine.reset_local_engine()


@pytest.fixture()
def ledger(tmp_path) -> egress_log.EgressLog:
    log = egress_log.EgressLog(tmp_path / "egress.db", keep_text=False)
    egress_log.set_default_log(log)
    yield log
    egress_log.set_default_log(None)
    log.close()


def _leftovers() -> list[str]:
    import tempfile

    return [p for p in os.listdir(tempfile.gettempdir()) if p.startswith(las.TEMP_PREFIX)]


# ---- the happy path ----------------------------------------------------------

def test_front_window_is_captured_asked_and_deleted(screen, model) -> None:
    before = set(_leftovers())
    result = las.look_at_screen_impl("what does the chart show?")
    assert result["status"] == "ok"
    assert result["answer"] == "The chart shows sales by quarter."
    assert result["captured"] == "the window “Budget.numbers” (Numbers)"
    assert result["engine"] == "local"
    # bot-inator's own chat window is skipped: the window behind it (id 22).
    assert screen.captures[0][:4] == ["-x", "-o", "-l", "22"]
    from config import default_vision_model

    assert model.loads == [default_vision_model()]
    assert "what does the chart show?" in model.calls[0][1]
    assert not screen.captured_paths[0].exists()
    assert not screen.captured_paths[0].parent.exists()
    assert set(_leftovers()) == before


def test_screen_target_captures_the_main_display(screen, model) -> None:
    result = las.look_at_screen_impl("what is on screen?", "screen")
    assert result["status"] == "ok" and result["captured"] == "the whole screen"
    assert screen.captures[0][:2] == ["-x", "-m"]


def test_model_loads_lazily_once(screen, model) -> None:
    assert model.loads == []  # nothing loads until a question
    las.look_at_screen_impl("a")
    las.look_at_screen_impl("b")
    assert len(model.loads) == 1


def test_big_capture_is_scaled_down_before_the_model_sees_it(screen, model) -> None:
    screen.capture_size = (2880, 1800)
    las.look_at_screen_impl("read it")
    assert max(model.seen_sizes[0]) == vision_engine.MAX_IMAGE_SIDE


# ---- the image is deleted on every failure ---------------------------------

def test_image_deleted_when_the_model_fails(screen, model) -> None:
    model.raise_on_generate = RuntimeError("metal exploded")
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "metal exploded" in result["error"]
    assert screen.captured_paths and not screen.captured_paths[0].parent.exists()


def test_image_deleted_when_the_capture_fails(screen, model) -> None:
    screen.fail_capture = True
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "Screen Recording" in result["error"]
    assert model.calls == []
    assert not screen.captured_paths[0].parent.exists()


def test_image_deleted_when_the_model_returns_nothing(screen, model) -> None:
    model.answer = "   "
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error"
    assert not screen.captured_paths[0].parent.exists()


# ---- sensitive windows (decision 4) ------------------------------------------

def test_secure_input_refuses_before_capturing(screen, model) -> None:
    screen.secure = True
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "password field" in result["error"]
    assert screen.captures == [] and model.calls == []


def test_unknown_secure_input_state_counts_as_on(monkeypatch) -> None:
    import ctypes

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no Carbon")

    monkeypatch.setattr(ctypes, "CDLL", boom)
    assert las._secure_input_enabled() is True


def test_password_manager_in_front_is_refused(screen, model) -> None:
    screen.front = ("com.1password.1password", "1Password")
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "never-watch" in result["error"]
    assert screen.captures == []


def test_password_manager_as_the_target_window_is_refused(screen, model) -> None:
    screen.windows = [OWN, {**NUMBERS, "id": 44, "pid": 400, "owner": "1Password",
                            "title": "Vault"}]
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "1Password" in result["error"]
    assert screen.captures == []


def test_whole_screen_refused_while_a_password_manager_is_visible(screen, model) -> None:
    # #111: partly out from under Numbers, so some of it is visible.
    screen.windows = [OWN, NUMBERS, {**NUMBERS, "id": 44, "pid": 400, "x": 1100,
                                     "owner": "1Password", "title": "Vault"}]
    result = las.look_at_screen_impl("what?", "screen")
    assert result["status"] == "error" and "has a window on screen" in result["error"]
    assert screen.captures == []


def test_private_browser_window_is_refused(screen, model) -> None:
    screen.windows = [OWN, {**NUMBERS, "pid": 500, "owner": "Google Chrome",
                            "title": "New Tab - Google Chrome (Incognito)"}]
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "private" in result["error"]


def test_sensitive_site_in_chrome_is_refused(screen, model) -> None:
    screen.windows = [OWN, {**NUMBERS, "pid": 500, "owner": "Google Chrome",
                            "title": "Chase"}]
    screen.tab_url = "https://secure.chase.com/accounts"
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "sensitive-site" in result["error"]
    screen.tab_url = "https://example.com/chart"
    assert las.look_at_screen_impl("what?")["status"] == "ok"


def test_no_other_window_is_refused(screen, model) -> None:
    screen.windows = [MENU, OWN]
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "no window" in result["error"]


def test_bad_target(screen, model) -> None:
    assert "target must be" in las.look_at_screen_impl("x", "desktop")["error"]


# ---- who may call it ---------------------------------------------------------

def test_only_in_a_turn_the_user_started(screen, model, monkeypatch) -> None:
    monkeypatch.setattr(las, "_user_asked", lambda: False)
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "only runs when the user asks" in result["error"]
    assert screen.captures == []


def test_user_asked_follows_the_turn_kind(monkeypatch) -> None:
    from tools import read_front_window as screen_text

    monkeypatch.setattr(screen_text, "_turn_kind_provider", lambda: "event")
    assert las._user_asked() is False
    monkeypatch.setattr(screen_text, "_turn_kind_provider", lambda: "chat")
    assert las._user_asked() is True


def test_refused_inside_the_tool_sandbox(screen, model, monkeypatch) -> None:
    monkeypatch.setenv("TMPDIR", "/private/var/folders/x/tool-sandbox-abc/")
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "generated tool" in result["error"]


# ---- cloud (decision 3) ------------------------------------------------------

class FakeClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class _Completions:
            def create(self, **kwargs: Any) -> Any:
                outer.requests.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="A bar chart."))],
                    usage=SimpleNamespace(prompt_tokens=900, completion_tokens=4),
                )

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture()
def cloud(monkeypatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(vision_engine, "_cloud_client", lambda key, base: client)
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return client


def test_cloud_refused_without_the_opt_in(screen, model, cloud, ledger) -> None:
    import os as _os

    _os.environ["OPENAI_VISION_MODEL"] = "gpt-vision-test"
    try:
        result = las.look_at_screen_impl("what?")
    finally:
        _os.environ.pop("OPENAI_VISION_MODEL", None)
    assert result["status"] == "error" and "not sent" in result["error"]
    assert screen.captures == [] and cloud.requests == []  # refused before capture
    assert ledger.rows() == []


def test_cloud_refused_without_a_chosen_vision_model(screen, model, cloud, ledger, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    result = las.look_at_screen_impl("what?")
    assert result["status"] == "error" and "no cloud vision model" in result["error"]
    assert screen.captures == [] and cloud.requests == []


def test_cloud_with_opt_in_and_model_sends_once_and_logs_a_row(
        screen, model, cloud, ledger, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "gpt-vision-test")
    result = las.look_at_screen_impl("what does the chart show?")
    assert result["status"] == "ok" and result["answer"] == "A bar chart."
    assert result["engine"] == "api.openai.com"
    assert model.loads == []  # the local model is not touched
    assert len(cloud.requests) == 1
    req = cloud.requests[0]
    assert req["model"] == "gpt-vision-test" and "max_completion_tokens" in req
    image = req["messages"][0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    rows = ledger.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["host"] == "api.openai.com" and row["role"] == "vision"
    assert row["tool"] == "look_at_screen" and row["model"] == "gpt-vision-test"
    assert "one screenshot" in row["note"] and row["bytes_out"] > 1000
    assert row["text"] is None  # keep-text off; and never the image
    assert not screen.captured_paths[0].parent.exists()


def test_ledger_never_keeps_the_image_even_with_keep_text(tmp_path) -> None:
    log = egress_log.EgressLog(tmp_path / "e.db", keep_text=True)
    egress_log.record_image(model="m", api_base="https://api.openai.com/v1",
                            image_bytes=123_456, question="what is this?", log=log)
    row = log.rows()[0]
    assert row["text"] == "what is this?" and row["bytes_out"] == 123_456 + len("what is this?")
    assert "120.6 KB" in row["note"]
    assert egress_log.record_image(model="m", api_base="http://localhost:11434/v1",
                                   image_bytes=1, question="q", log=log) is False


# ---- the answer under a cloud main engine (7c) -------------------------------

def test_answer_redacted_for_a_cloud_main_engine(screen, model, monkeypatch) -> None:
    from config import settings

    monkeypatch.setattr(settings, "provider_main", "groq")
    monkeypatch.setattr(settings, "groq_api_base", "https://api.groq.com/openai/v1")
    result = las.look_at_screen_impl("what?")
    assert result["answer"] == "[personal text kept on this Mac]"
    assert "kept on this Mac" in result["note"]
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    assert las.look_at_screen_impl("what?")["answer"] == model.answer


# ---- the card ------------------------------------------------------------------

def test_card_names_the_window_and_the_model(screen, model) -> None:
    note = las.card_note("what does the chart show?")
    assert "the window “Budget.numbers” (Numbers)" in note
    assert "local vision model (Qwen3-VL-" in note
    assert "deleted" in note
    assert "the whole screen" in las.card_note("x", target="screen")


def test_card_says_when_refused(screen, model) -> None:
    screen.secure = True
    assert "refused" in las.card_note("x")


def test_card_says_the_screenshot_leaves_the_mac(screen, model, cloud, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "gpt-vision-test")
    assert "leaves this Mac" in las.card_note("x")


def test_the_window_on_the_card_is_the_window_captured(screen, model) -> None:
    las.card_note("x")  # pins window 22
    screen.windows = [OWN, {**NUMBERS, "id": 33, "pid": 200, "title": "Other.numbers"}, NUMBERS]
    result = las.look_at_screen_impl("x")
    assert screen.captures[0][3] == "22"
    assert "Budget.numbers" in result["captured"]


def test_registered_and_approval_gated() -> None:
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    assert "look_at_screen" in names
    assert "look_at_screen" not in tools.READ_ONLY_TOOL_NAMES
    assert las.GATED.isdisjoint(tools.READ_ONLY_TOOL_NAMES)


def test_the_card_carries_the_note_and_the_call_waits_for_approval(screen, model) -> None:
    """Through the real approval gate: a denied card captures nothing."""
    from ai.orchestrator import Orchestrator
    from ai.mlx_engine import MLXEngine
    from events.event_bus import EventBus

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
        wrapped = orch._wrap_tool_for_approval(tools.look_at_screen)

        def call() -> Any:
            return asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(wrapped.forward, "what is this chart?"), loop).result(5)

        denied = call()
        captured_after_deny = list(screen.captures)
        decision["approve"] = True
        orch._turn_done = {}
        approved = call()
        _drain(loop)
    assert denied == {"skipped": True, "reason": "user_denied"}
    assert captured_after_deny == []
    assert approved["status"] == "ok"
    assert len(cards) == 2 and cards[0]["tool_name"] == "look_at_screen"
    assert "Budget.numbers" in cards[0]["reasoning"]


def test_the_drafter_is_told_to_call_look_at_screen() -> None:
    from tests.test_drafting_prompt_limits import _drafting_system_prompt

    assert "look_at_screen(question, target)" in _drafting_system_prompt()
    assert "look_at_screen" in tools.generate_custom_tool.description
