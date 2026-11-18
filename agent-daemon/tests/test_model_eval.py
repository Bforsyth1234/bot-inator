"""#120: use the personal model only if it beats the base model; rollback."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import user_settings
from ai import mlx_engine as mlx_mod
from ai import model_eval as me
from ai.mlx_engine import MLXEngine
from ai.model_eval import ACTIVE_KEY, PersonalModel, list_adapters, run_gate, switch_offer
from routes import personal_model as pm_routes
from routes import training as training_routes

BASE = "base/model"
SECRET = "SECRET-ROW-TEXT reply to Sam about the lease"


# ---- helpers ---------------------------------------------------------------


def _make_adapter(models_dir: Path, n: int, *, base: str = BASE, status: str = "done",
                  weights: bool = True, gate: Optional[dict] = None) -> Path:
    folder = models_dir / str(n)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(json.dumps({
        "base_model": base, "created_at": 1_700_000_000.0 + n, "status": status,
        "usable_rows": 320, "final_loss": 0.5}))
    if weights:
        (folder / "adapters.safetensors").write_bytes(b"\0")
    if gate is not None:
        me.write_gate(folder, gate, at=1_700_000_500.0)
    return folder


class FakeEngine:
    """A local-MLX-like engine: swap/unload recorded, no model."""

    supports_adapters = True

    def __init__(self, model_name: str = BASE, adapter_path: Optional[str] = None) -> None:
        self.model_name = model_name
        self.adapter_path = adapter_path
        self.swaps: list[tuple[str, Optional[str]]] = []
        self.unloads = 0
        self._loaded = True

    @property
    def current_adapter(self) -> Optional[str]:
        return self.adapter_path if self._loaded else None

    @property
    def adapter_active(self) -> bool:
        return bool(self._loaded and self.adapter_path)

    async def swap(self, model: str, adapter_path: Optional[str] = None) -> None:
        self.swaps.append((model, adapter_path))
        self.model_name, self.adapter_path, self._loaded = model, adapter_path, True

    async def unload(self) -> None:
        self.unloads += 1
        self._loaded = False


class CloudEngine:
    provider = "groq"
    model_name = "openai/gpt-oss-120b"

    def __init__(self) -> None:
        self.swaps: list = []

    async def swap(self, model: str) -> None:  # pragma: no cover - must not be called
        self.swaps.append(model)


def _personal(per_repeat_ok: list[int], n: int = 40) -> dict:
    cases = []
    for c in range(n):
        runs = [{"ok": c < k, "closeness": 0.7 if c < k else None,
                 "calls": [{"tool": "draft_email", "args": {"body": SECRET}}]} for k in per_repeat_ok]
        cases.append({"id": f"r{c}", "expected": "offer", "prompt": SECRET, "runs": runs})
    return {"suite": "personal", "cases": cases}


def _suite(name: str, ok_runs: int, cases: int = 40, repeats: int = 3) -> dict:
    runs = cases * repeats
    return {"suite": name, "summary": {"cases": cases, "runs": runs, "ok_runs": ok_runs,
                                       "accuracy": round(ok_runs / runs, 3)},
            "cases": [{"id": f"{name}{i}", "runs": [{"ok": True, "got": SECRET}]} for i in range(3)]}


def _safety(unsafe: bool = False) -> dict:
    safe = {"ok": True, "calls": [{"tool": "read_email", "args": {}}]}
    bad = {"ok": False, "calls": [{"tool": "move_file", "args": {"src": SECRET}}]}
    return {"suite": "safety", "cases": [{"id": "inj", "runs": [bad if unsafe else safe] * 3}]}


SCENARIOS = {
    # name: (base personal per repeat, adapter per repeat, adapter safety unsafe?, adapter tool_calls ok)
    "wins": ([20, 20, 20], [24, 24, 24], False, 100),
    "worse": ([24, 24, 24], [20, 20, 20], False, 100),
    "not_enough": ([20, 20, 20], [21, 21, 21], False, 100),     # +2.5 points < +5 margin
    "safety": ([20, 20, 20], [30, 30, 30], True, 100),
    "regression": ([20, 20, 20], [30, 30, 30], False, 60),
}


class Fakes:
    """evaluate / evaluate_personal fakes that answer by the engine's adapter."""

    def __init__(self, scenario: str, fail_on: Optional[str] = None) -> None:
        self.base_p, self.ad_p, self.unsafe, self.tc_ok = SCENARIOS[scenario]
        self.calls: list[dict] = []
        self.engines: list[FakeEngine] = []
        self.fail_on = fail_on

    def factory(self, model: str, adapter: Optional[str] = None) -> FakeEngine:
        eng = FakeEngine(model, adapter)
        self.engines.append(eng)
        return eng

    async def evaluate_personal(self, corpus: Any, *, engine: Any, repeats: int, settings: Any) -> dict:
        self.calls.append({"suite": "personal", "adapter": engine.adapter_path, "save": False,
                           "repeats": repeats, "engine": engine})
        return _personal(self.ad_p if engine.adapter_path else self.base_p)

    async def evaluate(self, suite: str, *, engine: Any, save: bool, repeats: int, settings: Any) -> dict:
        self.calls.append({"suite": suite, "adapter": engine.adapter_path, "save": save,
                           "repeats": repeats, "engine": engine})
        if suite == self.fail_on and engine.adapter_path:
            raise RuntimeError("model crashed")
        ad = bool(engine.adapter_path)
        if suite == "safety":
            return _safety(unsafe=ad and self.unsafe)
        if suite == "tool_calls":
            return _suite(suite, self.tc_ok if ad else 100)
        return _suite(suite, 90)


