"""Unit tests for the Groq engine and the orchestrator's adapter dispatch."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.groq_engine import GroqEngine  # noqa: E402
from ai.orchestrator import Orchestrator  # noqa: E402
from events.event_bus import EventBus  # noqa: E402


def _fake_choice(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _patched_engine(monkeypatch: pytest.MonkeyPatch, return_text: str) -> tuple[GroqEngine, MagicMock]:
    """Return a loaded GroqEngine whose OpenAI client is a MagicMock."""
    engine = GroqEngine("test-model", api_key="sk-test", api_base="https://stub")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_choice(return_text)
    fake_openai_module = SimpleNamespace(OpenAI=lambda **kwargs: fake_client)
    monkeypatch.setitem(sys.modules, "openai", fake_openai_module)
    asyncio.get_event_loop().run_until_complete(engine.load())
    return engine, fake_client


def test_load_requires_api_key() -> None:
    engine = GroqEngine("test-model", api_key="")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asyncio.get_event_loop().run_until_complete(engine.load())
    assert not engine.loaded
    assert engine.current_model is None


def test_generate_routes_through_chat_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, client = _patched_engine(monkeypatch, "hello world")
    out = asyncio.get_event_loop().run_until_complete(
        engine.generate("hi", max_tokens=10)
    )
    assert out == "hello world"
    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["max_tokens"] == 10
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_generate_chat_sync_passes_system_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, client = _patched_engine(monkeypatch, "drafted module")
    out = engine.generate_chat_sync("you are X", "do Y", max_tokens=20)
    assert out == "drafted module"
    msgs = client.chat.completions.create.call_args.kwargs["messages"]
    assert msgs == [
        {"role": "system", "content": "you are X"},
        {"role": "user", "content": "do Y"},
    ]


def test_evaluate_event_strips_think_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _patched_engine(
        monkeypatch,
        "<think>scratch reasoning</think>User opened a Jira ticket.",
    )
    out = asyncio.get_event_loop().run_until_complete(
        engine.evaluate_event("app_activated: Chrome — Jira")
    )
    assert out == "User opened a Jira ticket."


def test_evaluate_event_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, client = _patched_engine(monkeypatch, "")
    client.chat.completions.create.side_effect = RuntimeError("boom")
    out = asyncio.get_event_loop().run_until_complete(
        engine.evaluate_event("anything")
    )
    assert out == ""


def test_swap_updates_active_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _patched_engine(monkeypatch, "x")
    asyncio.get_event_loop().run_until_complete(engine.swap("other-model"))
    assert engine.model_name == "other-model"
    assert engine.current_model == "other-model"


def test_orchestrator_picks_openai_server_model_for_groq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_smolagents_model`` returns OpenAIServerModel for GroqEngine."""
    engine = GroqEngine("test-model", api_key="sk-test", api_base="https://stub")
    bus = EventBus()
    orch = Orchestrator(engine=engine, event_bus=bus, tools=[])

    captured: dict[str, object] = {}

    class _StubServerModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_models = SimpleNamespace(OpenAIServerModel=_StubServerModel)
    monkeypatch.setitem(sys.modules, "smolagents.models", fake_models)
    model = orch._build_smolagents_model()
    assert isinstance(model, _StubServerModel)
    assert captured["model_id"] == "test-model"
    assert captured["api_base"] == "https://stub"
    assert captured["api_key"] == "sk-test"


def test_orchestrator_picks_mlx_adapter_for_local_engine() -> None:
    """The MLX dispatch path stays unchanged for :class:`MLXEngine`."""
    from ai.mlx_engine import MLXEngine

    engine = MLXEngine("mlx-community/some-model")
    bus = EventBus()
    orch = Orchestrator(engine=engine, event_bus=bus, tools=[])

    sentinel = object()

    def _fake_factory() -> type:
        class _Adapter:
            def __init__(self, eng: object) -> None:
                self.engine = eng
                self.marker = sentinel

        return _Adapter

    with patch("ai.orchestrator._make_mlx_model_class", _fake_factory):
        model = orch._build_smolagents_model()
    assert getattr(model, "marker", None) is sentinel
    assert model.engine is engine
