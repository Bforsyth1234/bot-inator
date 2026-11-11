"""#97: OpenAI-compatible presets: resolution, privacy by host, engine
build, settings validation, the List models / Test routes, key hygiene."""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
import providers  # noqa: E402
import user_settings  # noqa: E402
from ai.groq_engine import GroqEngine, OpenAICompatibleEngine, build_engine  # noqa: E402
from auth import auth_header  # noqa: E402
from routes import providers as provider_routes  # noqa: E402

SECRET = "sk-test-SECRET-9876wxyz"
_PROVIDER_KEYS = (
    "AGENT_PROVIDER", "AGENT_PROVIDER_MAIN", "AGENT_PROVIDER_ANALYSIS",
    "AGENT_PROVIDER_DRAFTING",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (*_PROVIDER_KEYS, *providers.preset_env_keys()):
        monkeypatch.delenv(key, raising=False)


def _settings(monkeypatch, **env: str) -> config.Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return config.Settings()


# --- table and privacy by host -------------------------------------------

def test_every_preset_is_a_provider_and_has_its_keys_allowed():
    assert providers.PROVIDERS[0] == "local"
    for preset in providers.PRESETS.values():
        assert preset.id in providers.PROVIDERS
        assert set(preset.env_keys()) <= user_settings.ALLOWED_KEYS
    # Groq keeps its historic keys, so old settings files keep working.
    assert providers.PRESETS["groq"].key_env() == "GROQ_API_KEY"
    assert providers.PRESETS["groq"].model_env("main") == "GROQ_MAIN_MODEL"


@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:1234/v1", True),
    ("http://[::1]:8080/v1", True),
    ("http://lmstudio.localhost/v1", True),
    ("https://api.openai.com/v1", False),
    ("http://192.168.1.20:11434/v1", False),   # another machine on the LAN
    ("http://localhost.evil.example/v1", False),
    ("", False),
    ("not a url", False),
])
def test_loopback_decides_local(url, local):
    assert providers.is_loopback_url(url) is local


def test_local_server_preset_is_not_cloud_but_a_lan_one_is(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER="local_server")
    assert not s.is_cloud("main") and not s.uses_cloud()
    s = _settings(monkeypatch, AGENT_PROVIDER="local_server",
                  LOCAL_SERVER_API_BASE="http://192.168.1.20:11434/v1")
    assert s.is_cloud("main")


def test_custom_with_no_base_counts_as_cloud(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER_DRAFTING="custom")
    assert s.is_cloud("drafting") and not s.is_cloud("main")


def test_cloud_presets_are_cloud(monkeypatch):
    for pid in ("openai", "anthropic", "gemini", "xai", "openrouter", "groq"):
        s = _settings(monkeypatch, AGENT_PROVIDER=pid)
        assert s.is_cloud("main"), pid


# --- resolution and engine build -----------------------------------------

def test_preset_values_resolve_from_env_and_defaults(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER="anthropic", ANTHROPIC_API_KEY=SECRET,
                  ANTHROPIC_ANALYSIS_MODEL="my-small-model")
    assert s.provider_api_key("anthropic") == SECRET
    assert s.provider_api_base("anthropic") == providers.PRESETS["anthropic"].api_base
    assert s.provider_model("anthropic", "main") == "claude-sonnet-5-5"
    assert s.provider_model("anthropic", "analysis") == "my-small-model"
    assert s.engine_problems() == {}


def test_engine_problems_name_the_missing_piece(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER="openai")
    assert set(s.engine_problems()) == {"main", "analysis", "drafting"}
    assert "OPENAI_API_KEY" in s.engine_problems()["main"]
    s = _settings(monkeypatch, AGENT_PROVIDER="openai", OPENAI_API_KEY=SECRET)
    assert "OPENAI_MAIN_MODEL" in s.engine_problems()["main"]  # no default model
    s = _settings(monkeypatch, AGENT_PROVIDER="local_server",
                  LOCAL_SERVER_MAIN_MODEL="m", LOCAL_SERVER_ANALYSIS_MODEL="m",
                  LOCAL_SERVER_DRAFTING_MODEL="m")
    assert s.engine_problems() == {}  # no key needed on a local server


def test_build_engines_uses_each_roles_preset(monkeypatch):
    s = _settings(monkeypatch, AGENT_PROVIDER="local", AGENT_PROVIDER_MAIN="gemini",
                  AGENT_PROVIDER_DRAFTING="openai", GEMINI_API_KEY="g", GEMINI_MAIN_MODEL="gm",
                  OPENAI_API_KEY="o", OPENAI_DRAFTING_MODEL="od")
    monkeypatch.setattr(main, "settings", s)
    engine, analysis, drafting = main._build_engines()
    assert isinstance(engine, OpenAICompatibleEngine) and engine.provider == "gemini"
    assert engine.api_base == providers.PRESETS["gemini"].api_base and engine.model_name == "gm"
    assert not isinstance(analysis, OpenAICompatibleEngine)  # local MLX
    assert drafting.provider == "openai" and drafting.token_param == "max_completion_tokens"
    assert GroqEngine is OpenAICompatibleEngine


