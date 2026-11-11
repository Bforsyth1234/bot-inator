"""Per-engine provider override (#26): resolution, engine build, cloud helpers."""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import config  # noqa: E402
import main  # noqa: E402
from events.event_bus import EventBus  # noqa: E402
from ai.groq_engine import GroqEngine  # noqa: E402
from ai.mlx_engine import MLXEngine  # noqa: E402
from ai.orchestrator import Orchestrator  # noqa: E402

_KEYS = (
    "AGENT_PROVIDER", "AGENT_PROVIDER_MAIN", "AGENT_PROVIDER_ANALYSIS",
    "AGENT_PROVIDER_DRAFTING", "AGENT_PATTERN_ENGINE",
)


def _settings(monkeypatch, **env: str) -> config.Settings:
    """A fresh ``Settings`` read from exactly ``env`` (others unset)."""
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return config.Settings()


@pytest.mark.parametrize("env, expected", [
    # default only
    ({}, {"main": "local", "analysis": "local", "drafting": "local"}),
    ({"AGENT_PROVIDER": "groq"},
     {"main": "groq", "analysis": "groq", "drafting": "groq"}),
    # one override
    ({"AGENT_PROVIDER_DRAFTING": "groq"},
     {"main": "local", "analysis": "local", "drafting": "groq"}),
    ({"AGENT_PROVIDER": "groq", "AGENT_PROVIDER_ANALYSIS": "LOCAL"},
     {"main": "groq", "analysis": "local", "drafting": "groq"}),
    # all overrides beat the default
    ({"AGENT_PROVIDER": "local", "AGENT_PROVIDER_MAIN": "groq",
      "AGENT_PROVIDER_ANALYSIS": "groq", "AGENT_PROVIDER_DRAFTING": "groq"},
     {"main": "groq", "analysis": "groq", "drafting": "groq"}),
    ({"AGENT_PROVIDER": "groq", "AGENT_PROVIDER_MAIN": "local",
      "AGENT_PROVIDER_ANALYSIS": "local", "AGENT_PROVIDER_DRAFTING": "local"},
     {"main": "local", "analysis": "local", "drafting": "local"}),
])
def test_resolution_matrix(monkeypatch, env, expected):
    assert _settings(monkeypatch, **env).engine_providers() == expected


def test_invalid_override_is_local_with_warning(monkeypatch, caplog):
    # Invalid → local, never the (possibly cloud) default: a typo must not
    # brick startup nor send data off the machine (invariant 7c).
    s = _settings(monkeypatch, AGENT_PROVIDER="groq",
                  AGENT_PROVIDER_DRAFTING="grok")
    with caplog.at_level(logging.WARNING, logger="config"):
        assert s.engine_provider("drafting") == "local"
    assert "AGENT_PROVIDER_DRAFTING" in caplog.text
    assert s.engine_provider("main") == "groq"


def test_invalid_default_is_local(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER="nonsense")
    assert set(s.engine_providers().values()) == {"local"}
    assert not s.uses_cloud()


def test_cloud_helpers(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER_DRAFTING="groq")
    assert s.uses_cloud() and s.groq_key_required()
    assert s.is_cloud("drafting") and not s.is_cloud("main")
    assert not s.detection_is_cloud()  # detection defaults to main

    s = _settings(monkeypatch, AGENT_PROVIDER_DRAFTING="groq",
                  AGENT_PATTERN_ENGINE="drafting")
    assert s.detection_is_cloud()
    s = _settings(monkeypatch, AGENT_PROVIDER_MAIN="groq",
                  AGENT_PATTERN_ENGINE="typo")
    assert s.detection_is_cloud()  # unknown choice → main

    s = _settings(monkeypatch)
    assert not s.uses_cloud() and not s.groq_key_required()
    assert not s.detection_is_cloud()


def _patch_main_settings(monkeypatch, **fields: str) -> None:
    for name in ("provider_main", "provider_analysis", "provider_drafting"):
        monkeypatch.setattr(main.settings, name, "")
    monkeypatch.setattr(main.settings, "provider", "local")
    for name, value in fields.items():
        monkeypatch.setattr(main.settings, name, value)


def test_build_engines_mixed(monkeypatch):
    _patch_main_settings(monkeypatch, provider_drafting="groq")
    engine, analysis, drafting = main._build_engines()
    assert isinstance(engine, MLXEngine)
    assert engine.model_name == main.settings.model_path
    assert isinstance(analysis, MLXEngine)
    assert analysis.model_name == main.settings.analysis_model_path
    assert isinstance(drafting, GroqEngine)
    assert drafting.model_name == main.settings.groq_drafting_model


def test_build_engines_groq_default_local_analysis(monkeypatch):
    _patch_main_settings(monkeypatch, provider="groq",
                         provider_analysis="local")
    engine, analysis, drafting = main._build_engines()
    assert isinstance(engine, GroqEngine)
    assert engine.model_name == main.settings.groq_main_model
    assert isinstance(analysis, MLXEngine)
    assert isinstance(drafting, GroqEngine)


def test_mixed_setup_picks_adapter_per_main_engine(monkeypatch):
    """Local main + Groq drafting → MLX adapter; Groq main → OpenAI model."""
    _patch_main_settings(monkeypatch, provider_drafting="groq")
    engine, _, _ = main._build_engines()
    marker = object()
    monkeypatch.setattr(
        "ai.orchestrator._make_mlx_model_class",
        lambda: (lambda eng: SimpleNamespace(engine=eng, marker=marker)),
    )
    model = Orchestrator(engine=engine, event_bus=EventBus(),
                         tools=[])._build_smolagents_model()
    assert model.marker is marker and model.engine is engine

    _patch_main_settings(monkeypatch, provider_main="groq")
    engine, _, _ = main._build_engines()

    class _Stub:
        def __init__(self, **kw):
            self.kw = kw

    monkeypatch.setitem(sys.modules, "smolagents.models",
                        SimpleNamespace(OpenAIServerModel=_Stub))
    model = Orchestrator(engine=engine, event_bus=EventBus(),
                         tools=[])._build_smolagents_model()
    assert isinstance(model, _Stub)
    assert model.kw["model_id"] == main.settings.groq_main_model


@pytest.mark.parametrize("engine_cls", [MLXEngine, GroqEngine])
def test_drafting_engines_share_the_draft_surface(engine_cls):
    # meta_tool_generator calls only ``generate_chat_sync`` on the draft
    # engine, so either provider works there.
    assert callable(getattr(engine_cls, "generate_chat_sync", None))
