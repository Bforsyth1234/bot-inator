"""Privacy ledger (#82): the store, the route, the hook helpers and the guard.

Every egress site is hooked (wave 15): :func:`test_every_hook_point_records`
requires all of them, so removing one fails (the item's non-vacuity guard).
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import egress_log
from ai.egress_log import EgressLog

DAEMON = Path(__file__).resolve().parents[1]
SECRET = "Dinner with Sam at 7 tomorrow"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A fresh default ledger in ``tmp_path`` (keep-text follows the env)."""
    monkeypatch.setenv(egress_log.DB_PATH_ENV, str(tmp_path / "egress.db"))
    monkeypatch.delenv(egress_log.KEEP_TEXT_KEY, raising=False)
    log = EgressLog()
    egress_log.set_default_log(log)
    yield log
    egress_log.set_default_log(None)
    log.close()


def _fake_openai(reply: str = "hello", *, fail: bool = False) -> Any:
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if fail:
            raise RuntimeError("boom")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                role="assistant", content=reply, tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.calls = calls
    return client


# ---- store -------------------------------------------------------------------


def test_record_and_read_metadata(ledger):
    assert ledger.record("inference", "api.groq.com", "main", "m1", 120, 40, "replaced",
                         tool=None, tokens_out=10, tokens_in=2)
    [row] = ledger.rows()
    assert row["kind"] == "inference" and row["host"] == "api.groq.com"
    assert (row["role"], row["model"], row["bytes_out"], row["bytes_in"]) == ("main", "m1", 120, 40)
    assert row["personal_text"] == "replaced" and row["text"] is None
    assert row["time"]


def test_file_is_private_and_env_overrides_path(tmp_path, monkeypatch):
    target = tmp_path / "sub" / "ledger.db"
    monkeypatch.setenv(egress_log.DB_PATH_ENV, str(target))
    log = EgressLog()
    log.record("open_url", "example.org")
    assert log.db_path == target
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    log.close()


def test_keep_text_off_stores_no_text_on_disk(ledger):
    ledger.record("inference", "api.groq.com", "main", "m", 10, 0, "sent", text=SECRET)
    ledger.close()
    assert SECRET.encode() not in ledger.db_path.read_bytes()
    assert ledger.rows()[0]["text"] is None


def test_keep_text_on_keeps_text_then_forget(ledger, monkeypatch):
    monkeypatch.setenv(egress_log.KEEP_TEXT_KEY, "1")
    ledger.record("inference", "api.groq.com", "main", "m", 10, 0, "sent", text=SECRET)
    assert ledger.rows()[0]["text"] == SECRET
    assert ledger.forget_text() == 1
    assert ledger.rows()[0]["text"] is None


def test_retention_text_retention_and_cap(tmp_path):
    log = EgressLog(tmp_path / "e.db", keep_text=True, max_rows=3, prune_every=1000)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=91)).timestamp()
    week = (now - timedelta(days=8)).timestamp()
    log.record("inference", "h", text="old", ts=old)
    log.record("inference", "h", text="week", ts=week)
    log.record("inference", "h", text="fresh")
    log.prune()
    texts = [r["text"] for r in log.rows()]
    assert texts == ["fresh", None]  # 91 days gone, 8-day-old text erased
    for i in range(5):
        log.record("open_url", f"h{i}", ts=now.timestamp() + i)
    log.prune()
    assert [r["host"] for r in log.rows()] == ["h4", "h3", "h2"]
    log.close()


def test_clear_empties_it(ledger):
    ledger.record("open_url", "a")
    ledger.record("open_url", "b")
    assert egress_log.clear() == 2
    assert ledger.count() == 0


