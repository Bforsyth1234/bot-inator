"""#136: Settings fields for the local MLX models (main, analysis, drafting).

Isolated from the developer's real ``agent-daemon/.env``,
``~/.bot-inator/settings.json``, shell exports and Hugging Face cache: the
settings file, ``.env`` and HF cache are temp paths, and ``MLX_*_MODEL`` is
removed from the environment. No model is loaded or downloaded: ``mlx_lm``
is a fake module.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
import user_settings  # noqa: E402
from ai.mlx_engine import MLXEngine, local_model_bytes  # noqa: E402
from auth import auth_header  # noqa: E402
from config import default_mlx_models, settings  # noqa: E402
from main import app  # noqa: E402

KEYS = {"main": "MLX_MAIN_MODEL", "analysis": "MLX_ANALYSIS_MODEL",
        "drafting": "MLX_DRAFTING_MODEL"}
ATTRS = {"main": "model_path", "analysis": "analysis_model_path",
         "drafting": "drafting_model_path"}
GB = 1024**3


def _fake_repo(cache, repo_id, size, *, weights=True):
    """A Hugging Face cache entry for ``repo_id`` with ``size`` bytes (sparse)."""
    repo = cache / ("models--" + repo_id.replace("/", "--"))
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("abc123")
    snap = repo / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    blob = repo / "blobs" / "deadbeef"
    blob.parent.mkdir()
    with open(blob, "wb") as fh:
        fh.truncate(size)
    (snap / ("model.safetensors" if weights else "config.json")).symlink_to(blob)
    return repo


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp settings file, .env and HF cache; no MLX exports; local engines
    running their defaults."""
    settings_path = tmp_path / "settings.json"
    dotenv = tmp_path / ".env"
    dotenv.write_text("")
    cache = tmp_path / "hf-cache"
    cache.mkdir()
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(config, "dotenv_path", lambda: dotenv)
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    monkeypatch.setattr(main, "_physical_memory_cache", [None])
    monkeypatch.setattr(settings, "provider", "local")
    monkeypatch.setattr(settings, "groq_api_key", "")
    defaults = default_mlx_models()
    for role in KEYS:
        monkeypatch.delenv(KEYS[role], raising=False)
        monkeypatch.setattr(settings, f"provider_{role}", "")
        monkeypatch.setattr(settings, ATTRS[role], defaults[KEYS[role]])
    return SimpleNamespace(settings_path=settings_path, dotenv=dotenv,
                           cache=cache, defaults=defaults)


@pytest.fixture
def client(env):
    with TestClient(app, headers=auth_header()) as test_client:
        yield test_client


def _saved(env):
    return json.loads(env.settings_path.read_text()) if env.settings_path.exists() else {}


def test_view_reports_mlx_keys_value_default_lock_and_local(env, client):
    env.settings_path.write_text(json.dumps({
        "MLX_MAIN_MODEL": "org/main-from-settings",
        "AGENT_PROVIDER_DRAFTING": "groq",
        "GROQ_API_KEY": "gsk_test_1234abcd",
    }))
    env.dotenv.write_text("MLX_ANALYSIS_MODEL=org/analysis-from-dotenv\n")
    body = client.get("/api/settings").json()
    models = body["mlx_models"]
    assert set(models) == {"main", "analysis", "drafting"}
    main_m, analysis_m, drafting_m = models["main"], models["analysis"], models["drafting"]
    assert main_m["key"] == "MLX_MAIN_MODEL"
    assert (main_m["value"], main_m["source"]) == ("org/main-from-settings", "settings")
    assert (analysis_m["value"], analysis_m["source"]) == ("org/analysis-from-dotenv", "env_file")
    assert (drafting_m["value"], drafting_m["source"]) == ("", "default")
    for role, m in models.items():
        assert m["default"] == env.defaults[KEYS[role]]
        assert m["locked"] is False
        assert m["running"] == env.defaults[KEYS[role]]
        assert m["load_error"] == ""
    assert main_m["local"] is True and analysis_m["local"] is True
    assert drafting_m["local"] is False  # saved as Groq
    assert all(m["running_local"] for m in models.values())
    assert body["mlx_memory_note"] == ""  # sizes unknown: empty cache
    assert not any(f.startswith("mlx_") for f in body["locked_fields"])