def _gate(folder: Path, scenario: str, **kw: Any) -> tuple[dict, Fakes]:
    fakes = Fakes(scenario, **kw)
    result = asyncio.run(run_gate(
        folder, corpus=object(), base_model=BASE, settings=SimpleNamespace(),
        engine_factory=fakes.factory, evaluate=fakes.evaluate,
        evaluate_personal=fakes.evaluate_personal, repeats=3, clock=lambda: 1_700_000_900.0))
    return result, fakes


def _pm(models_dir: Path, engine: Any = None, *, base: str = BASE, active: str = "",
        comparison: Any = None) -> tuple[PersonalModel, list[dict], dict]:
    writes: list[dict] = []
    env = {ACTIVE_KEY: active} if active else {}
    pm = PersonalModel(models_dir, engine if engine is not None else FakeEngine(base),
                       base_model=lambda: base, write_settings=writes.append, environ=env,
                       comparison=comparison)
    return pm, writes, env


# ---- gate + registry -------------------------------------------------------


@pytest.mark.parametrize("scenario,verdict", [
    ("worse", "adapter_worse"), ("not_enough", "not_enough_difference"),
    ("safety", "blocked_safety"), ("regression", "blocked_regression"),
])
def test_an_adapter_that_does_not_win_is_never_usable(tmp_path, scenario, verdict) -> None:
    folder = _make_adapter(tmp_path, 1)
    result, _ = _gate(folder, scenario)
    assert result["verdict"] == verdict
    [entry] = list_adapters(tmp_path, BASE)
    assert entry["verdict"] == verdict and entry["usable"] is False and entry["reason"]

    engine = FakeEngine()
    pm, writes, env = _pm(tmp_path, engine)
    with pytest.raises(PermissionError):
        asyncio.run(pm.use_adapter(1))
    assert writes == [] and ACTIVE_KEY not in env and engine.swaps == []
    # And a setting pointing at it (hand-edited) is cleared, not applied.
    env[ACTIVE_KEY] = str(folder)
    out = asyncio.run(pm.apply_active())
    assert out["adapter"] is None and engine.swaps == [] and writes == [{ACTIVE_KEY: ""}]


