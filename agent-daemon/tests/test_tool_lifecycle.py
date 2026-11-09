"""Generated-tool lifecycle: disable/enable (#23), quarantine (#28), and
one ``generate_custom_tool`` call per agent turn (#38)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.test_integration import client  # noqa: F401  - pytest fixture

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent, EventBus
from smolagents import tool


def _tool_source(name: str) -> str:
    return (
        f'"""{name}."""\n'
        "from smolagents import tool\n\n"
        "@tool\n"
        f"def {name}() -> dict:\n"
        '    """Return a sentinel."""\n'
        '    return {"status": "ok"}\n'
    )


# ---- #23 disable / enable ---------------------------------------------


def test_disable_enable_round_trip(client, tmp_path, monkeypatch) -> None:  # noqa: F811
    from config import settings
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    orch = client.app.state.orchestrator
    (tmp_path / "lamp_tool.py").write_text(_tool_source("lamp_tool"))
    orch.load_dynamic_tools(tmp_path)
    assert "lamp_tool" in orch._dynamic_tool_names

    resp = client.post("/api/tools/lamp_tool/disable")
    assert resp.status_code == 200, resp.text
    assert resp.json()["unloaded"] is True
    assert not (tmp_path / "lamp_tool.py").exists()
    assert (tmp_path / "disabled" / "lamp_tool.py").exists()
    assert "lamp_tool" not in orch._dynamic_tool_names
    rows = {r["name"]: r for r in client.get("/api/tools").json()}
    assert rows["lamp_tool"]["disabled"] is True
    assert rows["lamp_tool"]["is_generated"] is True

    # The loader skips ``disabled/``.
    orch.load_dynamic_tools(tmp_path)
    assert "lamp_tool" not in orch._dynamic_tool_names

    resp = client.post("/api/tools/lamp_tool/enable")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "tool_name": "lamp_tool", "loaded": True}
    assert (tmp_path / "lamp_tool.py").exists()
    assert not (tmp_path / "disabled" / "lamp_tool.py").exists()
    assert "lamp_tool" in orch._dynamic_tool_names
    rows = {r["name"]: r for r in client.get("/api/tools").json()}
    assert "disabled" not in rows["lamp_tool"]

    # DELETE also clears a disabled copy.
    client.post("/api/tools/lamp_tool/disable")
    assert client.delete("/api/tools/lamp_tool").status_code == 200
    assert not (tmp_path / "disabled" / "lamp_tool.py").exists()


def test_disable_enable_refuse_builtins_and_bad_names(client, tmp_path, monkeypatch) -> None:  # noqa: F811
    from config import settings
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    for action in ("disable", "enable"):
        resp = client.post(f"/api/tools/show_notification/{action}")
        assert resp.status_code == 400
        assert "built-in" in resp.json()["detail"]
        assert client.post(f"/api/tools/BadName/{action}").status_code == 400
        assert client.post(f"/api/tools/no_such_tool/{action}").status_code == 404


# ---- #28 quarantine ---------------------------------------------------


def _orchestrator(tmp_path: Path, loop: asyncio.AbstractEventLoop) -> Orchestrator:
    bus = EventBus()
    bus.bind_loop(loop)
    return Orchestrator(
        engine=MLXEngine(), event_bus=bus, tools=[], generated_tools_dir=tmp_path,
    )


def test_broken_modules_are_quarantined_and_others_stay_loaded(tmp_path) -> None:
    (tmp_path / "good_tool.py").write_text(_tool_source("good_tool"))
    (tmp_path / "broken_tool.py").write_text("import not_a_real_module_xyz\n")
    (tmp_path / "empty_tool.py").write_text("X = 1\n")
    (tmp_path / "breaker_tool.py").write_text(_tool_source("breaker_tool"))

    def build(self: Orchestrator) -> None:
        names = {getattr(t, "name", "") for t in self.tools}
        if "breaker_tool" in names:
            raise ValueError("bad schema for breaker_tool")
        return None

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    thoughts: list[str] = []
    try:
        with patch.object(Orchestrator, "_build_agent", autospec=True, side_effect=build):
            orch = _orchestrator(tmp_path, loop)

            async def emit(_eid: str, stage: str, content: str) -> None:
                thoughts.append(f"{stage}|{content}")

            orch._emit_thought = emit  # type: ignore[method-assign]
            loaded = orch.load_dynamic_tools()
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert loaded == ["good_tool"]
    assert [t.name for t in orch.tools] == ["good_tool"]
    q = tmp_path / "quarantine"
    for name in ("broken_tool", "empty_tool", "breaker_tool"):
        assert not (tmp_path / f"{name}.py").exists()
        assert (q / f"{name}.py").exists()
        assert (q / f"{name}.error.txt").exists()
    assert (tmp_path / "good_tool.py").exists()
    first = lambda n: (q / f"{n}.error.txt").read_text().splitlines()[0]  # noqa: E731
    assert first("broken_tool").startswith("ModuleNotFoundError")
    assert "Traceback" in (q / "broken_tool.error.txt").read_text()
    assert "No smolagents Tool" in first("empty_tool")
    assert first("breaker_tool") == "ValueError: bad schema for breaker_tool"
    assert sorted(t.split("|")[1].split()[2] for t in thoughts) == [
        "breaker_tool", "broken_tool", "empty_tool",
    ]
    assert all(t.startswith("tool_result|") for t in thoughts)


def test_list_tools_shows_quarantined_with_error(client, tmp_path, monkeypatch) -> None:  # noqa: F811
    from config import settings
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    (tmp_path / "sick_tool.py").write_text("raise RuntimeError('boom')\n")
    client.app.state.orchestrator.load_dynamic_tools(tmp_path)
    rows = {r["name"]: r for r in client.get("/api/tools").json()}
    assert rows["sick_tool"]["quarantined"] is True
    assert rows["sick_tool"]["error"] == "RuntimeError: boom"
    # Enabling it again re-quarantines; DELETE clears it and its error file.
    resp = client.post("/api/tools/sick_tool/enable")
    assert resp.json()["status"] == "quarantined"
    assert client.delete("/api/tools/sick_tool").status_code == 200
    assert not any((tmp_path / "quarantine").iterdir())


def test_ok_install_of_quarantined_tool_reported_as_error(tmp_path) -> None:
    loop = asyncio.new_event_loop()
    try:
        with patch.object(Orchestrator, "_build_agent", return_value=None):
            orch = _orchestrator(tmp_path, loop)
            (tmp_path / "sick_tool.py").write_text("raise RuntimeError('boom')\n")
            orch.load_dynamic_tools()
    finally:
        loop.close()
    orch._pending_rerun_tool = "sick_tool"
    result = orch._flag_quarantined_install({"status": "ok", "tool_name": "sick_tool"})
    assert result["status"] == "error"
    assert "RuntimeError: boom" in result["error"]
    assert orch._pending_rerun_tool is None


# ---- #38 one generate_custom_tool call per turn ------------------------


@tool
def generate_custom_tool(tool_name: str) -> dict:
    """Stand-in for the meta-tool: always fails after approval.

    Args:
        tool_name: Name of the tool to generate.
    """
    return {"status": "error", "tool_name": tool_name, "error": "failed to load"}


def test_second_generate_call_in_a_turn_is_refused_without_approval(tmp_path) -> None:
    approvals: list[str] = []
    results: list[Any] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        with patch.object(Orchestrator, "_build_agent", return_value=None):
            orch = _orchestrator(tmp_path, loop)
        wrapped = orch._wrap_tool_for_approval(generate_custom_tool)

        async def request_approval(**kwargs: Any) -> tuple[bool, None]:
            approvals.append(kwargs["tool_name"])
            return True, None

        async def run_agent(event_id: str, prompt: str, max_steps: Any = None) -> str:
            # The model retries after the error, as in the live run.
            for name in ("recent_apps", "recent_apps_list", "recent_apps_v2"):
                results.append(
                    await asyncio.to_thread(wrapped.forward, tool_name=name)
                )
            return "done"

        orch._request_approval = request_approval  # type: ignore[method-assign]
        orch._run_agent = run_agent  # type: ignore[method-assign]
        for _ in range(2):  # two turns: the cap resets per turn
            await orch._handle_event(ContextEvent(
                event_type="pattern_detected",
                metadata={"suggested_tool_name": "recent_apps"},
            ))

    asyncio.run(scenario())
    assert approvals == ["generate_custom_tool", "generate_custom_tool"]
    statuses = [r.get("error") for r in results]
    assert statuses == [
        "failed to load",
        "generate_custom_tool_already_called",
        "generate_custom_tool_already_called",
    ] * 2
