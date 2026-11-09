"""#61: ``GET /api/tools/{name}`` detail and install-time provenance.

The router runs in a small FastAPI app behind the real
:class:`auth.AuthMiddleware`, with a fake orchestrator and a ``tmp_path``
generated dir; the real ``tools/generated`` is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from routes import tool_detail
from tools import AVAILABLE_TOOLS, READ_ONLY_TOOL_NAMES
from tools import meta_tool_generator as mtg

_GEN_SRC = '''from smolagents import tool


@tool
def tidy_downloads(folder: str, dry_run: bool = False) -> dict:
    """Move old files out of a folder into an archive.

    Runs over every file older than 30 days.

    Args:
        folder: Folder to tidy.
        dry_run: Only report what would move.
    """
    import urllib.request
    return {"folder": folder}
'''

_PROV = {
    "version": 1, "tool_name": "tidy_downloads", "installed_at": "2026-11-09T10:00:00+00:00",
    "turn_kind": "pattern", "event_id": "evt_1", "source": "repetition", "trigger": None,
    "idea": {"source": "repetition", "description": "Archive stale downloads."},
    "requested": {"description": "Archive stale downloads.", "expected_logic": "Move old files."},
    "user_edited": False,
}


def _builtin(name: str):
    return next(t for t in AVAILABLE_TOOLS if t.name == name)


@pytest.fixture
def env(tmp_path: Path):
    gen = tmp_path / "generated"
    gen.mkdir(exist_ok=True)
    orch = SimpleNamespace(
        tools=[_builtin("read_clipboard"), _builtin("open_url")],
        _dynamic_tool_names=set(),
    )
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(tool_detail.router)
    app.state.orchestrator = orch
    app.state.generated_tools_dir = gen
    client = TestClient(app)
    client.headers.update(auth.auth_header())
    return SimpleNamespace(gen=gen, orch=orch, client=client)


def _get(env, name: str):
    resp = env.client.get(f"/api/tools/{name}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_read_only_builtin(env):
    body = _get(env, "read_clipboard")
    assert body["kind"] == "builtin"
    assert body["state"] == "loaded"
    assert body["gate"] == "runs without asking"
    assert body["summary"] == "Read the current plain-text contents of the macOS clipboard."
    assert "read-only" in body["when_it_runs"]["text"]
    assert body["inputs"] == []
    assert body["sandbox"] is None
    assert body["source_path"].endswith("tools/read_clipboard.py")
    assert Path(body["source_path"]).is_file()


def test_gated_builtin(env):
    body = _get(env, "open_url")
    assert body["gate"] == "needs approval"
    assert "approval" in body["when_it_runs"]["text"]
    assert "url" in body["when_it_runs"]["text"]
    assert body["inputs"] == [{
        "name": "url", "type": "string", "optional": False,
        "description": "The absolute URL to open (http:// or https://).",
    }]
    assert body["source_path"].endswith("tools/open_url.py")


def test_builtin_hints_come_from_docstring(env):
    env.orch.tools.append(_builtin("remember_preference"))
    body = _get(env, "remember_preference")
    assert body["summary"] == "Save a short user preference into the agent's long-term memory."
    assert any(h.startswith("Use this when the user expresses") for h in body["when_it_runs"]["hints"])


def test_gate_follows_read_only_names(env, monkeypatch):
    # Non-vacuity: the gate is read from READ_ONLY_TOOL_NAMES, not hard-coded.
    monkeypatch.setattr(tool_detail, "_read_only_names", lambda: frozenset({"open_url"}))
    assert _get(env, "open_url")["gate"] == "runs without asking"
    assert _get(env, "read_clipboard")["gate"] == "needs approval"
    assert "read_clipboard" in READ_ONLY_TOOL_NAMES and "open_url" not in READ_ONLY_TOOL_NAMES


def test_generated_with_provenance(env):
    (env.gen / "tidy_downloads.py").write_text(_GEN_SRC)
    (env.gen / ".provenance").mkdir()
    (env.gen / ".provenance" / "tidy_downloads.json").write_text(json.dumps(_PROV))
    live = SimpleNamespace(
        name="tidy_downloads",
        description="Move old files out of a folder into an archive.\n\nMore.",
        inputs={"folder": {"type": "string", "description": "Folder to tidy."}},
        network_allowed=True,
        sandbox_note="runs under sandbox-exec; network allowed (imports urllib.request)",
    )
    env.orch.tools.append(live)
    env.orch._dynamic_tool_names.add("tidy_downloads")

    body = _get(env, "tidy_downloads")
    assert body["kind"] == "generated"
    assert body["state"] == "loaded"
    assert body["gate"] == "needs approval"
    assert body["summary"] == "Move old files out of a folder into an archive."
    when = body["when_it_runs"]
    assert when["provenance"] == _PROV
    assert "repeat a workflow" in when["text"]
    assert "Archive stale downloads." in when["text"]
    assert body["sandbox"] == {
        "enabled": True, "network_allowed": True,
        "note": "runs under sandbox-exec; network allowed (imports urllib.request)",
    }
    assert body["source_path"] == str((env.gen / "tidy_downloads.py").resolve())
    assert body["error_text"] is None


def test_generated_without_provenance_is_unknown(env):
    (env.gen / "tidy_downloads.py").write_text(_GEN_SRC)
    body = _get(env, "tidy_downloads")
    assert body["state"] == "not_loaded"
    assert body["when_it_runs"]["text"] == "unknown"
    assert body["when_it_runs"]["provenance"] is None
    # Parsed from source without importing it.
    assert body["summary"] == "Move old files out of a folder into an archive."
    assert body["inputs"] == [
        {"name": "folder", "type": "string", "description": "Folder to tidy.", "optional": False},
        {"name": "dry_run", "type": "boolean", "description": "Only report what would move.",
         "optional": True},
    ]
    assert body["sandbox"]["network_allowed"] is True


def test_disabled_tool(env):
    (env.gen / "disabled").mkdir()
    (env.gen / "disabled" / "tidy_downloads.py").write_text(_GEN_SRC.replace("import urllib.request\n", ""))
    body = _get(env, "tidy_downloads")
    assert body["state"] == "disabled"
    assert body["source_path"] == str((env.gen / "disabled" / "tidy_downloads.py").resolve())
    assert body["sandbox"]["network_allowed"] is False
    assert body["error_text"] is None


def test_quarantined_tool_has_error_text(env):
    q = env.gen / "quarantine"
    q.mkdir()
    (q / "tidy_downloads.py").write_text(_GEN_SRC)
    (q / "tidy_downloads.error.txt").write_text("SyntaxError: bad\nTraceback line 2\n")
    body = _get(env, "tidy_downloads")
    assert body["state"] == "quarantined"
    assert body["error_text"] == "SyntaxError: bad\nTraceback line 2\n"
    assert body["source_path"].endswith("quarantine/tidy_downloads.py")


def test_unknown_name_is_404(env):
    assert env.client.get("/api/tools/no_such_tool").status_code == 404
    assert env.client.get("/api/tools/..%2Fsecret").status_code == 404


def test_token_required(env):
    bare = TestClient(env.client.app)
    assert bare.get("/api/tools/read_clipboard").status_code == 401
    bad = bare.get("/api/tools/read_clipboard", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


# ---- provenance written on install ----------------------------------------


@pytest.fixture
def install_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gen = tmp_path / "generated"
    gen.mkdir(exist_ok=True)
    orch = SimpleNamespace(
        tools=[], _dynamic_tool_names=set(), mark_tool_ready_for_rerun=lambda n: None,
        load_dynamic_tools=lambda: [], _current_event_id="evt_pattern", _turn_kind="pattern",
    )
    monkeypatch.setattr(mtg, "_ORCHESTRATOR", orch)
    monkeypatch.setattr(mtg, "_ENGINE", object())
    monkeypatch.setattr(mtg, "_DRAFT_ENGINE", None)
    monkeypatch.setattr(mtg, "_GENERATED_DIR", gen)
    monkeypatch.setattr(mtg, "_CURRENT_IDEA", None)
    monkeypatch.setattr(mtg, "_draft_until_clean", lambda *a: (_GEN_SRC, 1))
    monkeypatch.setattr(
        mtg, "_request_code_approval",
        lambda *a: SimpleNamespace(approved=True, edited_code=None, user_note=None),
    )
    commits: list[tuple] = []
    monkeypatch.setattr(mtg, "_git_commit", lambda *a: (commits.append(a), (True, "ok"))[1])
    return SimpleNamespace(gen=gen, orch=orch, commits=commits)


def _install() -> dict:
    return mtg.generate_custom_tool(
        tool_name="tidy_downloads",
        description="Archive stale downloads.",
        expected_logic="Move files older than 30 days.",
    )


def test_install_writes_provenance_with_current_idea(install_env):
    mtg.set_current_idea({
        "source": "friction", "trigger": "clipboard_changed", "tool_name": "tidy_downloads",
        "description": "Idea text", "expected_logic": "Idea logic", "evidence": "x" * 5000,
        "event_details": {"secret": "not kept"},
    }, event_id="evt_pattern")
    assert _install()["status"] == "ok"

    prov = json.loads(mtg.provenance_path(install_env.gen, "tidy_downloads").read_text())
    assert prov["source"] == "friction"
    assert prov["trigger"] == "clipboard_changed"
    assert prov["turn_kind"] == "pattern"
    assert prov["event_id"] == "evt_pattern"
    assert prov["idea"]["description"] == "Idea text"
    assert len(prov["idea"]["evidence"]) == 1000
    assert "event_details" not in prov["idea"]
    assert prov["requested"] == {
        "description": "Archive stale downloads.", "expected_logic": "Move files older than 30 days.",
    }
    assert prov["installed_at"].startswith("20")
    # Metadata, not reviewed code: only the module is committed.
    assert [c[1] for c in install_env.commits] == ["tidy_downloads.py"]


def test_stale_idea_from_another_turn_is_not_attached(install_env):
    mtg.set_current_idea({"source": "friction"}, event_id="evt_older")
    install_env.orch._turn_kind = "chat"
    assert _install()["status"] == "ok"
    prov = json.loads(mtg.provenance_path(install_env.gen, "tidy_downloads").read_text())
    assert prov["idea"] is None
    assert prov["source"] == "chat"
    assert prov["requested"]["description"] == "Archive stale downloads."


def test_provenance_round_trips_through_detail_route(install_env, env, monkeypatch):
    monkeypatch.setattr(mtg, "_GENERATED_DIR", env.gen)
    mtg.set_current_idea({"source": "setup_scan", "trigger": "setup_scan"}, event_id="evt_pattern")
    assert _install()["status"] == "ok"
    body = _get(env, "tidy_downloads")
    assert body["when_it_runs"]["provenance"]["source"] == "setup_scan"
    assert "setup scan" in body["when_it_runs"]["text"]

