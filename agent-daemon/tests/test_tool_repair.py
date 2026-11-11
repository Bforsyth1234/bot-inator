"""Repairing an installed tool through the existing gates, and rollback (#90).

Drives ``meta_tool_generator.repair_tool`` / ``restore_previous_version``
against a real nested git repo in ``tmp_path`` (HOME and the global git
config point under ``tmp_path``). The drafter, the import probe and the
code card are faked; the critic and dry run are off.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import tool_health as th
from ai.idea_sources import IdeaState
from schemas.ws_messages import CodeApprovalResponsePayload
from tools import meta_tool_generator as mtg

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

V1 = '''"""Add a note."""
from __future__ import annotations
from smolagents import tool
import subprocess


@tool
def notes_tool(title: str) -> dict:
    """Add a note.

    Args:
        title: The note title.

    Returns:
        dict with status.
    """
    subprocess.run(["osascript", "-e", 'tell application "Notes" to make new nite'], check=True)
    return {"status": "ok"}
'''
V2 = V1.replace("new nite", "new note")
V2_NET = V2.replace("import subprocess\n", "import subprocess\nimport urllib.request\n")
SPEC_DESC = "Add a note to Notes."
SPEC_LOGIC = "Make a new note in Notes titled with the title argument."
FAILURE = {"error_class": "CalledProcessError",
           "message": "CalledProcessError: Command osascript returned non-zero exit status 1."}


@pytest.fixture
def git_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


class FakeOrch:
    def __init__(self, gen: Path) -> None:
        self.generated_tools_dir = gen
        self.tools: list[Any] = []
        self._dynamic_tool_names = {"notes_tool"}
        self.loads = 0
        self.tool_health = th.ToolHealthLog(None, state=IdeaState(None))
        self.event_bus = type("Bus", (), {"loop": None})()

    def load_dynamic_tools(self, directory: Any = None) -> list[str]:
        self.loads += 1
        return sorted(self._dynamic_tool_names)


@pytest.fixture
def env(tmp_path: Path, git_home: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    gen = tmp_path / "generated"
    gen.mkdir()
    (gen / "notes_tool.py").write_text(V1)
    ok, _ = mtg._git_commit(gen, "notes_tool.py", "Auto-generated tool: notes_tool")
    assert ok
    prov = mtg.provenance_path(gen, "notes_tool")
    prov.parent.mkdir()
    prov.write_text(json.dumps({"requested": {"description": SPEC_DESC, "expected_logic": SPEC_LOGIC}}))
    orch = FakeOrch(gen)
    orch.tool_health.record_failure("notes_tool", {**FAILURE, "repairable": True})
    monkeypatch.setattr(mtg, "_ORCHESTRATOR", orch)
    monkeypatch.setattr(mtg, "_ENGINE", object())
    monkeypatch.setattr(mtg, "_DRAFT_ENGINE", object())
    monkeypatch.setattr(mtg, "_GENERATED_DIR", gen)
    monkeypatch.setenv("AGENT_DRAFT_CRITIC", "0")
    monkeypatch.setenv("AGENT_DRAFT_DRY_RUN", "0")
    monkeypatch.setattr(mtg, "check_draft", lambda *a, **k: [])
    drafts: list[dict[str, Any]] = []
    state: dict[str, Any] = {"draft": V2, "drafts": drafts, "cards": [], "approve": True,
                             "gen": gen, "orch": orch}

    def fake_draft(engine: Any, tool_name: str, description: str, expected_logic: str, **kw: Any) -> str:
        drafts.append({"tool_name": tool_name, "description": description,
                       "expected_logic": expected_logic, **kw})
        return state["draft"]

    def fake_card(tool_name: str, description: str, source: str, **kw: Any) -> Any:
        # Nothing is written or committed while the card is open.
        state["cards"].append({"source": source, "on_disk": (gen / "notes_tool.py").read_text(),
                               "commits": _commits(gen), **kw})
        return CodeApprovalResponsePayload(request_id="r", approved=state["approve"])

    monkeypatch.setattr(mtg, "_draft_with_engine", fake_draft)
    monkeypatch.setattr(mtg, "_request_code_approval", fake_card)
    return state


def _commits(gen: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=gen, capture_output=True, text=True)
    return out.stdout.split("\n")[:-1] if out.stdout else []


def test_repair_draft_sees_the_failure_and_the_original_spec(env: dict[str, Any]) -> None:
    result = mtg.repair_tool("notes_tool")
    assert result["status"] == "ok", result
    draft = env["drafts"][0]
    assert draft["description"] == SPEC_DESC and draft["expected_logic"] == SPEC_LOGIC
    assert draft["installed_source"] == V1
    assert "CalledProcessError" in draft["failure_note"] and "exit status 1" in draft["failure_note"]


def test_repair_context_reaches_the_drafting_prompt() -> None:
    seen: list[str] = []

    class Engine:
        def generate_chat_sync(self, system: str, user: str, max_tokens: int = 0) -> str:
            seen.append(user)
            return V2

    mtg._draft_with_engine(Engine(), "notes_tool", SPEC_DESC, SPEC_LOGIC,
                           installed_source=V1, failure_note="KeyError: 'x'")
    assert "now FAILS when it runs" in seen[0] and V1 in seen[0] and "KeyError: 'x'" in seen[0]


def test_nothing_written_before_approval_then_commit_and_reload(env: dict[str, Any]) -> None:
    gen, orch = env["gen"], env["orch"]
    result = mtg.repair_tool("notes_tool")
    assert result["status"] == "ok"
    card = env["cards"][0]
    assert card["on_disk"] == V1 and len(card["commits"]) == 1
    assert card["previous_code"] == V1 and card["source"] == V2
    assert card["notice"].splitlines()[0].startswith("REPAIR of the installed tool notes_tool")
    assert (gen / "notes_tool.py").read_text().strip() == V2.strip()
    assert _commits(gen)[0] == "Repair tool: notes_tool (CalledProcessError)"
    assert orch.loads == 1
    assert orch.tool_health.health("notes_tool")["last_change"]["kind"] == "repaired"
    prov = json.loads(mtg.provenance_path(gen, "notes_tool").read_text())
    assert prov["repairs"][0]["error_class"] == "CalledProcessError"


def test_denied_repair_leaves_file_and_history_unchanged(env: dict[str, Any]) -> None:
    gen, orch = env["gen"], env["orch"]
    env["approve"] = False
    before = _commits(gen)
    result = mtg.repair_tool("notes_tool")
    assert result["status"] == "denied"
    assert (gen / "notes_tool.py").read_text() == V1
    assert _commits(gen) == before
    assert orch.loads == 0
    assert orch.tool_health.health("notes_tool")["last_change"] is None


def test_added_network_import_is_flagged_at_the_top_of_the_card(env: dict[str, Any]) -> None:
    env["draft"] = V2_NET
    mtg.repair_tool("notes_tool")
    first = env["cards"][0]["notice"].splitlines()[0]
    assert "ADDS network access" in first and "urllib.request" in first
    assert mtg.widening(V1, V2) == []
    assert any("network" in w for w in mtg.widening(V1, V2_NET))
    safari = V2.replace('"Notes"', '"Safari"')
    assert any("Safari" in w for w in mtg.widening(V1, safari))


def test_unrepairable_failure_is_refused_without_drafting(env: dict[str, Any]) -> None:
    result = mtg.repair_tool("notes_tool", {
        "error_class": "CalledProcessError",
        "message": "execution error: Not authorized to send Apple events to Notes. (-1743)",
    })
    assert result["status"] == "not_repairable" and result["category"] == "permission"
    assert env["drafts"] == [] and env["cards"] == []


def test_disabled_missing_and_identical_drafts_are_errors(env: dict[str, Any]) -> None:
    gen = env["gen"]
    assert mtg.repair_tool("other_tool")["status"] == "error"
    env["draft"] = V1
    out = mtg.repair_tool("notes_tool")
    assert out["status"] == "error" and "identical" in out["error"] and env["cards"] == []
    (gen / "disabled").mkdir()
    (gen / "notes_tool.py").rename(gen / "disabled" / "notes_tool.py")
    assert mtg.repair_tool("notes_tool")["error"] == "tool is disabled"


def test_repair_generated_tool_uses_the_latest_recorded_failure(env: dict[str, Any]) -> None:
    result = mtg.repair_generated_tool("notes_tool")
    assert result["status"] == "ok"
    assert "exit status 1" in env["drafts"][0]["failure_note"]


def test_restore_returns_the_previous_source_and_reloads(env: dict[str, Any]) -> None:
    gen, orch = env["gen"], env["orch"]
    assert mtg.restore_previous_version("notes_tool")["error"] == "no previous version"
    assert mtg.repair_tool("notes_tool")["status"] == "ok"
    loads = orch.loads
    result = mtg.restore_previous_version("notes_tool")
    assert result["status"] == "ok", result
    assert (gen / "notes_tool.py").read_text().strip() == V1.strip()
    assert orch.loads == loads + 1
    assert _commits(gen)[0].startswith("Restore previous version: notes_tool")
    assert orch.tool_health.health("notes_tool")["last_change"]["kind"] == "restored"
    # Restoring again toggles back to the repaired version.
    mtg.restore_previous_version("notes_tool")
    assert (gen / "notes_tool.py").read_text().strip() == V2.strip()


def test_restore_route(env: dict[str, Any]) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import tool_health as route

    gen, orch = env["gen"], env["orch"]
    mtg.repair_tool("notes_tool")
    app = FastAPI()
    app.include_router(route.router)
    app.state.tool_health = orch.tool_health
    app.state.generated_tools_dir = gen
    app.state.orchestrator = orch
    client = TestClient(app)
    assert client.get("/api/tools/notes_tool/health").json()["can_restore"] is True
    resp = client.post("/api/tools/notes_tool/restore-previous")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert (gen / "notes_tool.py").read_text().strip() == V1.strip()


# ---- the real card request carries previous_code ------------------------------------


def _loop_thread() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def test_previous_code_reaches_the_orchestrator_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _loop_thread()
    seen: dict[str, Any] = {}

    class New:
        code_approval_timeout = 5
        event_bus = type("Bus", (), {"loop": loop})()

        async def request_code_approval(self, *, tool_name: str, description: str, code: str,
                                        summary: Any = None, previous_code: Any = None) -> Any:
            seen.update(summary=summary, previous_code=previous_code)
            return CodeApprovalResponsePayload(request_id="r", approved=False)

    class Old(New):
        async def request_code_approval(self, *, tool_name: str, description: str, code: str,  # type: ignore[override]
                                        summary: Any = None) -> Any:
            seen.update(summary=summary, previous_code="absent")
            return CodeApprovalResponsePayload(request_id="r", approved=False)

    monkeypatch.setattr(mtg, "_plain_summary", lambda *a: "Plain.")
    try:
        monkeypatch.setattr(mtg, "_ORCHESTRATOR", New())
        mtg._request_code_approval("t", "d", V2, previous_code=V1, notice="REPAIR x")
        assert seen["previous_code"] == V1 and seen["summary"].startswith("REPAIR x")
        monkeypatch.setattr(mtg, "_ORCHESTRATOR", Old())
        mtg._request_code_approval("t", "d", V2, previous_code=V1, notice="REPAIR x")
        assert seen["previous_code"] == "absent"
    finally:
        loop.call_soon_threadsafe(loop.stop)