def _fake_client(calls: list):
    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.parametrize("pid,param", [
    ("openai", "max_completion_tokens"), ("anthropic", "max_tokens"), ("groq", "max_tokens"),
])
def test_token_parameter_per_preset(monkeypatch, pid, param):
    s = _settings(monkeypatch, **{f"{providers.PRESETS[pid].prefix}_API_KEY": "k",
                                  f"{providers.PRESETS[pid].prefix}_MAIN_MODEL": "m"})
    engine = build_engine(s, pid, "main")
    calls: list = []
    engine._client, engine._loaded = _fake_client(calls), True
    assert engine.generate_sync("hi", max_tokens=33) == "ok"
    other = {"max_tokens", "max_completion_tokens"} - {param}
    assert calls[0][param] == 33 and not other & set(calls[0])


def test_local_server_engine_loads_without_a_key(monkeypatch):
    import asyncio

    s = _settings(monkeypatch, LOCAL_SERVER_MAIN_MODEL="llama")
    engine = build_engine(s, "local_server", "main")
    asyncio.run(engine.load())
    assert engine.loaded
    keyed = build_engine(s, "openai", "main")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(keyed.load())


# --- settings API ----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    from config import settings
    monkeypatch.setattr(settings, "provider", "local")
    monkeypatch.setattr(settings, "groq_api_key", "")
    for role in ("main", "analysis", "drafting"):
        monkeypatch.setattr(settings, f"provider_{role}", "")
    with TestClient(main.app, headers=auth_header()) as test_client:
        test_client.settings_path = path
        yield test_client


def _preset(view: dict, pid: str) -> dict:
    return next(p for p in view["presets"] if p["id"] == pid)


def test_view_lists_presets_without_full_keys(client):
    resp = client.put("/api/settings", json={
        "provider": "anthropic",
        "preset_settings": {"anthropic": {"api_key": SECRET}},
    })
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text
    view = resp.json()
    anthropic = _preset(view, "anthropic")
    assert anthropic["key_set"] and anthropic["key_hint"] == "…wxyz"
    assert anthropic["cloud"] and anthropic["in_use"] == ["analysis", "drafting", "main"]
    assert anthropic["problem"] == "" and not anthropic["tested"]
    assert _preset(view, "groq")["tested"]
    assert not _preset(view, "local_server")["cloud"]
    saved = json.loads(client.settings_path.read_text())
    assert saved == {"AGENT_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": SECRET}


def test_preset_without_key_or_model_is_refused(client):
    resp = client.put("/api/settings", json={"provider": "xai"})
    assert resp.status_code == 400 and "API key" in resp.json()["detail"]
    resp = client.put("/api/settings", json={
        "provider_drafting": "xai", "preset_settings": {"xai": {"api_key": "k"}}})
    assert resp.status_code == 400 and "drafting model" in resp.json()["detail"]
    assert not client.settings_path.exists()
    resp = client.put("/api/settings", json={
        "provider_drafting": "xai",
        "preset_settings": {"xai": {"api_key": "k", "drafting_model": "grok-x"}}})
    assert resp.status_code == 200, resp.text
    # Only the drafting role needs a model; main and analysis stay local.
    assert _preset(resp.json(), "xai")["in_use"] == ["drafting"]


def test_defaults_are_not_pinned_and_bad_base_refused(client):
    resp = client.put("/api/settings", json={"preset_settings": {"anthropic": {
        "api_base": providers.PRESETS["anthropic"].api_base,
        "main_model": "claude-sonnet-5-5", "analysis_model": "custom-small"}}})
    assert resp.status_code == 200, resp.text
    saved = json.loads(client.settings_path.read_text())
    assert saved == {"ANTHROPIC_ANALYSIS_MODEL": "custom-small"}
    resp = client.put("/api/settings", json={"preset_settings": {
        "custom": {"api_base": "ftp://x"}}})
    assert resp.status_code == 400
    resp = client.put("/api/settings", json={"preset_settings": {"nope": {}}})
    assert resp.status_code == 400


