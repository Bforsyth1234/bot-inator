"""#104: per-tool trust, a reviewed generated tool may run outside the sandbox.

Covers the store (hash-tied, 0600, dropped on change), the loader
(``Orchestrator._load_tool``: untrusted -> sandboxed proxy, trusted ->
in-process), the code-review checkbox, the approval card flag, the routes
(token, sha check) and that no tool / rule / watch path can write trust.
"""
from __future__ import annotations

import ast
import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from ai import tool_trust as tt
from ai.orchestrator import Orchestrator
from routes import tool_detail
from schemas.ws_messages import (
    ApprovalRequestPayload,
    CodeApprovalRequest,
    CodeApprovalResponsePayload,
)
from tests.test_wave13_wiring import _approval_orch
from tests.test_wave15_wiring import _cards
from tools import AVAILABLE_TOOLS
from tools import tool_sandbox

DAEMON = Path(__file__).resolve().parents[1]

_SRC = '''from smolagents import tool


@tool
def close_app(app_name: str) -> dict:
    """Quit an app.

    Args:
        app_name: The app to quit.
    """
    return {"quit": app_name}
'''
_NET_SRC = _SRC.replace("    return {", "    import urllib.request\n    return {")


# ---- store ---------------------------------------------------------------------


def test_trust_is_tied_to_the_source_hash_and_saved_0600(tmp_path: Path) -> None:
    src = tmp_path / "close_app.py"
    src.write_text(_SRC)
    path = tmp_path / "state" / "tool_trust.json"
    store = tt.ToolTrust(path)
    assert not store.check("close_app", src)
    entry = store.trust("close_app", src)
    assert entry["sha256"] == tt.source_sha256(src)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A fresh store reads the same file.
    assert tt.ToolTrust(path).check("close_app", src)
    assert tt.ToolTrust(path).is_trusted("close_app", src)


def test_a_changed_source_drops_trust(tmp_path: Path) -> None:
    src = tmp_path / "close_app.py"
    src.write_text(_SRC)
    store = tt.ToolTrust(tmp_path / "tool_trust.json")
    store.trust("close_app", src)
    src.write_text(_SRC + "\n# repaired\n")
    assert not store.is_trusted("close_app", src)
    assert not store.check("close_app", src)
    assert store.entry("close_app") is None  # dropped, not just ignored
    # Restoring the old bytes does not bring trust back silently.
    src.write_text(_SRC)
    assert not store.check("close_app", src)


def test_trust_needs_the_hash_the_user_saw(tmp_path: Path) -> None:
    src = tmp_path / "close_app.py"
    src.write_text(_SRC)
    store = tt.ToolTrust(None)
    with pytest.raises(tt.TrustError):
        store.trust("close_app", src, expected_sha256="0" * 64)
    with pytest.raises(tt.TrustError):
        store.trust("missing", tmp_path / "missing.py")
    assert store.names() == []
    store.trust("close_app", src, expected_sha256=tt.source_sha256(src))
    assert store.revoke("close_app") and not store.revoke("close_app")


def test_code_review_checkbox_trusts_only_the_exact_installed_source(tmp_path: Path) -> None:
    src = tmp_path / "close_app.py"
    store = tt.ToolTrust(None)
    store.offer_from_review("close_app", _SRC)
    src.write_text(_SRC + "# not what was approved\n")
    assert not store.check("close_app", src)
    assert store.entry("close_app") is None
    store.offer_from_review("close_app", _SRC)
    src.write_text(_SRC)
    assert store.check("close_app", src)
    assert store.entry("close_app")["via"] == tt.VIA_CODE_REVIEW


def test_unreadable_file_is_empty_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "tool_trust.json"
    path.write_text("{not json")
    assert tt.ToolTrust(path).names() == []


# ---- loader ----------------------------------------------------------------------


@pytest.fixture
def loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "close_app.py"
    src.write_text(_SRC)
    calls: list[str] = []
    monkeypatch.setattr(tool_sandbox, "sandbox_enabled", lambda: True)

    def sandboxed(name: str, path: Path, exclude: Any = frozenset()) -> Any:
        calls.append("sandboxed")
        return SimpleNamespace(name=name, network_allowed=False)

    def in_process(name: str, path: Path, exclude: Any = frozenset()) -> Any:
        calls.append("in_process")
        return SimpleNamespace(name=name)

    monkeypatch.setattr(tool_sandbox, "load_sandboxed_tool", sandboxed)
    orch = Orchestrator.__new__(Orchestrator)
    orch.tool_trust = tt.ToolTrust(tmp_path / "tool_trust.json")
    monkeypatch.setattr(orch, "_import_tool_module", in_process)
    return SimpleNamespace(orch=orch, src=src, calls=calls)


