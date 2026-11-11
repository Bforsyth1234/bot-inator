"""Privacy ledger rows for a trusted generated tool (#109).

A trusted tool (#104) runs in the daemon process. ``egress_log.trusted_tool_run``
installs an audit hook and records the non-loopback hosts the call's own
Python code connected to, or the program it launched (user decision (a)).

Every socket here is loopback. A non-loopback connection is simulated by
raising the same audit event the interpreter raises before ``connect``
(``sys.audit("socket.connect", ...)``), so no packet ever leaves the Mac.
Real ``socket`` / ``urllib`` / ``subprocess`` paths are exercised against
loopback, with the loopback test switched off where a test needs a row.
"""
from __future__ import annotations

import asyncio
import http.server
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import egress_log
from ai.egress_log import EgressLog
from tests.test_notices_privacy import _LoopThread, _drain

REMOTE_IP = "203.0.113.5"  # TEST-NET-3: never contacted, only named in an event


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(egress_log.DB_PATH_ENV, str(tmp_path / "egress.db"))
    monkeypatch.delenv(egress_log.KEEP_TEXT_KEY, raising=False)
    log = EgressLog()
    egress_log.set_default_log(log)
    yield log
    egress_log.set_default_log(None)
    log.close()


def _trusted_rows(log: EgressLog) -> list[dict[str, Any]]:
    return [r for r in log.rows() if r["kind"] == egress_log.KIND_TRUSTED_TOOL]


def _fake_remote_connect(name: str | None = "api.example.com") -> None:
    """The events ``socket.create_connection((name, 443))`` raises, minus the packet."""
    if name:
        sys.audit("socket.getaddrinfo", name, 443, 0, socket.SOCK_STREAM, 0)
    sys.audit("socket.connect", None, (REMOTE_IP, 443))


@pytest.fixture
def loopback_http():
    """A loopback HTTP server answering every GET with ``ok``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


# ---- rows ----------------------------------------------------------------


def test_remote_connection_is_one_row_with_tool_and_host(ledger) -> None:
    with egress_log.trusted_tool_run("fetch_weather", {"args": ["Paris"], "kwargs": {}}):
        _fake_remote_connect()
        _fake_remote_connect()  # same host twice: still one row
    rows = _trusted_rows(ledger)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "fetch_weather" and row["host"] == "api.example.com"
    assert "best effort" in row["note"] and egress_log.SIZE_NOT_VISIBLE in row["note"]
    assert row["text"] is None  # keep-text is off
    assert ledger.stats()["count"] == 1


def test_one_row_per_host_and_ip_when_no_name(ledger) -> None:
    with egress_log.trusted_tool_run("t"):
        _fake_remote_connect("a.example.com")
        sys.audit("socket.getaddrinfo", "b.example.com", 443, 0, 0, 0)
        sys.audit("socket.connect", None, ("2001:db8::1", 443, 0, 0))
    with egress_log.trusted_tool_run("raw"):
        sys.audit("socket.connect", None, (REMOTE_IP, 9))
    hosts = sorted((r["tool"], r["host"]) for r in _trusted_rows(ledger))
    assert hosts == [("raw", REMOTE_IP), ("t", "a.example.com"), ("t", "b.example.com")]


def test_arguments_kept_only_under_keep_text_and_never_urls(ledger) -> None:
    ledger.keep_text = True
    with egress_log.trusted_tool_run("t", {"args": ["Paris"], "kwargs": {}}):
        sys.audit("urllib.Request", "https://api.example.com/secret?q=1", b"body", {}, "GET")
        _fake_remote_connect()
    row = _trusted_rows(ledger)[0]
    assert "Paris" in row["text"]
    assert "secret" not in str(row)


def test_loopback_connection_is_no_row(ledger) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        with egress_log.trusted_tool_run("local_only"):
            with socket.create_connection(("localhost", port), timeout=2):
                pass
            sys.audit("socket.connect", None, ("::1", port, 0, 0))
            sys.audit("socket.connect", None, ("0.0.0.0", port))
            sys.audit("socket.connect", None, "/tmp/some.sock")  # AF_UNIX
    finally:
        server.close()
    assert _trusted_rows(ledger) == []
    assert ledger.stats()["summary"] == egress_log.NOTHING_TODAY


def test_real_socket_and_urllib_events_reach_the_hook(ledger, loopback_http, monkeypatch) -> None:
    """With loopback counted as remote, real urllib code yields a row naming
    the host it resolved, with http.client's byte count."""
    monkeypatch.setattr(egress_log, "_is_local_host", lambda h: False)
    with egress_log.trusted_tool_run("fetcher"):
        with urllib.request.urlopen(f"http://localhost:{loopback_http}/x?q=SECRETQUERY", timeout=5) as r:
            assert r.read() == b"ok"
    rows = _trusted_rows(ledger)
    assert [r["host"] for r in rows] == ["localhost"]
    assert rows[0]["bytes_out"] > 0
    assert egress_log.SIZE_NOT_VISIBLE not in rows[0]["note"]
    assert "SECRETQUERY" not in str(rows[0])