def test_old_groq_fields_and_preset_fields_share_storage(client):
    resp = client.put("/api/settings", json={
        "provider": "groq", "preset_settings": {"groq": {"api_key": "gsk_test_1234abcd"}}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["groq_api_key_set"] is True
    assert json.loads(client.settings_path.read_text())["GROQ_API_KEY"] == "gsk_test_1234abcd"


# --- List models / Test routes --------------------------------------------

class _FakeOpenAI:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        _FakeOpenAI.instances.append(self)
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id="b"), SimpleNamespace(id="a")])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.calls: list = []

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


@pytest.fixture
def fake_openai(monkeypatch):
    import openai

    _FakeOpenAI.instances = []
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return _FakeOpenAI


def test_list_models_and_test_use_body_key_without_saving(client, fake_openai):
    resp = client.post("/api/providers/openai/models", json={"api_key": SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json()["models"] == ["a", "b"]
    assert fake_openai.instances[-1].kw["base_url"] == providers.PRESETS["openai"].api_base
    resp = client.post("/api/providers/openai/test", json={"api_key": SECRET, "model": "a"})
    assert resp.json()["ok"] is True
    assert fake_openai.instances[-1].calls[-1]["max_completion_tokens"] == 16
    assert not client.settings_path.exists()


def test_probe_needs_key_model_and_known_preset(client, fake_openai):
    assert client.post("/api/providers/openai/models", json={}).status_code == 400
    assert client.post("/api/providers/nope/models", json={}).status_code == 404
    resp = client.post("/api/providers/openai/test", json={"api_key": "k"})
    assert resp.status_code == 400  # no model picked and none saved
    # A local server needs no key.
    assert client.post("/api/providers/local_server/models", json={}).status_code == 200


def test_probe_errors_never_echo_the_key(client, monkeypatch):
    import openai

    class _Boom(_FakeOpenAI):
        def __init__(self, **kw):
            super().__init__(**kw)

            def fail():
                raise RuntimeError(f"401 bad key {kw['api_key']}")
            self.models = SimpleNamespace(list=fail)

    monkeypatch.setattr(openai, "OpenAI", _Boom)
    resp = client.post("/api/providers/openai/models", json={"api_key": SECRET})
    assert resp.status_code == 502 and SECRET not in resp.text


def test_key_is_never_logged(monkeypatch, caplog):
    import asyncio

    caplog.set_level(logging.DEBUG)
    s = _settings(monkeypatch, OPENAI_API_KEY=SECRET, OPENAI_MAIN_MODEL="m")
    engine = build_engine(s, "openai", "main")
    asyncio.run(engine.load())
    assert SECRET not in caplog.text


def test_routes_need_the_daemon_token(client, fake_openai):
    with TestClient(main.app) as anonymous:
        resp = anonymous.post("/api/providers/local_server/models", json={})
    assert resp.status_code == 401
    assert client.post("/api/providers/local_server/models", json={}).status_code == 200
    assert provider_routes.router.prefix == "/api/providers"


# --- privacy ledger by host (#97 decision 6, merged onto #82) -------------

@pytest.mark.parametrize("pid,host", [
    ("openai", "api.openai.com"), ("anthropic", "api.anthropic.com"),
    ("gemini", "generativelanguage.googleapis.com"), ("xai", "api.x.ai"),
    ("groq", "api.groq.com"), ("openrouter", "openrouter.ai"),
])
def test_cloud_preset_call_is_a_ledger_row_with_its_host_and_role(monkeypatch, tmp_path, pid, host):
    from ai import egress_log

    log = egress_log.EgressLog(tmp_path / "egress.db")
    monkeypatch.setattr(egress_log, "default_log", lambda: log)
    prefix = providers.PRESETS[pid].prefix
    s = _settings(monkeypatch, **{f"{prefix}_API_KEY": SECRET, f"{prefix}_DRAFTING_MODEL": "m"})
    engine = build_engine(s, pid, "drafting")
    calls: list = []
    engine._client, engine._loaded = _fake_client(calls), True
    engine.generate_sync("hi", max_tokens=5)
    rows = log.rows()
    assert [(r["host"], r["role"], r["kind"]) for r in rows] == [(host, "drafting", "inference")]
    assert SECRET not in json.dumps(rows)


def test_loopback_preset_call_is_not_a_ledger_row(monkeypatch, tmp_path):
    from ai import egress_log

    log = egress_log.EgressLog(tmp_path / "egress.db")
    monkeypatch.setattr(egress_log, "default_log", lambda: log)
    s = _settings(monkeypatch, LOCAL_SERVER_MAIN_MODEL="llama")
    engine = build_engine(s, "local_server", "main")
    calls: list = []
    engine._client, engine._loaded = _fake_client(calls), True
    assert engine.generate_sync("hi", max_tokens=5) == "ok"
    assert calls and log.rows() == []