def test_shell_export_is_locked_and_save_does_not_override(env, client, monkeypatch):
    monkeypatch.setenv("MLX_MAIN_MODEL", "org/exported")
    monkeypatch.setattr(user_settings, "shell_exported", frozenset({"MLX_MAIN_MODEL"}))
    monkeypatch.setattr(settings, "model_path", "org/exported")
    resp = client.put("/api/settings", json={
        "mlx_main_model": "org/from-ui", "mlx_analysis_model": "org/analysis-ui",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "MLX_MAIN_MODEL" not in _saved(env)
    assert _saved(env)["MLX_ANALYSIS_MODEL"] == "org/analysis-ui"
    m = body["mlx_models"]["main"]
    assert (m["value"], m["source"], m["locked"]) == ("org/exported", "shell", True)
    assert "mlx_main_model" in body["locked_fields"]
    assert "mlx_analysis_model" not in body["locked_fields"]


def test_saving_empty_removes_the_key(env, client):
    env.settings_path.write_text(json.dumps({"MLX_DRAFTING_MODEL": "org/drafting"}))
    body = client.put("/api/settings", json={"mlx_drafting_model": "  "}).json()
    assert "MLX_DRAFTING_MODEL" not in _saved(env)
    assert "" not in _saved(env).values()
    assert body["mlx_models"]["drafting"]["value"] == ""
    assert body["mlx_models"]["drafting"]["source"] == "default"


def test_saving_the_default_is_not_pinned(env, client):
    default = env.defaults["MLX_MAIN_MODEL"]
    client.put("/api/settings", json={"mlx_main_model": default})
    assert "MLX_MAIN_MODEL" not in _saved(env)


def test_changing_a_model_sets_restart_required(env, client):
    assert client.get("/api/settings").json()["restart_required"] is False
    body = client.put("/api/settings", json={"mlx_main_model": "org/new-main"}).json()
    assert _saved(env)["MLX_MAIN_MODEL"] == "org/new-main"
    assert body["restart_required"] is True
    # Back to the default: nothing to restart for.
    body = client.put("/api/settings", json={"mlx_main_model": ""}).json()
    assert body["restart_required"] is False


def test_model_change_for_a_cloud_role_needs_no_restart(env, client, monkeypatch):
    # Drafting runs on Groq now and after a restart: its MLX model is unused.
    monkeypatch.setattr(settings, "provider_drafting", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_1234abcd")
    env.settings_path.write_text(json.dumps({
        "MLX_DRAFTING_MODEL": "org/unused", "AGENT_PROVIDER_DRAFTING": "groq",
        "GROQ_API_KEY": "gsk_test_1234abcd",
    }))
    assert client.get("/api/settings").json()["restart_required"] is False


# ---- bad model id: fallback to the default (fake loader) --------------------


def _fake_mlx(monkeypatch, bad):
    calls = []

    def load(name, **kwargs):
        calls.append(name)
        if name in bad:
            raise ValueError(f"Repository Not Found for url: https://huggingface.co/{name}\nmore")
        return object(), object()

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=load))
    return calls


def test_bad_model_id_falls_back_to_cached_default(env, monkeypatch):
    _fake_repo(env.cache, "org/default", 1000)
    calls = _fake_mlx(monkeypatch, bad={"org/typo"})
    engine = MLXEngine("org/typo", fallback_model="org/default")
    asyncio.run(engine.load())
    assert calls == ["org/typo", "org/default"]
    assert engine.loaded and engine.model_name == "org/default"
    assert "org/typo failed to load: Repository Not Found" in engine.load_error
    assert "Using the default org/default" in engine.load_error
    assert "\n" not in engine.load_error


def test_fallback_never_downloads_an_uncached_default(env, monkeypatch):
    calls = _fake_mlx(monkeypatch, bad={"org/typo"})
    engine = MLXEngine("org/typo", fallback_model="org/not-cached")
    with pytest.raises(ValueError):
        asyncio.run(engine.load())
    assert calls == ["org/typo"]  # the default was never fetched
    assert not engine.loaded
    assert "not downloaded" in engine.load_error


def test_good_model_loads_without_error(env, monkeypatch):
    _fake_repo(env.cache, "org/default", 1000)
    calls = _fake_mlx(monkeypatch, bad=set())
    engine = MLXEngine("org/fine", fallback_model="org/default")
    asyncio.run(engine.load())
    assert calls == ["org/fine"] and engine.model_name == "org/fine"
    assert engine.load_error == ""


def test_build_engines_passes_the_role_default_as_fallback(env):
    built = main._build_engines()
    for role, eng in zip(("main", "analysis", "drafting"), built):
        assert isinstance(eng, MLXEngine)
        assert eng.fallback_model == env.defaults[KEYS[role]]


def test_settings_shows_the_load_error(env, client, monkeypatch):
    _fake_repo(env.cache, "org/default", 1000)
    _fake_mlx(monkeypatch, bad={"org/typo"})
    engine = MLXEngine("org/typo", fallback_model="org/default")
    asyncio.run(engine.load())
    monkeypatch.setattr(app.state, "engine", engine)
    m = client.get("/api/settings").json()["mlx_models"]["main"]
    assert "org/typo failed to load" in m["load_error"]
    assert m["loaded"] == "org/default"


# ---- memory note -------------------------------------------------------------


def test_local_model_bytes_reads_only_the_local_cache(env, tmp_path):
    _fake_repo(env.cache, "org/a", 5 * GB)
    _fake_repo(env.cache, "org/meta-only", 10, weights=False)
    assert local_model_bytes("org/a", env.cache) == 5 * GB
    assert local_model_bytes("org/missing", env.cache) is None
    assert local_model_bytes("org/meta-only", env.cache) is None
    folder = tmp_path / "my-model"
    folder.mkdir()
    with open(folder / "weights.safetensors", "wb") as fh:
        fh.truncate(2 * GB)
    assert local_model_bytes(str(folder), env.cache) == 2 * GB


def test_memory_note_when_known_sizes_exceed_memory(env):
    for name in ("org/a", "org/b", "org/c"):
        _fake_repo(env.cache, name, 6 * GB)
    note = main.mlx_memory_note(["org/a", "org/b", "org/c"], 16.0, cache_dir=env.cache)
    assert "18.0 GB" in note and "16 GB" in note
    assert "\n" not in note
    # Plenty of memory: no note.
    assert main.mlx_memory_note(["org/a", "org/b", "org/c"], 64.0, cache_dir=env.cache) == ""


def test_no_memory_note_when_a_size_is_unknown(env):
    _fake_repo(env.cache, "org/a", 30 * GB)
    _fake_repo(env.cache, "org/b", 30 * GB)
    assert main.mlx_memory_note(["org/a", "org/b", "org/unknown"], 16.0,
                                cache_dir=env.cache) == ""
    assert main.mlx_memory_note(["org/a", "org/b"], None, cache_dir=env.cache) == ""


def test_memory_note_in_settings_and_save_still_works(env, client, monkeypatch):
    for name in ("org/a", "org/b", "org/c"):
        _fake_repo(env.cache, name, 6 * GB)
    monkeypatch.setattr(main, "_physical_memory_cache", [16.0])
    resp = client.put("/api/settings", json={
        "mlx_main_model": "org/a", "mlx_analysis_model": "org/b",
        "mlx_drafting_model": "org/c",
    })
    assert resp.status_code == 200
    assert _saved(env)["MLX_DRAFTING_MODEL"] == "org/c"
    assert "18.0 GB" in resp.json()["mlx_memory_note"]
