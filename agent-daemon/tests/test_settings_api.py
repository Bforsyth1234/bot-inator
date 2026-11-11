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
from auth import auth_header  # noqa: E402
from main import app  # noqa: E402


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
    with TestClient(app, headers=auth_header()) as test_client:
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
    resp = client.put("/api/settings", json={"provider": "nonsense"})
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


# --- per-engine provider override (#26) ---------------------------------


def test_get_defaults_engine_overrides(client):
    body = client.get("/api/settings").json()
    assert body["provider_main"] == ""
    assert body["provider_analysis"] == ""
    assert body["provider_drafting"] == ""
    assert body["engine_providers"] == {
        "main": "local", "analysis": "local", "drafting": "local",
    }
    assert body["groq_api_key_required"] is False


def test_override_round_trip(client):
    resp = client.put("/api/settings", json={
        "provider_drafting": "GROQ", "groq_api_key": "gsk_test_1234abcd",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "local"
    assert body["provider_drafting"] == "groq"
    assert body["engine_providers"] == {
        "main": "local", "analysis": "local", "drafting": "groq",
    }
    assert body["running_engine_providers"]["drafting"] == "local"
    assert body["groq_api_key_required"] is True
    assert body["restart_required"] is True
    assert "gsk_test_1234abcd" not in resp.text
    saved = json.loads(client.settings_path.read_text())
    assert saved["AGENT_PROVIDER_DRAFTING"] == "groq"

    # "" (or "default") clears the override: back to the default.
    body = client.put("/api/settings", json={"provider_drafting": "default"}).json()
    assert body["provider_drafting"] == ""
    assert body["engine_providers"]["drafting"] == "local"
    assert "AGENT_PROVIDER_DRAFTING" not in json.loads(client.settings_path.read_text())


def test_override_needs_key_only_when_some_engine_uses_groq(client):
    # Groq drafting without a key: rejected.
    resp = client.put("/api/settings", json={"provider_drafting": "groq"})
    assert resp.status_code == 400
    assert not client.settings_path.exists()
    # Groq default but every engine overridden to local: no key needed.
    resp = client.put("/api/settings", json={
        "provider": "groq", "provider_main": "local",
        "provider_analysis": "local", "provider_drafting": "local",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["groq_api_key_required"] is False
    # Clearing one override brings Groq back for that engine: rejected.
    resp = client.put("/api/settings", json={"provider_main": ""})
    assert resp.status_code == 400
    assert json.loads(client.settings_path.read_text())["AGENT_PROVIDER_MAIN"] == "local"


def test_invalid_override_rejected(client):
    resp = client.put("/api/settings", json={"provider_analysis": "nonsense"})
    assert resp.status_code == 400
    assert "analysis" in resp.json()["detail"]


def test_shell_export_locks_override(client, monkeypatch):
    from config import settings
    monkeypatch.setattr(user_settings, "shell_exported",
                        frozenset({"AGENT_PROVIDER_MAIN"}))
    monkeypatch.setattr(settings, "provider_main", "groq")
    body = client.get("/api/settings").json()
    assert "provider_main" in body["locked_fields"]
    assert body["provider_main"] == "groq"
    assert body["running_engine_providers"]["main"] == "groq"
    # Saving needs a key, because the exported override is Groq.
    resp = client.put("/api/settings", json={"provider_main": "local"})
    assert resp.status_code == 400


# ---- cloud_personal_text toggle (#52) ---------------------------------------


@pytest.fixture
def cloud_main(client, monkeypatch):
    """A running cloud main engine with no env opt-in."""
    from config import settings
    monkeypatch.setattr(settings, "provider_main", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_1234abcd")
    monkeypatch.delenv("AGENT_CLOUD_PERSONAL_TEXT", raising=False)
    return client


def test_cloud_personal_text_defaults_off(cloud_main):
    from ai import privacy
    body = cloud_main.get("/api/settings").json()
    assert body["cloud_personal_text"] is False
    assert "cloud_personal_text" not in body["locked_fields"]
    assert privacy.should_redact("main") is True


def test_put_cloud_personal_text_flips_redaction_live(cloud_main):
    from ai import privacy
    resp = cloud_main.put("/api/settings", json={"cloud_personal_text": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cloud_personal_text"] is True
    # Applies without a restart, so it must not ask for one.
    assert body["restart_required"] is False
    saved = json.loads(cloud_main.settings_path.read_text())
    assert saved["AGENT_CLOUD_PERSONAL_TEXT"] == "1"
    assert privacy.should_redact("main") is False
    assert privacy.redact_text("hi there", "main") == "hi there"

    resp = cloud_main.put("/api/settings", json={"cloud_personal_text": False})
    assert resp.json()["cloud_personal_text"] is False
    assert privacy.should_redact("main") is True
    assert privacy.redact_text("hi there", "main") == privacy.PLACEHOLDER


def test_saved_off_beats_dotenv_opt_in(cloud_main, monkeypatch):
    """A ``=1`` from .env (not a shell export) is overridden by the toggle."""
    from ai import privacy
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    assert privacy.personal_text_allowed() is True
    cloud_main.put("/api/settings", json={"cloud_personal_text": False})
    assert privacy.should_redact("main") is True


def test_shell_export_locks_cloud_personal_text(cloud_main, monkeypatch):
    from ai import privacy
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "0")
    monkeypatch.setattr(user_settings, "shell_exported",
                        frozenset({"AGENT_CLOUD_PERSONAL_TEXT"}))
    cloud_main.put("/api/settings", json={"cloud_personal_text": True})
    body = cloud_main.get("/api/settings").json()
    assert "cloud_personal_text" in body["locked_fields"]
    assert body["cloud_personal_text"] is False
    assert privacy.should_redact("main") is True


def test_status_frame_reflects_cloud_personal_text(cloud_main):
    import main
    assert main._status_frame().payload.cloud_personal_text is False
    cloud_main.put("/api/settings", json={"cloud_personal_text": True})
    assert main._status_frame().payload.cloud_personal_text is True
