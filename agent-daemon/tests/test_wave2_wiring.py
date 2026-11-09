"""Wave 2 runner wiring in main.py: /health listener status, clear_memory,
and tests never opening the real ~/.bot-inator/memory.db."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import ai  # noqa: E402,F401  - must precede ``tools`` (circular import)
from ai.memory import Memory, _fallback_embed  # noqa: E402
from schemas.ws_messages import Command, CommandPayload  # noqa: E402


def _command(action: str) -> Command:
    return Command(seq=1, timestamp="2026-11-09T00:00:00.000Z",
                   payload=CommandPayload(action=action))


def test_tests_use_a_temp_memory_db() -> None:
    db = os.environ.get("AGENT_MEMORY_DB", "")
    assert db, "conftest must set AGENT_MEMORY_DB"
    assert Path(db).resolve() != (Path.home() / ".bot-inator" / "memory.db").resolve()


def test_health_includes_listener_status_when_running() -> None:
    import main

    fake = SimpleNamespace(status=lambda: {"alive": True, "restart_count": 2})
    old = getattr(main.app.state, "mac_listener", None)
    main.app.state.mac_listener = fake
    try:
        body = asyncio.run(main.health())
    finally:
        main.app.state.mac_listener = old
    assert body["status"] == "ok"
    assert body["listeners"]["nsworkspace"]["restart_count"] == 2


def test_health_without_listener() -> None:
    import main

    old = getattr(main.app.state, "mac_listener", None)
    main.app.state.mac_listener = None
    try:
        body = asyncio.run(main.health())
    finally:
        main.app.state.mac_listener = old
    assert "listeners" not in body


def test_clear_memory_command_deletes_rows(tmp_path) -> None:
    import main

    mem = Memory(db_path=tmp_path / "m.db", embedder=_fallback_embed)
    mem.save_memory("likes tea", {"kind": "preference"})
    mem.save_memory("opened Safari", {"kind": "event"})
    assert mem.count() == 2
    app = SimpleNamespace(state=SimpleNamespace(memory=mem))
    asyncio.run(main._handle_command(app, _command("clear_memory")))
    assert mem.count() == 0
    mem.close()