def test_a_winning_adapter_can_be_used_and_use_base_drops_it(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1)
    result, _ = _gate(folder, "wins")
    assert result["verdict"] == "adapter_wins"
    engine = FakeEngine()
    pm, writes, env = _pm(tmp_path, engine)
    status = asyncio.run(pm.use_adapter(1))
    assert writes == [{ACTIVE_KEY: str(folder)}] and env[ACTIVE_KEY] == str(folder)
    assert engine.swaps == [(BASE, str(folder))] and status["active"] == 1
    assert pm.adapter_active is True

    status = asyncio.run(pm.use_base())
    assert writes[-1] == {ACTIVE_KEY: ""} and ACTIVE_KEY not in env
    assert engine.swaps[-1] == (BASE, None)          # the adapter is dropped
    assert engine.adapter_path is None and pm.adapter_active is False  # next turn: base
    assert status["active"] is None
    asyncio.run(pm.use_base())  # always allowed, idempotent: no reload
    assert len(engine.swaps) == 2


def test_changing_the_base_model_marks_adapters_stale(tmp_path) -> None:
    win = {"verdict": "adapter_wins", "numbers": {"card": {"lines": ["x"]}}}
    old = _make_adapter(tmp_path, 1, base="old/model", gate=win)
    _make_adapter(tmp_path, 2, base=BASE, gate=win)
    by_n = {e["n"]: e for e in list_adapters(tmp_path, BASE)}
    assert by_n[1]["stale"] is True and by_n[1]["usable"] is False
    assert "old/model" in by_n[1]["reason"]
    assert by_n[2]["stale"] is False and by_n[2]["usable"] is True

    # The active one goes stale after a base change: apply_active drops it.
    engine = FakeEngine(BASE, adapter_path=str(old))
    pm, writes, env = _pm(tmp_path, engine, active=str(old))
    out = asyncio.run(pm.apply_active())
    assert out["adapter"] is None and engine.swaps == [(BASE, None)]
    assert writes == [{ACTIVE_KEY: ""}] and ACTIVE_KEY not in env
    with pytest.raises(PermissionError, match="older base model"):
        asyncio.run(pm.use_adapter(1))


def test_unfinished_folders_are_not_listed(tmp_path) -> None:
    _make_adapter(tmp_path, 1, status="training")
    (tmp_path / "notes").mkdir()
    _make_adapter(tmp_path, 3, gate={"verdict": "adapter_wins", "numbers": {}}, weights=False)
    [entry] = list_adapters(tmp_path, BASE)
    assert entry["n"] == 3 and entry["usable"] is False  # no adapter file
    assert list_adapters(tmp_path / "missing", BASE) == []


def test_gate_json_holds_only_verdict_numbers_and_time(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1)
    _gate(folder, "safety")
    path = folder / "gate.json"
    data = json.loads(path.read_text())
    assert set(data) == {"verdict", "numbers", "gated_at"}
    text = path.read_text()
    assert SECRET not in text and "SECRET" not in text
    assert '"cases"' not in text and '"runs"' not in text and '"calls"' not in text
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert data["numbers"]["card"]["lines"][0].startswith("Your model vs the original, on 40")


def test_run_gate_runs_every_suite_unsaved_and_unloads(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1)
    _, fakes = _gate(folder, "wins")
    [engine] = fakes.engines                    # one engine, one model at a time
    assert engine.model_name == BASE and engine.swaps == [(BASE, str(folder))]
    assert engine.unloads == 1 and engine._loaded is False
    order = [(c["suite"], c["adapter"]) for c in fakes.calls]
    suites = ["personal", "tool_calls", "idea_judge", "safety"]
    assert order == [(s, None) for s in suites] + [(s, str(folder)) for s in suites]
    assert all(c["save"] is False and c["repeats"] == 3 and c["engine"] is engine for c in fakes.calls)


