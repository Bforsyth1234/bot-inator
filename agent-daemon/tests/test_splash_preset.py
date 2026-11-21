"""#146: the Splash preset (local OpenAI-compatible server for Apple silicon).

Splash defaults to port 8000, which is the daemon's own port, so the preset
must point elsewhere; it must count as local (7c), need no key, and fill a
model for every role so picking it in Settings is enough.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import config  # noqa: E402
import providers  # noqa: E402
import user_settings  # noqa: E402
from ai.groq_engine import build_engine  # noqa: E402

SPLASH = providers.PRESETS["splash"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("AGENT_PROVIDER", "AGENT_PROVIDER_MAIN", "AGENT_PROVIDER_ANALYSIS",
                "AGENT_PROVIDER_DRAFTING", *providers.preset_env_keys()):
        monkeypatch.delenv(key, raising=False)


def test_splash_is_a_provider_with_allowed_keys():
    assert "splash" in providers.PROVIDERS
    assert set(SPLASH.env_keys()) <= user_settings.ALLOWED_KEYS
    assert SPLASH.key_env() == "SPLASH_API_KEY"


def test_splash_base_is_loopback_and_not_the_daemon_port():
    url = urlparse(SPLASH.api_base)
    assert providers.is_loopback_url(SPLASH.api_base)
    assert url.path == "/v1"
    assert url.port != config.Settings().port


def test_splash_fills_every_role_and_needs_no_key(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "splash")
    s = config.Settings()
    assert not s.uses_cloud()
    for role in providers.ENGINE_ROLES:
        assert s.provider_model("splash", role) == providers.SPLASH_DEFAULT_MODEL
    engine = build_engine(s, "splash", "main")
    asyncio.run(engine.load())
    assert engine.loaded


def test_splash_on_the_lan_counts_as_cloud(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "splash")
    monkeypatch.setenv("SPLASH_API_BASE", "http://192.168.1.20:8001/v1")
    assert config.Settings().uses_cloud()