def test_untrusted_tool_loads_sandboxed(loader) -> None:
    loader.orch._load_tool("close_app", loader.src)
    assert loader.calls == ["sandboxed"]
    assert "close_app" not in loader.orch._unsandboxed_tools


def test_trusted_tool_loads_in_process_then_sandboxed_after_a_change(loader) -> None:
    loader.orch.tool_trust.trust("close_app", loader.src)
    loader.orch._load_tool("close_app", loader.src)
    assert loader.calls == ["in_process"]
    assert "close_app" in loader.orch._unsandboxed_tools
    loader.src.write_text(_SRC + "\n# edited\n")
    loader.orch._load_tool("close_app", loader.src)
    assert loader.calls == ["in_process", "sandboxed"]
    assert "close_app" not in loader.orch._unsandboxed_tools
    assert loader.orch.tool_trust.entry("close_app") is None


# ---- code review checkbox + approval card -------------------------------------------


def _code_review(outside: bool, edited: str | None = None) -> Orchestrator:
    async def scenario() -> Orchestrator:
        orch = _approval_orch()
        orch._pending_code = {}
        orch.code_approval_timeout = 2.0
        orch.tool_trust = tt.ToolTrust(None)

        async def answer() -> None:
            while not orch._pending_code:
                await asyncio.sleep(0.01)
            rid, fut = next(iter(orch._pending_code.items()))
            fut.set_result(CodeApprovalResponsePayload(
                request_id=rid, approved=True, edited_code=edited, outside_sandbox=outside))

        asyncio.get_running_loop().create_task(answer())
        await orch.request_code_approval(tool_name="close_app", description="d", code=_SRC)
        assert any(isinstance(m, CodeApprovalRequest) for m in orch.sent)
        return orch

    return asyncio.run(scenario())


def test_review_checkbox_off_by_default_records_nothing(tmp_path: Path) -> None:
    assert CodeApprovalResponsePayload(request_id="r", approved=True).outside_sandbox is False
    orch = _code_review(False)
    src = tmp_path / "close_app.py"
    src.write_text(_SRC)
    assert not orch.tool_trust.check("close_app", src)


def test_review_checkbox_on_trusts_the_installed_source(tmp_path: Path) -> None:
    orch = _code_review(True, edited=_SRC + "# user edit\n")
    src = tmp_path / "close_app.py"
    src.write_text(_SRC + "# user edit\n")  # the meta-tool writes edited_code
    assert orch.tool_trust.check("close_app", src)


def test_approval_card_says_outside_the_sandbox() -> None:
    base = dict(request_id="r", event_id="e", tool_name="t", reasoning="x")
    assert ApprovalRequestPayload(**base).outside_sandbox is False

    async def scenario(unsandboxed: set[str]) -> Orchestrator:
        orch = _approval_orch()
        orch.approval_timeout = 0.05
        orch._unsandboxed_tools = unsandboxed
        await orch._request_approval("evt", "close_app", {"kwargs": {}}, "r")
        return orch

    [card] = _cards(asyncio.run(scenario({"close_app"})))
    assert card.outside_sandbox is True
    [card] = _cards(asyncio.run(scenario(set())))
    assert card.outside_sandbox is False


# ---- routes ------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path):
    gen = tmp_path / "generated"
    (gen / "disabled").mkdir(parents=True)
    (gen / "close_app.py").write_text(_NET_SRC)
    (gen / "disabled" / "old_tool.py").write_text(_SRC.replace("close_app", "old_tool"))
    reloads: list[Path] = []

    def load_dynamic_tools(directory: Path) -> list[str]:
        reloads.append(directory)
        return ["close_app"]

    builtin = next(t for t in AVAILABLE_TOOLS if t.name == "read_clipboard")
    orch = SimpleNamespace(tools=[builtin], _dynamic_tool_names=set(),
                           load_dynamic_tools=load_dynamic_tools, _unsandboxed_tools=set())
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(tool_detail.router)
    app.state.orchestrator = orch
    app.state.generated_tools_dir = gen
    app.state.tool_trust = tt.ToolTrust(tmp_path / "tool_trust.json")
    client = TestClient(app)
    client.headers.update(auth.auth_header())
    return SimpleNamespace(gen=gen, client=client, app=app, reloads=reloads)