def test_no_connection_and_no_program_is_no_row(ledger) -> None:
    with egress_log.trusted_tool_run("pure", {"args": [1], "kwargs": {}}):
        sum(range(100))
        open(__file__).close()
    assert _trusted_rows(ledger) == []


# ---- launched programs (user decision (a)) ---------------------------------


def test_launched_program_is_one_row_naming_it_never_its_arguments(ledger) -> None:
    with egress_log.trusted_tool_run("close_app"):
        subprocess.run(["/usr/bin/true", "SECRET-ARG"], check=True)
        subprocess.run("FOO=1 /usr/bin/true SECRET-SHELL", shell=True, check=True)
    rows = _trusted_rows(ledger)
    assert len(rows) == 1
    row = rows[0]
    assert row["host"] is None and row["tool"] == "close_app"
    assert row["note"] == egress_log.LAUNCHED_NOTE.format(program="true")
    assert "SECRET" not in str(row)


def test_launched_program_rows_are_out_of_the_days_total(ledger) -> None:
    with egress_log.trusted_tool_run("close_app"):
        sys.audit("subprocess.Popen", "/usr/bin/osascript", ["osascript", "-e", "quit"], None, None)
    s = ledger.stats()
    assert s["count"] == 0 and s["bytes_out"] == 0 and s["by_kind"] == {}
    assert s["not_visible"] == 1
    assert s["summary"].startswith(egress_log.NOTHING_TODAY)
    assert "1 program a trusted tool ran" in s["summary"]
    ledger.record(egress_log.KIND_INFERENCE, "api.groq.com", "main", "m", 10, 5)
    with egress_log.trusted_tool_run("fetch"):
        _fake_remote_connect()
    s = ledger.stats()
    assert s["count"] == 2 and s["not_visible"] == 1
    assert s["summary"].startswith("2 requests left this Mac today")
    assert s["by_kind"] == {"inference": 1, "trusted_tool": 1}


def test_program_and_connection_note_both(ledger) -> None:
    with egress_log.trusted_tool_run("t"):
        sys.audit("os.system", "curl -s https://x.example/secret")
        _fake_remote_connect()
    rows = _trusted_rows(ledger)
    assert len(rows) == 1 and rows[0]["host"] == "api.example.com"
    assert "also ran curl" in rows[0]["note"] and "secret" not in rows[0]["note"]


# ---- never fails, delays or misattributes (decision 4) ----------------------


def test_nothing_recorded_when_no_trusted_tool_is_running(ledger) -> None:
    with egress_log.trusted_tool_run("t"):
        pass  # the hook is installed
    _fake_remote_connect()
    sys.audit("subprocess.Popen", "/usr/bin/curl", ["curl", "x"], None, None)
    egress_log.record_completion(role="main", model="m", api_base="https://api.groq.com/openai/v1",
                                 request={"messages": []})
    assert _trusted_rows(ledger) == []
    assert egress_log._RUN.get() is None


def test_hook_returns_at_once_outside_a_trusted_call(ledger) -> None:
    with egress_log.trusted_tool_run("t"):
        pass
    hook = egress_log._audit_hook
    n = 200_000
    start = time.perf_counter()
    for _ in range(n):
        hook("open", ("x", "r", 0))
        hook("socket.connect", (None, (REMOTE_IP, 443)))
    per_event = (time.perf_counter() - start) / (2 * n)
    assert per_event < 5e-6  # measured ~0.1 µs on an M-series Mac


