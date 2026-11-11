"""#93: first-run engine choice: recommendation, pending gate (no engine,
no download), model folders, download only from its button, the choice
saved through user_settings, existing installs untouched."""
from __future__ import annotations

import json
import logging
import os

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
import providers  # noqa: E402
import user_settings  # noqa: E402
from ai import first_run  # noqa: E402
from ai.groq_engine import OpenAICompatibleEngine  # noqa: E402
from ai.mlx_engine import MLXEngine  # noqa: E402
from auth import auth_header  # noqa: E402
from config import MLX_DEFAULT_MODELS  # noqa: E402

SECRET = "sk-ant-test-SECRET-1234abcd"
STOCK = {
    "main": MLX_DEFAULT_MODELS["MLX_MAIN_MODEL"],
    "analysis": MLX_DEFAULT_MODELS["MLX_ANALYSIS_MODEL"],
    "drafting": MLX_DEFAULT_MODELS["MLX_DRAFTING_MODEL"],
}
_PROVIDER_KEYS = (
    "AGENT_PROVIDER", "AGENT_PROVIDER_MAIN", "AGENT_PROVIDER_ANALYSIS",
    "AGENT_PROVIDER_DRAFTING", "MLX_MAIN_MODEL", "MLX_ANALYSIS_MODEL",
    "MLX_DRAFTING_MODEL",
)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A Mac with no ~/.bot-inator/settings.json and an empty HF cache."""
    path = tmp_path / "home" / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    monkeypatch.delenv(first_run.SKIP_ENV, raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf"))
    for key in (*_PROVIDER_KEYS, *providers.preset_env_keys()):
        monkeypatch.delenv(key, raising=False)
    from config import settings
    monkeypatch.setattr(settings, "model_path", STOCK["main"])
    monkeypatch.setattr(settings, "analysis_model_path", STOCK["analysis"])
    monkeypatch.setattr(settings, "drafting_model_path", STOCK["drafting"])
    monkeypatch.setattr(first_run, "downloader", first_run.Downloader())
    return path


@pytest.fixture
def downloads(monkeypatch):
    """Every Hugging Face download and MLX load the code attempts."""
    calls: list[str] = []
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download",
                        lambda repo, **kw: calls.append(f"download:{repo}"))

    async def _load(self):
        calls.append(f"load:{self.model_name}")

    monkeypatch.setattr(MLXEngine, "load", _load)
    return calls


@pytest.fixture
def client(fresh, downloads, monkeypatch):
    monkeypatch.setenv("AGENT_EAGER_LOAD", "1")  # the gate, not this, must stop loads
    with TestClient(main.app, headers=auth_header()) as test_client:
        test_client.settings_path = fresh
        yield test_client


def _mlx_folder(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text("{}")
    (path / "model.safetensors").write_bytes(b"\0")
    return path


def _cache_stock(hf):
    for model in STOCK.values():
        snap = hf / ("models--" + model.replace("/", "--")) / "snapshots" / "abc"
        _mlx_folder(snap)


# --- recommendation ----------------------------------------------------------

@pytest.mark.parametrize("gb,choice", [
    (8, "cloud"), (16, "cloud"), (23.9, "cloud"), (24, "local"), (64, "local"), (128, "local"),
])
def test_recommend_by_memory(gb, choice):
    rec = first_run.recommend(gb)
    assert rec["choice"] == choice
    assert f"{gb:.0f} GB" in rec["reason"]


def test_unknown_memory_recommends_local():
    assert first_run.recommend(None)["choice"] == "local"


def test_physical_memory_is_read():
    gb = first_run.physical_memory_gb()
    assert gb is not None and gb > 1


# --- pending gate ------------------------------------------------------------

def test_pending_until_settings_exist(fresh, monkeypatch):
    assert first_run.is_pending()
    fresh.parent.mkdir(parents=True)
    fresh.write_text("{}")
    assert not first_run.is_pending()
    fresh.unlink()
    monkeypatch.setenv(first_run.SKIP_ENV, "1")
    assert not first_run.is_pending()


def test_pending_start_loads_and_downloads_nothing(client, downloads):
    engines = (main.app.state.engine,)
    assert all(isinstance(e, first_run.PendingEngine) for e in engines)
    view = client.get("/api/first_run").json()
    assert view["pending"] is True
    assert view["local"]["download"]["total"] > 12 * 1024**3
    assert downloads == []


def test_pending_engine_never_loads():
    import asyncio

    engine = first_run.PendingEngine("main")
    with pytest.raises(RuntimeError, match="first-run"):
        asyncio.run(engine.load())
    with pytest.raises(RuntimeError):
        engine.generate_sync("hi")
    assert asyncio.run(engine.evaluate_event({})) == ""


def test_build_engines_builds_stand_ins_while_pending(fresh, downloads):
    built = main._build_engines()
    assert all(isinstance(e, first_run.PendingEngine) for e in built)


# --- model folders -----------------------------------------------------------

def test_mlx_folder_passes(tmp_path):
    assert first_run.check_model_folder(str(_mlx_folder(tmp_path / "m"))) is None


def test_gguf_folder_points_at_local_server(tmp_path):
    folder = tmp_path / "g"
    folder.mkdir()
    (folder / "model.Q4_K_M.gguf").write_bytes(b"\0")
    reason = first_run.check_model_folder(str(folder))
    assert reason and "GGUF" in reason and "Local server" in reason


@pytest.mark.parametrize("make", ["empty", "missing", "no_weights"])
def test_bad_folders_are_refused(tmp_path, make):
    folder = tmp_path / "x"
    if make != "missing":
        folder.mkdir()
    if make == "no_weights":
        (folder / "config.json").write_text("{}")
    assert first_run.check_model_folder(str(folder))


# --- download only from its button ---------------------------------------------

def test_download_starts_only_from_its_button(client, downloads):
    client.get("/api/first_run")
    assert downloads == []
    state = client.post("/api/first_run/download").json()
    first_run.downloader._thread.join(timeout=5)
    assert sorted(downloads) == sorted(f"download:{m}" for m in STOCK.values())
    assert state["total"] > 0
    assert client.get("/api/first_run").json()["download"]["finished"] is True


# --- the choice --------------------------------------------------------------

def test_cloud_without_a_key_is_refused(client):
    resp = client.post("/api/first_run/choice", json={"choice": "cloud", "provider": "anthropic"})
    assert resp.status_code == 400 and "key" in resp.json()["detail"]
    assert not client.settings_path.exists()


def test_cloud_needs_a_model_when_the_preset_has_no_default(client):
    resp = client.post("/api/first_run/choice",
                       json={"choice": "cloud", "provider": "openai", "api_key": SECRET})
    assert resp.status_code == 400 and "model" in resp.json()["detail"]
    assert not client.settings_path.exists()


def test_loopback_preset_is_not_a_cloud_choice(client):
    resp = client.post("/api/first_run/choice",
                       json={"choice": "cloud", "provider": "local_server"})
    assert resp.status_code == 400


def test_choosing_cloud_saves_settings_and_the_restart_builds_cloud_engines(
        client, downloads, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    resp = client.post("/api/first_run/choice",
                       json={"choice": "cloud", "provider": "anthropic", "api_key": SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json()["restart_required"] is True and SECRET not in resp.text
    saved = json.loads(client.settings_path.read_text())
    assert saved == {"AGENT_FIRST_RUN_CHOICE": "cloud", "AGENT_PROVIDER": "anthropic",
                     "ANTHROPIC_API_KEY": SECRET}
    assert oct(client.settings_path.stat().st_mode & 0o777) == "0o600"
    assert not first_run.is_pending()
    assert SECRET not in caplog.text
    # What the restart does: read the file, build the engines.
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    user_settings.apply_to_environ()
    monkeypatch.setattr(main, "settings", config.Settings())
    built = main._build_engines()
    assert all(isinstance(e, OpenAICompatibleEngine) and e.provider == "anthropic" for e in built)
    assert downloads == []  # Cloud never downloads the local models


def test_choosing_twice_is_refused_and_changes_nothing(client):
    client.settings_path.parent.mkdir(parents=True)
    client.settings_path.write_text('{"AGENT_PROVIDER": "local"}')
    resp = client.post("/api/first_run/choice",
                       json={"choice": "cloud", "provider": "anthropic", "api_key": SECRET})
    assert resp.status_code == 409
    assert client.post("/api/first_run/download").status_code == 409
    assert json.loads(client.settings_path.read_text()) == {"AGENT_PROVIDER": "local"}
    assert client.get("/api/first_run").json()["pending"] is False


def test_local_download_source_needs_the_models_downloaded(client, fresh, tmp_path):
    body = {"choice": "local", "local_source": "download"}
    resp = client.post("/api/first_run/choice", json=body)
    assert resp.status_code == 400 and "Download" in resp.json()["detail"]
    _cache_stock(tmp_path / "hf")
    assert client.post("/api/first_run/choice", json=body).status_code == 200
    saved = json.loads(fresh.read_text())
    assert saved == {"AGENT_FIRST_RUN_CHOICE": "local", "AGENT_PROVIDER": "local"}


def test_local_folder_source_saves_paths_and_refuses_gguf(client, fresh, tmp_path):
    good = str(_mlx_folder(tmp_path / "mlx"))
    gguf = tmp_path / "gguf"
    gguf.mkdir()
    (gguf / "m.gguf").write_bytes(b"\0")
    bad = {"choice": "local", "local_source": "folder",
           "model_folders": {"main": good, "analysis": str(gguf), "drafting": good}}
    resp = client.post("/api/first_run/choice", json=bad)
    assert resp.status_code == 400 and "GGUF" in resp.json()["detail"]
    ok = {**bad, "model_folders": {r: good for r in STOCK}}
    assert client.post("/api/first_run/choice", json=ok).status_code == 200
    saved = json.loads(fresh.read_text())
    assert saved["MLX_MAIN_MODEL"] == good and saved["AGENT_PROVIDER"] == "local"


def test_local_server_must_be_loopback(client, fresh):
    models = {r: "llama" for r in STOCK}
    lan = {"choice": "local", "local_source": "server",
           "server_base": "http://192.168.1.5:11434/v1", "server_models": models}
    assert client.post("/api/first_run/choice", json=lan).status_code == 400
    ok = {**lan, "server_base": "http://localhost:1234/v1"}
    assert client.post("/api/first_run/choice", json=ok).status_code == 200
    saved = json.loads(fresh.read_text())
    assert saved["AGENT_PROVIDER"] == "local_server"
    assert saved["LOCAL_SERVER_API_BASE"] == "http://localhost:1234/v1"


def test_mixed_sends_only_drafting_to_the_cloud(client, fresh, tmp_path, monkeypatch):
    _cache_stock(tmp_path / "hf")
    resp = client.post("/api/first_run/choice", json={
        "choice": "mixed", "local_source": "download", "provider": "groq", "api_key": SECRET,
    })
    assert resp.status_code == 200, resp.text
    saved = json.loads(fresh.read_text())
    assert saved["AGENT_PROVIDER"] == "local" and saved["AGENT_PROVIDER_DRAFTING"] == "groq"
    assert saved["GROQ_API_KEY"] == SECRET
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    user_settings.apply_to_environ()
    s = config.Settings()
    assert not s.is_cloud("main") and not s.is_cloud("analysis") and s.is_cloud("drafting")


def test_view_offers_cloud_presets_and_says_what_leaves(client):
    view = client.get("/api/first_run").json()
    ids = {p["id"] for p in view["presets"]}
    assert {"groq", "openai", "anthropic", "gemini", "xai"} <= ids
    assert "local_server" not in ids and "custom" not in ids
    by_id = {c["id"]: c for c in view["choices"]}
    assert by_id["local"]["leaves_mac"].startswith("Nothing")
    assert "Only tool drafts" in by_id["mixed"]["leaves_mac"]
    assert "Privacy ledger" in view["ledger_line"]
    assert view["recommended"]["choice"] in ("local", "cloud")
    assert next(p for p in view["presets"] if p["id"] == "groq")["note"]