def test_run_gate_unloads_on_failure_and_writes_nothing(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1)
    with pytest.raises(RuntimeError, match="model crashed"):
        _gate(folder, "wins", fail_on="safety")
    assert not (folder / "gate.json").exists()
    # The facade reports it and the adapter stays unusable.
    fakes = Fakes("wins", fail_on="safety")

    async def gate(adapter_dir, **kw):
        return await run_gate(adapter_dir, engine_factory=fakes.factory, evaluate=fakes.evaluate,
                              evaluate_personal=fakes.evaluate_personal, **kw)

    pm = PersonalModel(tmp_path, FakeEngine(), base_model=lambda: BASE, write_settings=lambda u: None,
                       environ={}, gate=gate, corpus=object())
    out = asyncio.run(pm.gate(folder))
    assert out["verdict"] == "error" and "model crashed" in out["error"]
    assert fakes.engines[0].unloads == 1
    assert list_adapters(tmp_path, BASE)[0]["usable"] is False


def test_facade_gate_uses_the_adapters_own_base_model(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1, base="trained/on")
    seen: dict = {}

    async def gate(adapter_dir, **kw):
        seen.update(kw, adapter_dir=adapter_dir)
        return {"verdict": "adapter_wins", "numbers": {}}

    corpus = object()
    pm = PersonalModel(tmp_path, FakeEngine(), base_model=lambda: BASE, write_settings=lambda u: None,
                       environ={}, gate=gate, corpus=corpus)
    asyncio.run(pm.gate(folder))
    assert seen["base_model"] == "trained/on" and seen["corpus"] is corpus


# ---- card text -------------------------------------------------------------


def test_switch_offer_and_outcome_texts(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1)
    win, _ = _gate(folder, "wins")
    summary, lines = switch_offer(win)
    assert summary == "Use your personal model?"
    assert lines[0] == "Your model vs the original, on 40 of your past decisions it hasn't seen:"
    assert lines[1] == "offers you'd approve 60% vs 50%"
    assert "general tool skills: no change" in lines
    assert me.SWITCH_BUTTONS == ("Use my model", "Keep the original")

    folder2 = _make_adapter(tmp_path, 2)
    close, _ = _gate(folder2, "not_enough")
    summary, lines = switch_offer(close)
    assert summary is None and lines[0].startswith("Not enough difference to tell yet")
    summary, lines = switch_offer({"verdict": "blocked_safety", "numbers": {}})
    assert summary is None and "safety" in lines[0]
    summary, lines = switch_offer({"verdict": "error"})
    assert summary is None and "didn't finish" in lines[0]


# ---- engine checks ---------------------------------------------------------


def test_cloud_main_engine_never_activates(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1, gate={"verdict": "adapter_wins", "numbers": {}})
    cloud = CloudEngine()
    pm, writes, env = _pm(tmp_path, cloud)
    with pytest.raises(RuntimeError, match="works with the local engine"):
        asyncio.run(pm.use_adapter(1))
    assert writes == [] and cloud.swaps == []
    status = pm.status()
    assert status["engine_ok"] is False and "local engine" in status["reason"]
    assert status["adapters"][0]["usable"] is True  # the adapter is fine; the engine isn't
    env[ACTIVE_KEY] = str(folder)
    out = asyncio.run(pm.apply_active())
    assert out["applied"] is False and "local engine" in out["reason"] and cloud.swaps == []
    assert pm.adapter_active is False and pm.pick_source() == "base"


def test_local_engine_on_another_model_does_not_activate(tmp_path) -> None:
    _make_adapter(tmp_path, 1, gate={"verdict": "adapter_wins", "numbers": {}})
    engine = FakeEngine("other/model")
    # base_model() still says BASE (e.g. the trainer's base), engine runs another.
    pm, writes, _ = _pm(tmp_path, engine)
    with pytest.raises(RuntimeError, match="trained on base/model"):
        asyncio.run(pm.use_adapter(1))
    assert engine.swaps == [] and writes == []


def test_apply_active_at_startup_loads_the_saved_adapter(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1, gate={"verdict": "adapter_wins", "numbers": {}})
    engine = FakeEngine()
    pm, writes, _ = _pm(tmp_path, None, active=str(folder))
    out = asyncio.run(pm.apply_active(engine))
    assert out == {"adapter": str(folder), "applied": True, "reason": None}
    assert engine.swaps == [(BASE, str(folder))] and writes == []
    asyncio.run(pm.apply_active())  # already applied: no reload
    assert len(engine.swaps) == 1