def test_a_thread_the_tool_starts_is_not_attributed(ledger) -> None:
    """Finding 2: a new thread has an empty context. Its events (and the
    daemon's own threads' events) are never guessed onto the tool."""
    with egress_log.trusted_tool_run("t"):
        th = threading.Thread(target=_fake_remote_connect)
        th.start()
        th.join()
    assert _trusted_rows(ledger) == []


def test_hook_error_does_not_fail_the_tool(ledger, monkeypatch) -> None:
    def boom(*a: Any) -> None:
        raise RuntimeError("hook bug")

    monkeypatch.setattr(egress_log, "_note_connect", boom)
    monkeypatch.setattr(egress_log, "_program_name", boom)
    with egress_log.trusted_tool_run("t"):
        _fake_remote_connect()
        subprocess.run(["/usr/bin/true"], check=True)
        result = "tool result"
    assert result == "tool result"


def test_ledger_error_does_not_fail_the_tool(ledger, monkeypatch) -> None:
    def boom(*a: Any, **k: Any) -> bool:
        raise sqlite_error

    sqlite_error = RuntimeError("disk full")
    monkeypatch.setattr(ledger, "record", boom)
    with egress_log.trusted_tool_run("t"):
        _fake_remote_connect()
    monkeypatch.setattr(egress_log, "default_log", boom)
    with egress_log.trusted_tool_run("t"):
        _fake_remote_connect()
    monkeypatch.setattr(egress_log, "_install_hook", boom)
    with egress_log.trusted_tool_run("t"):
        _fake_remote_connect()
    assert egress_log._RUN.get() is None


def test_tool_exception_propagates_and_row_is_kept(ledger) -> None:
    with pytest.raises(ValueError, match="tool failed"):
        with egress_log.trusted_tool_run("t"):
            _fake_remote_connect()
            raise ValueError("tool failed")
    assert len(_trusted_rows(ledger)) == 1
    assert egress_log._RUN.get() is None


def test_nested_runs_restore_the_outer_tool(ledger) -> None:
    with egress_log.trusted_tool_run("outer"):
        with egress_log.trusted_tool_run("inner"):
            _fake_remote_connect("inner.example.com")
        _fake_remote_connect("outer.example.com")
    assert sorted((r["tool"], r["host"]) for r in _trusted_rows(ledger)) == [
        ("inner", "inner.example.com"), ("outer", "outer.example.com"),
    ]


# ---- through the approval wrapper ------------------------------------------


class _FakeTool:
    inputs = {"city": {"type": "string"}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def forward(self, city: str) -> str:
        self.calls += 1
        _fake_remote_connect()
        return f"sunny in {city}"


def _call_through_gate(tool: _FakeTool, trusted: bool) -> Any:
    from ai.mlx_engine import MLXEngine
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus

    with _LoopThread() as loop:
        bus = EventBus()
        bus.bind_loop(loop)
        orch = Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[])
        if trusted:
            orch.__dict__.setdefault("_unsandboxed_tools", set()).add(tool.name)

        async def request_approval(**kwargs: Any) -> tuple[bool, None]:
            return True, None

        orch._request_approval = request_approval  # type: ignore[method-assign]
        wrapped = orch._wrap_tool_for_approval(tool)
        out = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(wrapped.forward, "Paris"), loop).result(10)
        _drain(loop)
    return out


def test_trusted_tool_through_the_gate_gets_a_row(ledger) -> None:
    tool = _FakeTool("fetch_weather")
    assert _call_through_gate(tool, trusted=True) == "sunny in Paris"
    rows = _trusted_rows(ledger)
    assert [(r["tool"], r["host"]) for r in rows] == [("fetch_weather", "api.example.com")]


def test_sandboxed_tool_through_the_gate_gets_no_trusted_row(ledger) -> None:
    """Not in ``_unsandboxed_tools``: the sandbox proxy records it, not this hook."""
    tool = _FakeTool("fetch_weather")
    assert _call_through_gate(tool, trusted=False) == "sunny in Paris"
    assert tool.calls == 1
    assert _trusted_rows(ledger) == []


def test_trust_confirm_note_and_limit_note_say_best_effort() -> None:
    from ai import tool_trust

    assert "best effort" in tool_trust.NETWORK_NOTE
    assert "privacy ledger" in tool_trust.NETWORK_NOTE.format(reason="urllib")
    assert "best effort" in egress_log.LIMIT_NOTE
    assert egress_log.KIND_TRUSTED_TOOL in egress_log.KINDS