def test_stats_today_and_nothing_left(ledger):
    s = ledger.stats()
    assert s["count"] == 0 and s["summary"] == egress_log.NOTHING_TODAY
    ledger.record("inference", "api.groq.com", "main", "m", 2048, 512, "sent")
    ledger.record("open_url", "example.org", bytes_out=20)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    ledger.record("inference", "api.groq.com", ts=yesterday, bytes_out=999)
    s = ledger.stats()
    assert (s["count"], s["bytes_out"], s["bytes_in"]) == (2, 2068, 512)
    assert s["by_kind"] == {"inference": 1, "open_url": 1}
    assert s["personal_text_sent"] == 1
    assert s["summary"].startswith("2 requests left this Mac today")


def test_ledger_failure_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "dir.db"
    blocker.mkdir()  # a directory where the db file should be
    log = EgressLog(blocker)
    assert log.record("open_url", "x") is False
    monkeypatch.setattr(EgressLog, "record", lambda *a, **k: 1 / 0)
    egress_log.set_default_log(log)
    try:
        assert egress_log.record("open_url", "x") is False
        assert egress_log.record_completion(role="main", model="m", api_base="https://a",
                                            request={}, response=None) is False
        assert egress_log.record_open_url("https://x.org") is False
        assert egress_log.record_tool_run("mcp_tool", "t", {"a": 1}) is False
        with egress_log.model_download("org/model", cache_dir=tmp_path):
            pass
    finally:
        egress_log.set_default_log(None)


# ---- helpers -------------------------------------------------------------------


def test_record_completion_sizes_and_flag(ledger, monkeypatch):
    from ai import privacy

    monkeypatch.setattr(privacy, "should_redact", lambda role: True)
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    response = _fake_openai("hello").chat.completions.create(**request)
    assert egress_log.record_completion(role="analysis", model="m",
                                        api_base="https://api.groq.com/openai/v1",
                                        request=request, response=response)
    [row] = ledger.rows()
    assert row["host"] == "api.groq.com" and row["role"] == "analysis"
    assert row["bytes_out"] == egress_log.payload_size(request) > 0
    assert row["bytes_in"] > 0
    assert (row["tokens_out"], row["tokens_in"]) == (11, 3)
    assert row["personal_text"] == "replaced"
    monkeypatch.setattr(privacy, "should_redact", lambda role: False)
    egress_log.record_completion(role="analysis", model="m", api_base="https://a", request=request)
    assert ledger.rows()[0]["personal_text"] == "sent"
    # A placeholder in the prompt means it was replaced, whatever the policy.
    egress_log.record_completion(role="analysis", model="m", api_base="https://a",
                                 request={"messages": [privacy.PLACEHOLDER]})
    assert ledger.rows()[0]["personal_text"] == "replaced"


def test_wrapped_client_records_each_call_and_failures(ledger):
    inner = _fake_openai()
    client = egress_log.wrap_openai_client(inner, role="main", api_base="https://api.groq.com/x")
    assert egress_log.wrap_openai_client(client, role="main") is client  # idempotent
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "a"}])
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "b"}])
    assert len(inner.calls) == 2 and ledger.count() == 2
    bad = egress_log.wrap_openai_client(_fake_openai(fail=True), role="main", api_base="https://h")
    with pytest.raises(RuntimeError):
        bad.chat.completions.create(model="m", messages=[])
    assert ledger.rows()[0]["note"] == "failed: RuntimeError"
    # Objects with no chat.completions are returned unchanged.
    plain = object()
    assert egress_log.wrap_openai_client(plain, role="main") is plain


def test_tool_run_and_open_url_rows(ledger):
    egress_log.record_tool_run("mcp_tool", "github__search", {"q": SECRET})
    egress_log.record_open_url("https://example.org/path?q=secret")
    url_row, mcp_row = ledger.rows()
    assert mcp_row["host"] is None and mcp_row["tool"] == "github__search"
    assert mcp_row["bytes_out"] > 0 and "network" in mcp_row["note"] and mcp_row["text"] is None
    assert url_row["host"] == "example.org" and url_row["text"] is None