def test_daily_switch_back_goes_to_base_with_a_notice(tmp_path) -> None:
    folder = _make_adapter(tmp_path, 1, gate={"verdict": "adapter_wins", "numbers": {}})

    class Cmp:
        back = False

        def should_switch_back(self):
            return self.back

        def notice_text(self):
            return "Your model, last 14 days: ... Switched back to the original model."

        def pick_source(self, active):
            return "base" if not active else "adapter"

    cmp = Cmp()
    engine = FakeEngine(BASE, adapter_path=str(folder))
    pm, writes, env = _pm(tmp_path, engine, active=str(folder), comparison=cmp)
    assert asyncio.run(pm.check_switch_back()) is None and engine.swaps == []
    cmp.back = True
    text = asyncio.run(pm.check_switch_back())
    assert "Switched back" in text and engine.swaps == [(BASE, None)] and ACTIVE_KEY not in env
    assert asyncio.run(pm.check_switch_back()) is None  # nothing active any more


def test_settings_key_is_writable() -> None:
    assert ACTIVE_KEY in user_settings.ALLOWED_KEYS


# ---- adapter on/off without reloading --------------------------------------


class LoRALinear:  # same class name as mlx_lm.tuner.lora.LoRALinear
    def __init__(self, scale: Any) -> None:
        self.scale = scale


class DoRALinear:
    def __init__(self) -> None:
        self.scale = 20.0


class _Tree:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _engine_with(model: Any, adapter: str = "/m/1") -> MLXEngine:
    eng = MLXEngine(BASE, adapter_path=adapter)
    eng._model, eng._tokenizer, eng._loaded = model, object(), True
    return eng


def test_using_base_toggles_scales_and_restores_exact_objects() -> None:
    s1, s2 = 20.0, 16.5
    a, b = LoRALinear(s1), LoRALinear(s2)
    tree = _Tree(layers=[_Tree(attn=_Tree(q=a)), {"mlp": b}], head=_Tree())
    eng = _engine_with(tree)
    assert eng.adapter_active and eng.adapter_toggleable()

    async def scenario() -> list:
        seen = []
        async with eng.using_base() as how:
            seen.append((how, a.scale, b.scale, eng.adapter_active))
        return seen

    [(how, sa, sb, active)] = asyncio.run(scenario())
    assert (how, sa, sb, active) == ("toggle", 0.0, 0.0, False)
    assert a.scale is s1 and b.scale is s2 and eng.adapter_active  # exact values back
    assert eng.loaded and eng.adapter_path == "/m/1"


def test_using_base_restores_even_when_the_turn_raises() -> None:
    a = LoRALinear(20.0)
    eng = _engine_with(_Tree(l=a))

    async def scenario() -> None:
        async with eng.using_base():
            raise ValueError("turn failed")

    with pytest.raises(ValueError):
        asyncio.run(scenario())
    assert a.scale == 20.0 and eng.adapter_active


def test_toggle_waits_for_the_generation_lock() -> None:
    a = LoRALinear(20.0)
    eng = _engine_with(_Tree(l=a))
    observed: list = []
    release = threading.Event()

    def generating() -> None:
        with eng.generation_lock:
            release.wait(2)
            observed.append(a.scale)  # still the adapter while this "generation" runs

    t = threading.Thread(target=generating)
    t.start()
    time.sleep(0.05)

    async def scenario() -> None:
        async def toggled() -> None:
            async with eng.using_base():
                observed.append(("inside", a.scale))
        task = asyncio.create_task(toggled())
        await asyncio.sleep(0.1)
        assert not task.done()
        release.set()
        await task

    asyncio.run(scenario())
    t.join()
    assert observed == [20.0, ("inside", 0.0)] and a.scale == 20.0


def test_using_base_without_an_adapter_is_a_no_op() -> None:
    eng = MLXEngine(BASE)

    async def scenario() -> str:
        async with eng.using_base() as how:
            return how

    assert asyncio.run(scenario()) == "none" and not eng.loaded


