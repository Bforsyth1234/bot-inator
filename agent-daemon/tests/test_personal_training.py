"""Tests for the personal-model training offer and runner (#119).

Never runs real mlx training: the child is a tiny fake script that prints
mlx-lm-style progress lines and writes ``adapters.safetensors``.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from ai import personal_training as pt
from ai.training_corpus import TrainingCorpus
from routes import training as training_routes

ROOT = Path(__file__).resolve().parent.parent

FAKE_CHILD = textwrap.dedent('''
    import sys, time, pathlib
    adapter, iters, delay, code = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
    print("Loading pretrained model", flush=True)
    print("  iter   train_loss     tok/s     tokens", flush=True)
    for it in range(1, iters + 1):
        time.sleep(delay)
        if it % 5 == 0 or it == iters:
            arrow = "\\u25bc"
            print(f"  {it:>4}    {3.0 - it * 0.01:>5.3f} {arrow}      500     {it:.1f}k", flush=True)
    if code == 0:
        pathlib.Path(adapter, "adapters.safetensors").write_bytes(b"fake")
        pathlib.Path(adapter, "adapter_config.json").write_text("{}")
    else:
        print("Traceback: out of memory", flush=True)
    sys.exit(code)
''')


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeCorpus:
    def __init__(self, total: int = 0, negatives: int = 0) -> None:
        self.total, self.negatives = total, negatives

    def usable_counts(self) -> dict:
        return {"total": self.total, "negatives": self.negatives, "tainted": 0,
                "all": self.total, "heldout": 0}


class Box:
    """A mutable value the fake guards read."""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(pt.ENV_MIN_ROWS, raising=False)
    monkeypatch.delenv(pt.ENV_MIN_NEGATIVE, raising=False)


@pytest.fixture()
def fake_child(tmp_path) -> Path:
    path = tmp_path / "fake_lora.py"
    path.write_text(FAKE_CHILD)
    return path


def fake_command(script: Path, *, delay: float = 0.0, code: int = 0, iters: int | None = None, calls=None):
    def build(base_model, data_dir, adapter_dir, planned):
        if calls is not None:
            calls.append({"base_model": base_model, "data_dir": data_dir, "adapter_dir": adapter_dir,
                          "data": {n: (data_dir / f"{n}.jsonl").read_text() for n in ("train", "valid")}})
        return [sys.executable, str(script), str(adapter_dir), str(iters or planned), str(delay), str(code)]
    return build


def seeded_corpus(tmp_path, rows: int = 120) -> TrainingCorpus:
    """Real corpus with every kind of row; prompt carries a marker per row."""
    clock = FakeClock(1_800_000_000.0)
    c = TrainingCorpus(tmp_path / "training.db", enabled=lambda: True,
                       keep_mail_text=lambda: False, clock=clock)
    for i in range(rows):
        clock.now += 60
        kind = ("approved", "approved", "edited", "denied", "undone", "tainted", "timed_out")[i % 7]
        verdict = {"edited": "approved", "undone": "approved", "tainted": "approved"}.get(kind, kind)
        edited = {"kwargs": {"title": f"EDITED-{i}"}} if kind == "edited" else None
        c.record(tool_name="create_reminder", tool_args={"kwargs": {"title": f"orig-{i}"}},
                 verdict=verdict, prompt=f"ROW-{i}-{kind} remind me", edited_args=edited,
                 offer_id=f"o{i}", event_type="user_message", tainted=kind == "tainted")
        if kind == "undone":
            c.mark_undone(f"o{i}")
    # A mail-derived row stored stripped (mail toggle off): not a target.
    c.record(tool_name="draft_email", tool_args={"kwargs": {"body": "Thanks!"}}, verdict="approved",
             prompt="ROW-mail secret email text", offer_id="omail", event_type="mail_received",
             event_meta={"subject": "secret"})
    return c


def make_runner(corpus, tmp_path, script, *, power="ac", idle=999.0, delay=0.0, code=0,
                iters=None, calls=None, progress=None, **kw):
    state = kw.pop("state", None) or pt.TrainingState(tmp_path / "state.json")
    pw, idl = Box(power), Box(idle)
    runner = pt.TrainingRunner(
        corpus, state, models_dir=tmp_path / "models",
        command=fake_command(script, delay=delay, code=code, iters=iters, calls=calls),
        power=pw, idle=idl, poll_seconds=0.02,
        on_progress=(lambda s: progress.append(s)) if progress is not None else None, **kw)
    return runner, state, pw, idl


async def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); ps shows its state.
    out = os.popen(f"ps -o stat= -p {pid}").read().strip()
    return bool(out) and not out.startswith("Z")


# ---- threshold ---------------------------------------------------------------------------


def test_299_rows_not_due_300_with_50_negatives_due(tmp_path):
    state = pt.TrainingState(tmp_path / "s.json")
    assert not pt.offer_due(FakeCorpus(299, 50), state)
    assert pt.offer_due(FakeCorpus(300, 50), state)


def test_too_few_negatives_not_due(tmp_path):
    state = pt.TrainingState(tmp_path / "s.json")
    assert not pt.offer_due(FakeCorpus(300, 49), state)
    assert not pt.offer_due(FakeCorpus(5000, 0), state), "approvals alone never trigger the offer"


def test_env_overrides_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv(pt.ENV_MIN_ROWS, "10")
    monkeypatch.setenv(pt.ENV_MIN_NEGATIVE, "2")
    state = pt.TrainingState(None)
    assert pt.offer_due(FakeCorpus(10, 2), state)
    assert not pt.offer_due(FakeCorpus(9, 2), state)


def test_declined_not_due_until_corpus_doubles(tmp_path):
    clock = FakeClock()
    state = pt.TrainingState(tmp_path / "s.json", clock=clock)
    corpus = FakeCorpus(300, 50)
    assert pt.offer_due(corpus, state)
    state.record_offered(300)
    assert not pt.offer_due(corpus, state), "no second card while one is pending"
    state.record_decline(300)
    for total in (300, 301, 450, 599):
        clock.now += 30 * 86400
        corpus.total = total
        assert not pt.offer_due(corpus, state), total
    corpus.total = 600
    assert pt.offer_due(corpus, state)
    # The decline survives a restart.
    assert not pt.offer_due(FakeCorpus(599, 50), pt.TrainingState(tmp_path / "s.json", clock=clock))


def test_pending_offer_expires(tmp_path):
    clock = FakeClock()
    state = pt.TrainingState(None, clock=clock)
    state.record_offered(300)
    assert not pt.offer_due(FakeCorpus(300, 50), state)
    clock.now += pt.OFFER_PENDING_TTL + 1
    assert pt.offer_due(FakeCorpus(300, 50), state)


def test_not_due_while_running_accepted_or_after_training(tmp_path):
    state = pt.TrainingState(None)
    corpus = FakeCorpus(300, 50)
    assert not pt.offer_due(corpus, state, running=True)
    state.record_accept(300)
    assert not pt.offer_due(corpus, state)
    assert state.consume_accept() and not state.consume_accept()
    state.record_trained(300, "/x/1")
    assert not pt.offer_due(FakeCorpus(599, 80), state)
    assert pt.offer_due(FakeCorpus(600, 80), state)


def test_state_file_is_0600_and_atomic(tmp_path):
    path = tmp_path / "sub" / "s.json"
    state = pt.TrainingState(path)
    state.record_decline(321)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["declined_at_rows"] == 321
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")], "no temp file left"


def test_offer_card_text():
    tool, kwargs, reasoning, summary = pt.offer_card(FakeCorpus(300, 50), base_model="m")
    assert tool == pt.TRAIN_TOOL_NAME
    assert summary.startswith("Train your own model? About ") and \
        summary.endswith(" min, uses the GPU, stays on this Mac")
    assert kwargs == {"base_model": "m", "rows": 300, "minutes": kwargs["minutes"]} and kwargs["minutes"] >= 1
    assert "never decides what needs your approval" in reasoning
    _, _, _, s2 = pt.offer_card(FakeCorpus(300, 50), unloads_assistant=True)
    assert "assistant pauses" in s2


def test_facade_decline_and_accept(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    trainer = pt.PersonalTraining(corpus, state=pt.TrainingState(tmp_path / "s.json"),
                                  base_model=lambda: "base", models_dir=tmp_path / "models",
                                  command=fake_command(fake_child), power=lambda: "battery",
                                  idle=lambda: 999.0, poll_seconds=0.02)
    trainer.record_decline()
    assert trainer.state.get("declined_at_rows") == corpus.usable_counts()["total"]

    async def go():
        status = await trainer.on_card_decision(True)
        assert status["state"] == "waiting_for_idle" and status["reason"] == "on battery"
        assert not trainer.state.accepted, "the accept is used by this start"
        await trainer.cancel()
        return trainer.status()

    assert asyncio.run(go())["state"] == "cancelled"


# ---- dataset -------------------------------------------------------------------------------


def test_dataset_excludes_heldout_denied_undone_tainted(tmp_path):
    corpus = seeded_corpus(tmp_path, rows=140)
    info = pt.build_dataset(corpus, tmp_path / "data")
    lines = []
    for name in ("train", "valid"):
        path = tmp_path / "data" / f"{name}.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        lines += [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == info["train"] + info["valid"] and info["valid"] >= pt.BATCH_SIZE
    prompts = [ex["messages"][1]["content"] for ex in lines]
    text = "\n".join(prompts)
    for bad in ("-denied", "-undone", "-tainted", "-timed_out", "ROW-mail", "[not kept"):
        assert bad not in text, bad
    heldout = {r["prompt"] for r in corpus.heldout_rows()}
    assert heldout and not heldout & set(prompts), "held-out rows never reach train/valid"
    assert corpus.split_info()["split_id"] == info["split_id"]
    # Edited rows train on the final card; approved on the proposal.
    by_prompt = {ex["messages"][1]["content"]: ex["messages"][2]["content"] for ex in lines}
    edited = [p for p in by_prompt if "-edited" in p]
    approved = [p for p in by_prompt if "-approved" in p]
    assert edited and approved
    for p in edited:
        i = p.split("-")[1]
        assert f'"EDITED-{i}"' in by_prompt[p] and f"orig-{i}" not in by_prompt[p]
    call = by_prompt[approved[0]]
    assert call.startswith("<tool_call>\n") and call.endswith("\n</tool_call>")
    body = json.loads(call[len("<tool_call>\n"):-len("\n</tool_call>")])
    assert body["name"] == "create_reminder" and body["arguments"]["title"].startswith("orig-")


def test_row_target_rejects_negatives_directly():
    base = {"verdict": "approved", "prompt": "p", "card": {"tool": "t", "args": {}}}
    assert pt.row_target(base) is not None
    assert pt.row_target({**base, "verdict": "denied"}) is None
    assert pt.row_target({**base, "outcome": "undone", "negative": True}) is None
    assert pt.row_target({**base, "tainted": True}) is None
    assert pt.row_target({**base, "heldout": True}) is None
    assert pt.row_target({**base, "mail_stripped": True}) is None


def test_dataset_too_small_raises(tmp_path):
    corpus = seeded_corpus(tmp_path, rows=7)
    with pytest.raises(ValueError):
        pt.build_dataset(corpus, tmp_path / "data")


# ---- runner guards ----------------------------------------------------------------------------


@pytest.mark.parametrize("power,idle,reason", [
    ("battery", 999.0, "on battery"),
    ("ac", 10.0, "waiting until you're away"),
    ("ac", 179.0, "waiting until you're away"),
    (None, 999.0, "power source unknown"),
    ("ac", None, "can't tell if you're away"),
])
def test_on_battery_or_active_the_run_does_not_start(tmp_path, fake_child, power, idle, reason):
    corpus = seeded_corpus(tmp_path)
    calls: list = []
    runner, state, _, _ = make_runner(corpus, tmp_path, fake_child, power=power, idle=idle, calls=calls)

    async def go():
        await runner.start("base", require_accept=False)
        await asyncio.sleep(0.2)
        status = runner.status()
        await runner.cancel()
        return status

    status = asyncio.run(go())
    assert status["state"] == "waiting_for_idle" and status["reason"] == reason
    assert calls == [], "no child was launched"
    assert not any((tmp_path / "models").iterdir()), "nothing left after cancel"


def test_start_needs_accept_and_one_run_at_a_time(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    runner, state, _, _ = make_runner(corpus, tmp_path, fake_child, power="battery")

    async def go():
        with pytest.raises(PermissionError):
            await runner.start("base")
        state.record_accept(100)
        await runner.start("base")
        state.record_accept(100)
        with pytest.raises(RuntimeError):
            await runner.start("base")
        await runner.cancel()

    asyncio.run(go())


def test_full_run_writes_adapter_and_meta(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    calls: list = []
    progress: list = []
    finished: list = []
    hooks: list = []
    runner, state, _, _ = make_runner(corpus, tmp_path, fake_child, iters=20, calls=calls,
                                      progress=progress, on_finished=finished.append,
                                      before_start=lambda: hooks.append("unload"),
                                      after_end=lambda: hooks.append("reload"))

    async def go():
        await runner.start("mlx-community/base", require_accept=False)
        return await asyncio.wait_for(runner.wait(), 20)

    status = asyncio.run(go())
    assert status["state"] == "done" and status["progress"] == 1.0 and status["eta_s"] == 0
    adapter = Path(status["adapter_dir"])
    assert adapter == tmp_path / "models" / "1"
    assert (adapter / "adapters.safetensors").exists()
    assert not (adapter / "data").exists(), "training text is deleted after the run"
    meta = json.loads((adapter / "meta.json").read_text())
    assert meta["base_model"] == "mlx-community/base" and meta["status"] == "done"
    assert meta["split_id"] == corpus.split_info()["split_id"] and meta["train_rows"] > 0
    assert {"created_at", "finished_at", "valid_rows", "heldout_rows", "usable_rows"} <= set(meta)
    its = [p["iteration"] for p in progress if p["state"] == "running"]
    assert 5 in its and 20 in its and any(p.get("loss") for p in progress)
    assert state.get("last_trained_rows") == corpus.usable_counts()["total"]
    assert state.get("last_adapter") == str(adapter)
    assert finished and finished[0]["state"] == "done"
    assert hooks == ["unload", "reload"]
    assert "-denied" not in calls[0]["data"]["train"]

    # n increments.
    runner2, *_ = make_runner(corpus, tmp_path, fake_child, iters=5, state=state)

    async def again():
        await runner2.start("b", require_accept=False)
        return await asyncio.wait_for(runner2.wait(), 20)

    assert asyncio.run(again())["adapter_dir"] == str(tmp_path / "models" / "2")


def test_cancel_kills_child_and_leaves_no_partial_adapter(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    runner, *_ = make_runner(corpus, tmp_path, fake_child, iters=10_000, delay=0.01)

    async def go():
        await runner.start("base", require_accept=False)
        assert await wait_for(lambda: runner.status()["iteration"] >= 5)
        pid = runner._proc.pid
        adapter = Path(runner.status()["adapter_dir"])
        assert adapter.exists()
        status = await runner.cancel()
        return pid, adapter, status

    pid, adapter, status = asyncio.run(go())
    assert status["state"] == "cancelled" and status["adapter_dir"] is None
    assert not pid_alive(pid)
    assert not adapter.exists()
    assert list((tmp_path / "models").iterdir()) == []


def test_user_returns_pauses_child_then_resumes(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    runner, _, power, idle = make_runner(corpus, tmp_path, fake_child, iters=300, delay=0.01)

    async def go():
        await runner.start("base", require_accept=False)
        assert await wait_for(lambda: runner.status()["state"] == "running")
        pid = runner._proc.pid
        idle.value = 5.0  # the user is back
        assert await wait_for(lambda: runner.status()["state"] == "paused")
        assert runner.status()["reason"] == "waiting until you're away"
        assert os.popen(f"ps -o stat= -p {pid}").read().strip().startswith("T"), "child is stopped"
        it = runner.status()["iteration"]
        await asyncio.sleep(0.3)
        assert runner.status()["iteration"] == it, "no progress while paused"
        idle.value = 100.0  # away, but under 180 s: stays paused
        await asyncio.sleep(0.2)
        assert runner.status()["state"] == "paused"
        idle.value = 999.0
        assert await wait_for(lambda: runner.status()["state"] in ("running", "done"))
        power.value = "battery"  # unplugged mid-run also pauses
        assert await wait_for(lambda: runner.status()["state"] == "paused")
        assert runner.status()["reason"] == "on battery"
        power.value = "ac"
        return await asyncio.wait_for(runner.wait(), 30)

    assert asyncio.run(go())["state"] == "done"


def test_failed_child_reports_error_and_cleans_up(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    runner, state, _, _ = make_runner(corpus, tmp_path, fake_child, iters=5, code=3)

    async def go():
        await runner.start("base", require_accept=False)
        return await asyncio.wait_for(runner.wait(), 20)

    status = asyncio.run(go())
    assert status["state"] == "failed" and "code 3" in status["error"] and "out of memory" in status["error"]
    assert list((tmp_path / "models").iterdir()) == []
    assert state.get("last_trained_rows") is None


def test_default_command_shape(tmp_path):
    argv = pt.default_command("m", tmp_path / "d", tmp_path / "a", 120)
    assert argv[1:5] == ["-m", "mlx_lm", "lora", "--train"]
    assert "--mask-prompt" in argv and argv[argv.index("--iters") + 1] == "120"
    assert argv[argv.index("--adapter-path") + 1] == str(tmp_path / "a")


def test_parsers():
    assert pt.parse_pmset("Now drawing from 'AC Power'\n -InternalBattery-0") == "ac"
    assert pt.parse_pmset("Now drawing from 'Battery Power'") == "battery"
    assert pt.parse_pmset("garbage") is None
    assert pt.parse_progress("    10    2.345 ▼      512     1.2k") == {"iteration": 10, "loss": 2.345}
    assert pt.parse_progress("\x1b[2m  15\x1b[0m    \x1b[1m2.100 ▲\x1b[0m    9    1k")["iteration"] == 15
    assert pt.parse_progress("Iter 30: Train loss 1.500, Learning Rate 1e-5") == {"iteration": 30, "loss": 1.5}
    assert pt.parse_progress("    40    val 1.900    0.52s") == {"iteration": 40, "val_loss": 1.9}
    assert pt.parse_progress("Loading pretrained model") is None


# ---- routes ------------------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, fake_child):
    corpus = seeded_corpus(tmp_path)
    trainer = pt.PersonalTraining(
        corpus, state=pt.TrainingState(tmp_path / "s.json"), base_model=lambda: "base",
        models_dir=tmp_path / "models", command=fake_command(fake_child, iters=100_000, delay=0.005),
        power=lambda: "ac", idle=lambda: 999.0, poll_seconds=0.02)
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(training_routes.router)
    app.state.training_corpus = corpus
    app.state.personal_training = trainer
    with TestClient(app) as c:
        yield c, trainer


def test_training_routes_need_auth(client):
    c, _ = client
    assert c.get("/api/training/status").status_code == 401
    assert c.post("/api/training/start").status_code == 401
    assert c.post("/api/training/cancel").status_code == 401


def test_start_refused_without_accept_then_runs_and_cancels(client, tmp_path):
    c, trainer = client
    h = auth.auth_header()
    body = c.get("/api/training/status", headers=h).json()
    assert body["state"] == "idle" and body["accepted"] is False and body["min_rows"] == 300
    assert c.post("/api/training/start", headers=h).status_code == 403
    assert not (tmp_path / "models").exists()
    trainer.record_accept()
    r = c.post("/api/training/start", json={}, headers=h)
    assert r.status_code == 200 and r.json()["state"] in ("waiting_for_idle", "running")
    trainer.record_accept()
    assert c.post("/api/training/start", headers=h).status_code == 409
    end = time.monotonic() + 10
    while c.get("/api/training/status", headers=h).json()["iteration"] < 5 and time.monotonic() < end:
        time.sleep(0.05)
    assert c.get("/api/training/status", headers=h).json()["state"] == "running"
    r = c.post("/api/training/cancel", headers=h)
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert list((tmp_path / "models").iterdir()) == []


def test_routes_503_without_trainer():
    app = FastAPI()
    app.include_router(training_routes.router)
    with TestClient(app) as c:
        assert c.get("/api/training/status", headers=auth.auth_header()).status_code == 503


# ---- seed script ----------------------------------------------------------------------------------


def test_seed_script_rows_pass_the_threshold(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import seed_training_corpus as seed
    finally:
        sys.path.pop(0)
    counts = seed.seed(tmp_path / "seed.db", 340)
    assert counts["total"] >= 300 and counts["negatives"] >= 50 and counts["tainted"] > 0
    corpus = TrainingCorpus(tmp_path / "seed.db", enabled=lambda: True)
    assert pt.offer_due(corpus, pt.TrainingState(None))
    info = pt.build_dataset(corpus, tmp_path / "data")
    assert info["train"] > 100
    with pytest.raises(SystemExit):
        seed.main([])  # no --path and no --real
    with pytest.raises(SystemExit):
        seed.main(["--path", str(Path.home() / ".bot-inator" / "training.db")])