def test_detail_reports_trust_and_the_confirm_text(api) -> None:
    body = api.client.get("/api/tools/close_app").json()
    trust = body["trust"]
    assert trust["trusted"] is False and trust["can_trust"] is True
    assert trust["sha256"] == tt.source_sha256(api.gen / "close_app.py")
    assert any("~/.bot-inator" in line for line in trust["sandbox_blocks"])
    assert "privacy ledger" in trust["network_note"]
    assert api.client.get("/api/tools/read_clipboard").json()["trust"] is None


def test_trust_route_trusts_reloads_and_revokes(api) -> None:
    sha = api.client.get("/api/tools/close_app").json()["trust"]["sha256"]
    resp = api.client.post("/api/tools/close_app/trust", json={"trusted": True, "sha256": sha})
    assert resp.status_code == 200, resp.text
    assert resp.json()["trusted"] is True and api.reloads == [api.gen]
    assert api.client.get("/api/tools/close_app").json()["trust"]["trusted"] is True
    assert api.client.get("/api/tool-trust").json() == {"trusted": ["close_app"]}
    resp = api.client.post("/api/tools/close_app/trust", json={"trusted": False})
    assert resp.status_code == 200 and len(api.reloads) == 2
    assert api.client.get("/api/tool-trust").json() == {"trusted": []}


def test_trust_route_refuses_a_stale_or_missing_hash(api) -> None:
    assert api.client.post("/api/tools/close_app/trust", json={"trusted": True}).status_code == 422
    resp = api.client.post("/api/tools/close_app/trust", json={"trusted": True, "sha256": "0" * 64})
    assert resp.status_code == 409
    assert api.app.state.tool_trust.names() == []


def test_trust_route_refuses_builtins_disabled_and_unknown(api) -> None:
    body = {"trusted": True, "sha256": "x"}
    assert api.client.post("/api/tools/read_clipboard/trust", json=body).status_code == 409
    sha = tt.source_sha256(api.gen / "disabled" / "old_tool.py")
    assert api.client.post("/api/tools/old_tool/trust",
                           json={"trusted": True, "sha256": sha}).status_code == 409
    assert api.client.post("/api/tools/nope/trust", json=body).status_code == 404
    assert api.client.post("/api/tools/close_app/trust", json={"trusted": "yes"}).status_code == 422


def test_trust_routes_need_the_app_token(api) -> None:
    sha = tt.source_sha256(api.gen / "close_app.py")
    bare = TestClient(api.app)
    assert bare.post("/api/tools/close_app/trust",
                     json={"trusted": True, "sha256": sha}).status_code == 401
    assert bare.get("/api/tool-trust").status_code == 401
    assert api.app.state.tool_trust.names() == []


# ---- nothing but the app can set trust ------------------------------------------------


def _imports_tool_trust(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "ai.tool_trust" or (mod == "ai" and any(a.name == "tool_trust" for a in node.names)):
                return True
        elif isinstance(node, ast.Import) and any(a.name == "ai.tool_trust" for a in node.names):
            return True
    return False


def test_only_the_loader_and_the_routes_use_the_trust_store() -> None:
    users = sorted(
        str(p.relative_to(DAEMON)) for p in DAEMON.rglob("*.py")
        if ".venv" not in p.parts and "tests" not in p.parts and _imports_tool_trust(p)
    )
    # No tool (built-in or generated), standing rule (#78), watch (#88) or
    # chat command imports it: only the code-review answer + loader and the
    # token-checked routes.
    assert users == ["ai/orchestrator.py", "routes/tool_detail.py"]


def test_no_tool_input_or_rule_can_name_trust() -> None:
    for t in AVAILABLE_TOOLS:
        assert "trust" not in str(getattr(t, "name", "")).lower()
        assert "outside_sandbox" not in set(getattr(t, "inputs", {}) or {})


def test_the_sandbox_denies_writes_to_the_trust_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(tt.PATH_ENV, raising=False)
    real = tt.default_path()
    assert real == Path.home() / ".bot-inator" / "tool_trust.json"
    profile = tool_sandbox.build_profile(
        home=Path.home(), work=tmp_path, python="/usr/bin/python3", network=False)
    deny_lines = [ln for ln in profile.splitlines() if ln.startswith("(deny file-write")]
    assert any(str(real.parent) in ln for ln in deny_lines), profile
