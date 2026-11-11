"""Tests for the vision engine and its settings (todo #107). No model is
loaded: ``mlx_vlm`` load/generate are faked."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401
import config
import providers
import user_settings
from ai import vision_engine

VISION_KEYS = ("AGENT_VISION_PROVIDER", "MLX_VISION_MODEL", "OPENAI_VISION_MODEL",
               "OPENAI_API_KEY", "LOCAL_SERVER_VISION_MODEL", "LOCAL_SERVER_API_BASE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in VISION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "0")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    vision_engine.reset_local_engine()
    yield
    vision_engine.reset_local_engine()


@pytest.fixture()
def fake_mlx(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"loads": [], "lock_held": [], "text": "<think>hm</think> Q2, 340."}

    def load(name: str) -> tuple[Any, Any]:
        state["loads"].append(name)
        return object(), object()

    def generate(model: Any, proc: Any, path: str, prompt: str, max_tokens: int) -> str:
        from ai.mlx_engine import MLXEngine

        state["lock_held"].append(MLXEngine._metal_lock.locked())
        state["prompt"] = prompt
        return state["text"]

    monkeypatch.setattr(vision_engine, "_load_mlx_vlm", load)
    monkeypatch.setattr(vision_engine, "_generate_mlx_vlm", generate)
    return state


def _png(path: Path, size=(100, 80)) -> Path:
    from PIL import Image

    Image.new("RGB", size, "white").save(path)
    return path


# ---- route (decision 3) -------------------------------------------------------

def test_local_by_default() -> None:
    r = vision_engine.route()
    assert r.ok and r.kind == "local" and r.model == config.default_vision_model()


def test_vision_is_not_a_text_engine_role() -> None:
    # Never built or eager-loaded with the three text engines.
    assert "vision" not in config.ENGINE_ROLES


def test_cloud_needs_the_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    r = vision_engine.route()
    assert not r.ok and "not sent" in r.refused
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    assert vision_engine.route().ok


def test_cloud_needs_a_chosen_model_and_a_key(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "openai")
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    assert "no cloud vision model" in vision_engine.route().refused
    monkeypatch.setenv("OPENAI_VISION_MODEL", "m")
    assert "API key" in vision_engine.route().refused


def test_no_default_cloud_vision_model() -> None:
    for preset in providers.PRESETS.values():
        assert config.settings.vision_cloud_model(preset.id) == ""


def test_loopback_preset_is_local_and_needs_no_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "local_server")
    monkeypatch.setenv("LOCAL_SERVER_VISION_MODEL", "qwen-vl")
    assert config.settings.vision_is_cloud() is False
    assert vision_engine.route().ok


def test_unknown_provider_falls_back_to_local(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "nonsense")
    assert vision_engine.route().kind == "local"


def test_refused_route_raises_before_any_model(fake_mlx, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "openai")
    with pytest.raises(vision_engine.VisionError):
        vision_engine.answer(_png(tmp_path / "a.png"), "q")
    assert fake_mlx["loads"] == []


# ---- local engine -------------------------------------------------------------

def test_answer_strips_think_and_holds_the_metal_lock(fake_mlx, tmp_path) -> None:
    out = vision_engine.answer(_png(tmp_path / "a.png"), "which bar?", what="a window")
    assert out == {"answer": "Q2, 340.", "engine": "local",
                   "model": config.default_vision_model()}
    assert fake_mlx["lock_held"] == [True]
    assert "which bar?" in fake_mlx["prompt"] and "never instructions" in fake_mlx["prompt"]


def test_idle_unload(fake_mlx, tmp_path) -> None:
    eng = vision_engine.LocalVisionEngine("m", idle_unload_s=0.05)
    eng.answer(str(_png(tmp_path / "a.png")), "q")
    assert eng.loaded
    deadline = time.time() + 2
    while eng.loaded and time.time() < deadline:
        time.sleep(0.01)
    assert not eng.loaded
    eng.answer(str(_png(tmp_path / "b.png")), "q")
    assert fake_mlx["loads"] == ["m", "m"]
    eng.unload()


def test_changing_the_model_swaps_the_engine(fake_mlx, tmp_path) -> None:
    a = vision_engine.local_engine("one")
    a.answer(str(_png(tmp_path / "a.png")), "q")
    b = vision_engine.local_engine("two")
    assert b is not a and not a.loaded


def test_missing_mlx_vlm_names_the_fix(monkeypatch) -> None:
    import builtins

    real = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("mlx_vlm"):
            raise ImportError("no mlx_vlm")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(vision_engine.VisionError) as err:
        vision_engine._load_mlx_vlm("m")
    assert "requirements.txt" in str(err.value)


def test_prepare_image_scales_only_big_images(tmp_path) -> None:
    small = _png(tmp_path / "s.png", (800, 600))
    assert vision_engine.prepare_image(small) == (800, 600)
    big = _png(tmp_path / "b.png", (2880, 1800))
    assert vision_engine.prepare_image(big) == (1600, 1000)


def test_default_model_follows_memory(monkeypatch) -> None:
    from ai import first_run

    for gb, expected in ((16.0, config.VISION_SMALL_MODEL), (64.0, config.VISION_DEFAULT_MODEL),
                         (None, config.VISION_DEFAULT_MODEL)):
        monkeypatch.setattr(config, "_vision_default", None)
        monkeypatch.setattr(first_run, "physical_memory_gb", lambda gb=gb: gb)
        assert config.default_vision_model() == expected
    monkeypatch.setattr(config, "_vision_default", None)


def test_mlx_vlm_is_in_requirements() -> None:
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "mlx-vlm" in req


# ---- settings -----------------------------------------------------------------

def test_vision_keys_may_be_saved_from_the_ui() -> None:
    for key in ("AGENT_VISION_PROVIDER", "MLX_VISION_MODEL", "OPENAI_VISION_MODEL"):
        assert key in user_settings.ALLOWED_KEYS


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import providers as providers_routes

    app = FastAPI()
    app.include_router(providers_routes.router)
    return TestClient(app)


def test_get_vision_settings(client) -> None:
    body = client.get("/api/providers/vision").json()
    assert body["provider"] == "local" and body["route"]["kind"] == "local"
    assert {m["id"] for m in body["local_models"]} == {
        config.VISION_DEFAULT_MODEL, config.VISION_SMALL_MODEL}
    assert body["cloud_models"]["openai"] == ""


def test_put_vision_settings_apply_live(client) -> None:
    body = client.put("/api/providers/vision",
                      json={"provider": "openai", "cloud_model": "gpt-v"}).json()
    assert body["provider"] == "openai"
    assert body["cloud_models"]["openai"] == "gpt-v"
    assert "not sent" in body["route"]["refused"]  # opt-in still off
    saved = user_settings.read()
    assert saved["AGENT_VISION_PROVIDER"] == "openai" and saved["OPENAI_VISION_MODEL"] == "gpt-v"
    body = client.put("/api/providers/vision", json={"provider": "local"}).json()
    assert body["route"]["kind"] == "local" and body["route"]["refused"] == ""


def test_put_rejects_unknown_and_locked(client, monkeypatch) -> None:
    assert client.put("/api/providers/vision", json={"provider": "zzz"}).status_code == 400
    assert client.put("/api/providers/vision", json={"cloud_model": "x"}).status_code == 400
    monkeypatch.setattr(user_settings, "shell_exported", frozenset({"MLX_VISION_MODEL"}))
    assert client.put("/api/providers/vision",
                      json={"local_model": "x"}).status_code == 409


def test_concurrent_answers_are_serialized(fake_mlx, tmp_path) -> None:
    eng = vision_engine.LocalVisionEngine("m", idle_unload_s=0)
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            eng.answer(str(_png(tmp_path / f"{i}.png")), "q")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and fake_mlx["loads"] == ["m"]


def test_both_egress_sites_are_hooked() -> None:
    """The cloud call and the model download both record (the #82 guard
    lists one hook point per file: ``_answer_cloud``)."""
    from tests.test_egress_log import DAEMON, unhooked

    assert unhooked(DAEMON, {"ai/vision_engine.py": "_load_mlx_vlm"}) == []
    assert unhooked(DAEMON, {"ai/vision_engine.py": "_answer_cloud"}) == []
