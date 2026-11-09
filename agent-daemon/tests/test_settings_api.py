"""Tests for the UI-editable provider settings (``/api/settings``)."""
from __future__ import annotations

import json
import os
import stat

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import user_settings  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    from config import settings
    monkeypatch.setattr(settings, "provider", "local")
    monkeypatch.setattr(settings, "groq_api_key", "")
    with TestClient(app) as test_client:
        test_client.settings_path = path
        yield test_client


def test_get_defaults(client):
    body = client.get("/api/settings").json()
    assert body["provider"] == "local"
    assert body["running_provider"] == "local"
    assert body["groq_api_key_set"] is False
    assert body["restart_required"] is False


def test_save_groq_writes_private_file_and_hides_key(client):
    resp = client.put("/api/settings", json={
        "provider": "groq", "groq_api_key": "gsk_test_1234abcd",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "groq"
    assert body["groq_api_key_set"] is True
    assert body["groq_api_key_hint"] == "…abcd"
    assert "gsk_test_1234abcd" not in resp.text
    assert body["restart_required"] is True

    saved = json.loads(client.settings_path.read_text())
    assert saved == {"AGENT_PROVIDER": "groq", "GROQ_API_KEY": "gsk_test_1234abcd"}
    assert stat.S_IMODE(client.settings_path.stat().st_mode) == 0o600


def test_omitted_key_keeps_saved_key(client):
    client.put("/api/settings", json={"provider": "groq", "groq_api_key": "gsk_keepme_9999"})
    client.put("/api/settings", json={"groq_main_model": "some-model"})
    saved = json.loads(client.settings_path.read_text())
    assert saved["GROQ_API_KEY"] == "gsk_keepme_9999"
    assert saved["GROQ_MAIN_MODEL"] == "some-model"


def test_default_model_is_not_pinned(client):
    from config import GROQ_DEFAULT_MODELS
    client.put("/api/settings", json={
        "provider": "groq", "groq_api_key": "gsk_test_1234abcd",
        "groq_main_model": GROQ_DEFAULT_MODELS["GROQ_MAIN_MODEL"],
        "groq_analysis_model": "custom-small",
    })
    saved = json.loads(client.settings_path.read_text())
    assert "GROQ_MAIN_MODEL" not in saved
    assert saved["GROQ_ANALYSIS_MODEL"] == "custom-small"


def test_groq_without_key_rejected(client):
    resp = client.put("/api/settings", json={"provider": "groq"})
    assert resp.status_code == 400
    assert not client.settings_path.exists()


def test_unknown_provider_rejected(client):
    resp = client.put("/api/settings", json={"provider": "openai"})
    assert resp.status_code == 400


def test_shell_export_locks_field(client, monkeypatch):
    monkeypatch.setattr(user_settings, "shell_exported", frozenset({"AGENT_PROVIDER"}))
    client.put("/api/settings", json={"provider": "groq", "groq_api_key": "gsk_test_1234abcd"})
    body = client.get("/api/settings").json()
    assert body["provider"] == "local"
    assert "provider" in body["locked_fields"]


def test_apply_to_environ_respects_exports(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"AGENT_PROVIDER": "groq", "GROQ_API_KEY": "k"}))
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    monkeypatch.setenv("AGENT_PROVIDER", "local")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    user_settings.apply_to_environ()
    try:
        assert os.environ["AGENT_PROVIDER"] == "local"
        assert os.environ["GROQ_API_KEY"] == "k"
        user_settings.clear_inherited_environ()
        assert "GROQ_API_KEY" not in os.environ
        assert os.environ["AGENT_PROVIDER"] == "local"
    finally:
        monkeypatch.setattr(user_settings, "shell_exported", frozenset())