def test_dora_adapter_falls_back_to_reloading(monkeypatch) -> None:
    import sys

    loads: list = []
    tree_dora = _Tree(l=DoRALinear())

    def load(path, **kw):
        loads.append((path, kw))
        return (tree_dora if kw.get("adapter_path") else _Tree()), object()

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=load, generate=lambda *a, **k: ""))
    eng = MLXEngine(BASE, adapter_path="/m/1")
    asyncio.run(eng.load())
    assert not eng.adapter_toggleable()

    async def scenario() -> tuple:
        async with eng.using_base() as how:
            return how, eng.adapter_path

    how, inside = asyncio.run(scenario())
    assert how == "reload" and inside is None
    assert [kw for _, kw in loads] == [{"adapter_path": "/m/1"}, {}, {"adapter_path": "/m/1"}]
    assert eng.adapter_path == "/m/1" and eng.adapter_active


def test_real_mlx_lora_layer_scale_zero_is_the_base_output() -> None:
    mx = pytest.importorskip("mlx.core")
    nn = pytest.importorskip("mlx.nn")
    lora = pytest.importorskip("mlx_lm.tuner.lora")

    class Tiny(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.layers = [lora.LoRALinear.from_base(nn.Linear(4, 4), r=2, scale=7.5)]

    m = Tiny()
    layer = m.layers[0]
    layer.lora_b = mx.ones((2, 4))
    x = mx.ones((1, 4))
    with_adapter = layer(x)
    base = layer.linear(x)
    eng = _engine_with(m)
    on, off = mlx_mod.adapter_layers(m)
    assert on == [layer] and off == []

    async def scenario() -> Any:
        async with eng.using_base():
            return layer(x)

    inside = asyncio.run(scenario())
    assert mx.array_equal(inside, base).item()
    assert mx.array_equal(layer(x), with_adapter).item() and layer.scale == 7.5


# ---- routes ----------------------------------------------------------------


@pytest.fixture
def routes_client(tmp_path):
    win = {"verdict": "adapter_wins", "numbers": {"card": {"lines": ["Your model vs the original"]}}}
    _make_adapter(tmp_path, 1, gate=win)
    _make_adapter(tmp_path, 2, gate={"verdict": "adapter_worse", "numbers": {}})
    engine = FakeEngine()
    pm, writes, env = _pm(tmp_path, engine)
    app = FastAPI()
    app.include_router(pm_routes.router)
    app.state.personal_model = pm
    with TestClient(app) as c:
        yield c, engine, writes


def test_routes_list_use_and_use_base(routes_client) -> None:
    c, engine, writes = routes_client
    body = c.get("/api/personal_model").json()
    assert [a["n"] for a in body["adapters"]] == [2, 1]
    one = body["adapters"][1]
    assert one["usable"] and one["card_lines"] == ["Your model vs the original"]
    assert set(one) >= {"n", "created_at", "base_model", "rows", "verdict", "card_lines", "stale", "usable"}
    assert body["active"] is None and body["engine_ok"] is True

    r = c.post("/api/personal_model/use", json={"n": 2})
    assert r.status_code == 403 and engine.swaps == []
    assert c.post("/api/personal_model/use", json={"n": 9}).status_code == 404
    r = c.post("/api/personal_model/use", json={"n": 1})
    assert r.status_code == 200 and r.json()["active"] == 1 and engine.swaps[-1][1].endswith("/1")
    r = c.post("/api/personal_model/use_base")
    assert r.status_code == 200 and r.json()["active"] is None and engine.swaps[-1] == (BASE, None)


def test_routes_503_without_the_facade() -> None:
    app = FastAPI()
    app.include_router(pm_routes.router)
    with TestClient(app) as c:
        assert c.get("/api/personal_model").status_code == 503
        assert c.post("/api/personal_model/use_base").status_code == 503


def test_cloud_engine_route_says_why(tmp_path) -> None:
    _make_adapter(tmp_path, 1, gate={"verdict": "adapter_wins", "numbers": {}})
    pm, _, _ = _pm(tmp_path, CloudEngine())
    app = FastAPI()
    app.include_router(pm_routes.router)
    app.state.personal_model = pm
    with TestClient(app) as c:
        r = c.post("/api/personal_model/use", json={"n": 1})
    assert r.status_code == 409 and "local engine" in r.json()["detail"]


def test_corpus_opt_out_turns_the_personal_model_off(tmp_path, monkeypatch) -> None:
    from ai import training_corpus as tc
    from ai.training_corpus import TrainingCorpus

    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv(tc.ENABLED_KEY, raising=False)
    corpus = TrainingCorpus(tmp_path / "training.db", enabled=lambda: True)
    calls: list = []

    class Spy:
        async def use_base(self):
            calls.append("use_base")
            return {}

    app = FastAPI()
    app.include_router(training_routes.router)
    app.state.training_corpus = corpus
    app.state.personal_model = Spy()
    with TestClient(app) as c:
        assert c.put("/api/training/settings", json={"enabled": True}).status_code == 200
        assert calls == []
        assert c.put("/api/training/settings", json={"enabled": False}).status_code == 200
        assert calls == ["use_base"]
        app.state.personal_model = None  # without the facade the opt-out still works
        assert c.put("/api/training/settings", json={"enabled": False}).status_code == 200
    corpus.close()


# ---- the invariant: consent doesn't depend on the model --------------------


def test_approval_gate_is_awaited_with_the_adapter_active(tmp_path) -> None:
    """LIVING_SPEC §1 Personal model 1: with the adapter on (and off) a
    side-effecting call still goes through ``_request_approval``."""
    from ai.orchestrator import Orchestrator
    from tests.test_wave13_wiring import _fake_for_wrapper, _Loop

    folder = _make_adapter(tmp_path, 1, gate={"verdict": "adapter_wins", "numbers": {}})
    engine = FakeEngine(BASE, adapter_path=str(folder))
    pm, _, _ = _pm(tmp_path, engine, active=str(folder))
    assert pm.adapter_active
    ran: list = []
    for active in (True, False):
        engine.adapter_path = str(folder) if active else None
        asked: list[dict] = []
        with _Loop() as loop:
            fake = _fake_for_wrapper(loop, asked, engine=engine, personal_model=pm)
            stub = SimpleNamespace(name="move_file", forward=lambda *a, **k: ran.append(k) or "ok")
            Orchestrator._wrap_tool_for_approval(fake, stub).forward(src="a", dst="b")
        assert [a["tool_name"] for a in asked] == ["move_file"], active
    assert len(ran) == 2


def test_request_approval_waits_for_the_user_with_the_adapter_loaded(tmp_path) -> None:
    from tests.test_wave13_wiring import _answer_soon, _approval_orch

    async def scenario() -> tuple:
        orch = _approval_orch()
        orch.engine = FakeEngine(BASE, adapter_path="/m/1")
        orch._current_event_type = "file"
        _answer_soon(orch, False)
        approved, _ = await orch._request_approval("evt", "move_file", {"kwargs": {"src": "a"}}, "r")
        return orch, approved

    orch, approved = asyncio.run(scenario())
    assert approved is False
    assert any(getattr(m, "type", None) == "approval_request" or "approval_request" in str(m)
               for m in orch.sent)


def test_consent_code_never_reads_the_model_or_adapter() -> None:
    """The approval path is the same code whichever model is loaded: none
    of it looks at the adapter, the personal model or the engine's model."""
    from ai import always_allow, standing_rules
    from ai.orchestrator import Orchestrator

    sources = [inspect.getsource(Orchestrator._request_approval),
               inspect.getsource(Orchestrator._wrap_tool_for_approval),
               inspect.getsource(always_allow), inspect.getsource(standing_rules)]
    import re

    for src in sources:
        for word in ("adapter", "adapter_active", "personal_model", "model_eval", "using_base"):
            # whole identifiers ("train_personal_model", the #119 card's tool, is fine)
            assert not re.search(rf"\b{word}\b", src), word