def test_model_download_records_cache_growth(ledger, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    cache = tmp_path / "hub"
    blobs = cache / "models--org--model" / "blobs"
    with egress_log.model_download("org/model", cache_dir=cache):
        blobs.mkdir(parents=True)
        (blobs / "a").write_bytes(b"x" * 1000)
    with egress_log.model_download("org/model", cache_dir=cache):
        pass  # warm cache: nothing downloaded
    local = tmp_path / "local-model"
    local.mkdir()
    with egress_log.model_download(str(local), cache_dir=cache):
        pass
    [row] = ledger.rows()
    assert (row["kind"], row["host"], row["model"], row["bytes_in"]) == (
        "model_download", "huggingface.co", "org/model", 1000)


# ---- routes ------------------------------------------------------------------


@pytest.fixture
def api(ledger, tmp_path, monkeypatch):
    import user_settings
    from routes import egress as egress_routes

    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(user_settings, "ALLOWED_KEYS",
                        user_settings.ALLOWED_KEYS | {egress_log.KEEP_TEXT_KEY})
    monkeypatch.setattr(user_settings, "shell_exported", frozenset())
    app = FastAPI()
    app.include_router(egress_routes.router)
    return TestClient(app)


def test_route_list_stats_toggle_clear(api, ledger, monkeypatch):
    body = api.get("/api/egress").json()
    assert body["rows"] == [] and body["today"]["summary"] == egress_log.NOTHING_TODAY
    assert body["keep_text"] is False and body["limit_note"] == egress_log.LIMIT_NOTE
    r = api.put("/api/egress", json={"keep_text": True})
    assert r.status_code == 200 and r.json()["keep_text"] is True
    ledger.record("inference", "api.groq.com", "main", "m", 5, 5, "sent", text=SECRET)
    assert api.get("/api/egress?limit=1").json()["rows"][0]["text"] == SECRET
    api.put("/api/egress", json={"keep_text": False})
    assert ledger.rows()[0]["text"] is None  # switching off erases kept text
    assert api.get("/api/egress/stats").json()["count"] == 1
    assert api.get("/api/egress/stats?day=nope").status_code == 422
    assert api.get("/api/egress?since=nope").status_code == 422
    r = api.delete("/api/egress")
    assert r.json()["removed"] == 1 and r.json()["rows"] == []


def test_route_locked_by_shell(api, monkeypatch):
    import user_settings

    monkeypatch.setattr(user_settings, "shell_exported", frozenset({egress_log.KEEP_TEXT_KEY}))
    assert api.get("/api/egress").json()["locked_fields"] == ["keep_text"]
    assert api.put("/api/egress", json={"keep_text": True}).status_code == 409


def test_route_needs_auth_once_registered(ledger):
    import main
    from auth import auth_header

    bare = TestClient(main.app)
    authed = bare.get("/api/egress", headers=auth_header())
    assert authed.status_code == 200
    assert bare.get("/api/egress").status_code == 401


# ---- the guard: every outbound call site has a ledger hook ------------------------

# Modules whose import means the file can reach the network (or download).
NETWORK_IMPORTS = frozenset({
    "openai", "httpx", "requests", "urllib.request", "urllib3", "http.client",
    "socket", "aiohttp", "websocket", "websockets", "smtplib", "ftplib",
    "imaplib", "poplib", "xmlrpc", "ssl", "huggingface_hub", "mcp", "mlx_lm",
    "sentence_transformers", "transformers", "webbrowser",
})
NETWORK_NAMES = frozenset({"OpenAIServerModel", "OpenAIModel"})
# Not daemon code: tests, dev scripts, evals, generated tools (they run
# through tools/tool_sandbox.py, which is hooked).
SKIP_DIRS = frozenset({".venv", "tests", "scripts", "evals", "__pycache__", "generated"})

# file -> the function that must call ai.egress_log.
HOOK_POINTS: dict[str, str] = {
    "ai/groq_engine.py": "GroqEngine._chat_completion",
    "ai/orchestrator.py": "_make_groq_model_class",
    "ai/mlx_engine.py": "MLXEngine._load_locked",
    "ai/mcp_client.py": "McpManager.call_tool",
    "tools/tool_sandbox.py": "SandboxedTool.forward",
    "tools/open_url.py": "open_url",
    "ai/memory.py": "_load_st_model",
    "routes/providers.py": "_client",  # #97 Settings "List models" / "Test"
}
# file -> why a network import there sends nothing for the ledger.
EXEMPT: dict[str, str] = {}


def _network_import(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            for i in range(len(alias.name.split(".")), 0, -1):
                if ".".join(alias.name.split(".")[:i]) in NETWORK_IMPORTS:
                    return alias.name
    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
        parts = node.module.split(".")
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in NETWORK_IMPORTS:
                return node.module
        for alias in node.names:
            if f"{node.module}.{alias.name}" in NETWORK_IMPORTS or alias.name in NETWORK_NAMES:
                return f"{node.module}.{alias.name}"
    return None


def network_sites(root: Path) -> dict[str, str]:
    """``{relative path: first network import}`` for daemon sources under ``root``."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            hit = _network_import(node)
            if hit:
                found[rel.as_posix()] = hit
                break
    return found


def function_source(path: Path, qualname: str) -> Optional[str]:
    """Source of ``Class.method`` / ``function`` in ``path`` (AST, no import)."""
    text = path.read_text(encoding="utf-8")
    nodes: list[ast.AST] = [ast.parse(text)]
    for name in qualname.split("."):
        nxt = None
        for parent in nodes:
            for child in ast.walk(parent):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and child.name == name:
                    nxt = child
                    break
            if nxt:
                break
        if nxt is None:
            return None
        nodes = [nxt]
    return ast.get_source_segment(text, nodes[0])


def unhooked(root: Path, points: dict[str, str]) -> list[str]:
    """Hook points whose function does not mention ``egress_log``."""
    missing = []
    for rel, qual in points.items():
        src = function_source(root / rel, qual) if (root / rel).exists() else None
        if src is None or "egress_log" not in src:
            missing.append(f"{rel}:{qual}")
    return missing


def test_every_network_site_is_listed():
    sites = network_sites(DAEMON)
    unlisted = {f: m for f, m in sites.items() if f not in HOOK_POINTS and f not in EXEMPT}
    assert not unlisted, (
        f"network imports with no ledger hook point: {unlisted}. Hook the call "
        "site with ai.egress_log and add it to HOOK_POINTS (or EXEMPT with a reason)."
    )
    for rel, qual in HOOK_POINTS.items():
        assert function_source(DAEMON / rel, qual) is not None, f"{rel}:{qual} moved"


def test_guard_catches_a_new_site_and_a_removed_hook(tmp_path):
    """Non-vacuity for the two guard checks, on a synthetic tree."""
    (tmp_path / "ai").mkdir()
    (tmp_path / "ai" / "fetch.py").write_text("import httpx\n")
    (tmp_path / "ai" / "local.py").write_text("from urllib.parse import urlsplit\n")
    (tmp_path / "ai" / "agent.py").write_text("from smolagents.models import OpenAIServerModel\n")
    assert network_sites(tmp_path) == {
        "ai/agent.py": "smolagents.models.OpenAIServerModel", "ai/fetch.py": "httpx"}
    (tmp_path / "ai" / "hooked.py").write_text(
        "class A:\n    def go(self):\n        from ai import egress_log\n        egress_log.record('x')\n")
    (tmp_path / "ai" / "bare.py").write_text("class A:\n    def go(self):\n        return 1\n")
    points = {"ai/hooked.py": "A.go", "ai/bare.py": "A.go"}
    assert unhooked(tmp_path, points) == ["ai/bare.py:A.go"]


def test_every_hook_point_records():
    """Every egress site has its ledger hook (wired in wave 15)."""
    missing = unhooked(DAEMON, HOOK_POINTS)
    assert not missing, f"egress sites without a ledger hook: {missing}"


def _hooked(rel: str) -> bool:
    return not unhooked(DAEMON, {rel: HOOK_POINTS[rel]})


def test_groq_engine_one_row_per_call(ledger, monkeypatch):
    assert _hooked("ai/groq_engine.py")
    from ai import privacy
    from ai.groq_engine import GroqEngine

    monkeypatch.setattr(privacy, "should_redact", lambda role: True)
    engine = GroqEngine("gm", api_key="k", api_base="https://api.groq.com/openai/v1")
    engine.role = "drafting"
    fake = _fake_openai("ok")
    engine._client, engine._loaded = fake, True
    assert engine.generate_chat_sync("sys", "user text") == "ok"
    assert engine.generate_sync("p") == "ok"
    rows = ledger.rows()
    assert len(rows) == 2 == len(fake.calls)
    for row in rows:
        assert (row["kind"], row["host"], row["role"], row["model"]) == (
            "inference", "api.groq.com", "drafting", "gm")
        assert row["bytes_out"] > 0 and row["bytes_in"] > 0
        assert row["personal_text"] == "replaced"
    # A broken ledger does not fail the request.
    monkeypatch.setattr(EgressLog, "record", lambda *a, **k: 1 / 0)
    assert engine.generate_sync("p") == "ok"


def test_agent_loop_model_one_row_per_call(ledger, monkeypatch):
    assert _hooked("ai/orchestrator.py")
    from smolagents.models import ChatMessage, OpenAIServerModel

    from ai.orchestrator import _make_groq_model_class

    fake = _fake_openai("final words")
    model = _make_groq_model_class(OpenAIServerModel)(
        model_id="gm", api_base="https://api.groq.com/openai/v1", api_key="k",
        client=fake, flatten_messages_as_text=False,
    )
    model.generate([ChatMessage(role="user", content="hi")])
    model.generate([ChatMessage(role="user", content="again")])
    rows = ledger.rows()
    assert len(rows) == 2 == len(fake.calls)
    assert {(r["host"], r["role"], r["model"]) for r in rows} == {("api.groq.com", "main", "gm")}
    assert all(r["bytes_out"] > 0 and r["bytes_in"] > 0 for r in rows)


def test_local_engine_records_nothing(ledger, monkeypatch):
    from ai.mlx_engine import MLXEngine

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(
        generate=lambda *a, **k: "local reply", load=lambda name: (object(), object())))
    engine = MLXEngine("/no/such/local/model")
    asyncio.run(engine.load())
    assert engine.generate_sync("hi") == "local reply"
    assert ledger.count() == 0


def test_open_url_records_host_only(ledger, monkeypatch):
    assert _hooked("tools/open_url.py")
    import subprocess

    from tools.open_url import open_url

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    fn = getattr(open_url, "forward", open_url)
    assert fn("https://example.org/a?q=private")["status"] == "ok"
    assert fn("ftp://nope")["status"] == "error"
    [row] = ledger.rows()
    assert row["host"] == "example.org" and row["text"] is None


def test_sandboxed_tool_records_only_with_network(ledger, monkeypatch):
    assert _hooked("tools/tool_sandbox.py")
    from tools import tool_sandbox

    monkeypatch.setattr(tool_sandbox, "_spawn", lambda *a, **k: {"result": "r"})
    meta = {"name": "fetch_weather", "description": "d", "output_type": "string",
            "inputs": {"city": {"type": "string", "description": "c"}}}
    net = tool_sandbox.SandboxedTool(meta, Path("/tmp/x.py"), "imports urllib.request")
    off = tool_sandbox.SandboxedTool(dict(meta, name="offline"), Path("/tmp/y.py"), None)
    assert net.forward(city="Paris") == "r" and off.forward(city="Paris") == "r"
    [row] = ledger.rows()
    assert (row["kind"], row["tool"], row["host"]) == ("sandboxed_tool", "fetch_weather", None)
    assert row["bytes_out"] > 0
