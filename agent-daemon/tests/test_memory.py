"""Tests for ai.memory: retention cap, clear_memory, vec0 fallback (#20, #21)."""

from __future__ import annotations

import logging
import threading

import pytest

import ai.memory as memory_mod
from ai.memory import Memory, _fallback_embed


def _embed(text: str) -> list[float]:
    return _fallback_embed(text, 8)


@pytest.fixture(autouse=True)
def _reset_warn_flag(monkeypatch):
    monkeypatch.setattr(memory_mod, "_vec_warned", False)


@pytest.fixture
def mem(tmp_path):
    m = Memory(db_path=tmp_path / "memory.db", embedder=_embed, dim=8, max_rows=5)
    yield m
    m.close()


def _texts(m: Memory) -> list[str]:
    rows = m.connect().execute("SELECT text FROM memories ORDER BY id").fetchall()
    return [r[0] for r in rows]


def _no_vec(monkeypatch):
    monkeypatch.setattr(memory_mod, "_try_load_vec", lambda conn: "sqlite-vec not installed")


def test_default_cap_is_5000(tmp_path):
    m = Memory(db_path=tmp_path / "m.db", embedder=_embed, dim=8)
    assert m.max_rows == 5000
    m.close()


def test_eviction_drops_oldest_non_preference_first(mem):
    mem.save_memory("pref-0", {"kind": "preference"})
    for i in range(4):
        mem.save_memory(f"obs-{i}", {"kind": "observation"})
    assert mem.count() == 5
    mem.save_memory("obs-4", {"kind": "observation"})
    mem.save_memory("obs-5")  # no metadata at all
    assert mem.count() == 5
    assert _texts(mem) == ["pref-0", "obs-2", "obs-3", "obs-4", "obs-5"]


def test_preferences_survive_until_only_preferences_remain(mem):
    for i in range(5):
        mem.save_memory(f"pref-{i}", {"kind": "preference"})
    mem.save_memory("obs-new")
    # Only the new non-preference row is evictable; it is the one dropped.
    assert _texts(mem) == [f"pref-{i}" for i in range(5)]
    mem.save_memory("pref-5", {"kind": "preference"})
    assert _texts(mem) == [f"pref-{i}" for i in range(1, 6)]


def test_eviction_removes_vectors_too(mem):
    if not mem.connect() or not mem.vec_enabled:
        pytest.skip("sqlite-vec not available")
    for i in range(8):
        mem.save_memory(f"obs-{i}")
    n_vec = mem.connect().execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
    assert n_vec == 5


def test_clear_memory(mem):
    for i in range(3):
        mem.save_memory(f"m-{i}", {"kind": "preference"})
    mem.log_chat("e1", "user", "hi")
    assert mem.clear_memory() == 3
    assert mem.count() == 0
    assert mem.recall_memory("m-1") == []
    assert len(mem.get_chat_log()) == 1  # transcript is kept


def test_module_level_clear_memory(mem):
    memory_mod.set_default_memory(mem)
    try:
        memory_mod.save_memory("x")
        assert memory_mod.clear_memory() == 1
        assert mem.count() == 0
    finally:
        memory_mod.set_default_memory(None)


def test_fallback_recency_when_extension_unavailable(tmp_path, monkeypatch, caplog):
    _no_vec(monkeypatch)
    m = Memory(db_path=tmp_path / "m.db", embedder=_embed, dim=8)
    with caplog.at_level(logging.WARNING, logger="ai.memory"):
        for i in range(4):
            m.save_memory(f"m-{i}")
        assert m.vec_enabled is False
        assert m.recall_memory("anything", top_k=2) == ["m-3", "m-2"]
        assert m.recall_memory("other", top_k=1) == ["m-3"]
        m.close()
        m.recall_memory("after reconnect")
    warnings = [r for r in caplog.records if "Vector memory search disabled" in r.getMessage()]
    assert len(warnings) == 1
    assert not [r for r in caplog.records if "vec0 search unavailable" in r.getMessage()]
    m.close()


def test_pysqlite3_used_when_stdlib_cannot_load(tmp_path, monkeypatch):
    """Stdlib load fails -> pysqlite3 tried; absent here, so fallback, no crash."""
    calls = []

    def fake_load(conn):
        calls.append(type(conn).__module__)
        return "sqlite3 built without load_extension support"

    monkeypatch.setattr(memory_mod, "_try_load_vec", fake_load)
    import sys

    monkeypatch.setitem(sys.modules, "pysqlite3", None)
    monkeypatch.setitem(sys.modules, "pysqlite3.dbapi2", None)
    m = Memory(db_path=tmp_path / "m.db", embedder=_embed, dim=8)
    m.save_memory("a")
    assert m.vec_enabled is False
    assert m.recall_memory("a") == ["a"]
    assert len(calls) == 1
    m.close()


def test_vector_search_when_available(mem):
    if not (mem.connect() and mem.vec_enabled):
        pytest.skip("sqlite-vec not available")
    for t in ["alpha", "beta", "gamma"]:
        mem.save_memory(t)
    assert mem.recall_memory("beta", top_k=1) == ["beta"]


def test_backfill_rows_saved_without_vec(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    with monkeypatch.context() as mp:
        _no_vec(mp)
        m = Memory(db_path=path, embedder=_embed, dim=8)
        m.save_memory("old-row")
        m.close()
    m = Memory(db_path=path, embedder=_embed, dim=8)
    if not (m.connect() and m.vec_enabled):
        m.close()
        pytest.skip("sqlite-vec not available")
    n_vec = m.connect().execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
    assert n_vec == 1
    m.close()


_RACE_SCRIPT = r"""
import sys, threading
sys.path.insert(0, sys.argv[2])
from ai.memory import Memory, _fallback_embed
mem = Memory(sys.argv[1], embedder=lambda t: _fallback_embed(t, 8), dim=8)
for i in range(50):
    mem.save_memory(f"m{i}")
errs = []
stop = threading.Event()
def worker():
    while not stop.is_set():
        try:
            mem.search("m", 5)
            mem.store_embedding("w")
        except BaseException as exc:
            errs.append(repr(exc))
threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for _ in range(3000):
    mem.close()
stop.set()
for t in threads:
    t.join()
print(len(errs), errs[:2])
sys.exit(1 if errs else 0)
"""


def test_close_while_searching_from_threads(tmp_path):
    """close() racing worker-thread search/save must not error or segfault.

    Reproduces the #20 / wave-1 #40 crash: the lifespan's close() freed the
    connection while an asyncio.to_thread worker was inside search(). Runs
    in a subprocess so a SIGSEGV fails the test instead of killing pytest.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = str(Path(memory_mod.__file__).resolve().parent.parent)
    for _ in range(3):
        proc = subprocess.run(
            [sys.executable, "-c", _RACE_SCRIPT, str(tmp_path / "race.db"), root],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr[-500:])
