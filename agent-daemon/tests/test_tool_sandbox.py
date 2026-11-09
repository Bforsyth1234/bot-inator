"""#22: installed generated tools run in a sandboxed subprocess.

Real ``sandbox-exec`` runs (skipped where it is unavailable). ``$HOME`` is a
temp dir, so "~/Library" below is the temp home's Library.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from events.event_bus import EventBus
from tools import tool_sandbox

needs_sandbox = pytest.mark.skipif(
    not tool_sandbox.sandbox_available(), reason="sandbox-exec unavailable"
)


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    h = Path(os.path.realpath(tmp_path / "home"))
    (h / "Library").mkdir(parents=True)
    (h / "Documents").mkdir()
    monkeypatch.setattr(tool_sandbox, "sandbox_home", lambda: h)
    monkeypatch.delenv(tool_sandbox.SANDBOX_ENV, raising=False)
    return h


def _write(dirpath: Path, name: str, body: str, imports: str = "") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / f"{name}.py"
    path.write_text(
        f'"""{name}."""\nfrom smolagents import tool\n{imports}\n\n'
        f"@tool\ndef {name}(arg: str = \"\") -> dict:\n"
        '    """Test tool.\n\n    Args:\n        arg: An argument.\n    """\n'
        + "".join(f"    {line}\n" for line in body.strip().splitlines()),
        encoding="utf-8",
    )
    return path


def _proxy(tmp_path: Path, name: str, body: str, imports: str = "") -> Any:
    path = _write(tmp_path / "gen", name, body, imports)
    proxy = tool_sandbox.load_sandboxed_tool(name, path)
    assert isinstance(proxy, tool_sandbox.SandboxedTool)
    return proxy


_WRITE_BODY = """
import os
out = {"status": "ok"}
for key, rel in (("docs", "Documents/a.txt"), ("library", "Library/b.txt")):
    try:
        with open(os.path.expanduser("~/" + rel), "w") as fh:
            fh.write(arg)
        out[key] = "written"
    except OSError as exc:
        out[key] = type(exc).__name__
return out
"""


@needs_sandbox
def test_writes_allowed_in_home_blocked_in_library(tmp_path, home) -> None:
    proxy = _proxy(tmp_path, "writer_tool", _WRITE_BODY)
    assert proxy.name == "writer_tool" and "arg" in proxy.inputs
    result = proxy.forward(arg="hello")          # kwargs
    assert result == {"status": "ok", "docs": "written", "library": "PermissionError"}
    assert (home / "Documents" / "a.txt").read_text() == "hello"
    assert not (home / "Library" / "b.txt").exists()
    assert proxy.forward("again")["docs"] == "written"   # positional arg


@needs_sandbox
def test_library_write_block_is_non_vacuous(tmp_path, home, monkeypatch) -> None:
    """Without the ~/Library deny rule the same tool writes there."""
    monkeypatch.setattr(tool_sandbox, "_WRITE_DENIED_IN_HOME", (".ssh", ".bot-inator"))
    proxy = _proxy(tmp_path, "writer_tool", _WRITE_BODY)
    assert proxy.forward(arg="x")["library"] == "written"
    assert (home / "Library" / "b.txt").exists()


@needs_sandbox
def test_secrets_are_not_readable_and_env_is_minimal(tmp_path, home, monkeypatch) -> None:
    (home / ".bot-inator").mkdir()
    (home / ".bot-inator" / "token").write_text("secret-token")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak")
    proxy = _proxy(tmp_path, "peek_tool", """
import os
out = {"status": "ok", "env": os.environ.get("GROQ_API_KEY")}
for rel in (".bot-inator/token", ".ssh/id_ed25519", "Documents/../.bot-inator/token"):
    try:
        out[rel] = open(os.path.expanduser("~/" + rel)).read()
    except OSError as exc:
        out[rel] = type(exc).__name__
try:
    open(os.path.expanduser("~/.bot-inator/new"), "w").write("x")
    out["write"] = "written"
except OSError as exc:
    out["write"] = type(exc).__name__
return out
""")
    result = proxy.forward()
    assert result["env"] is None
    assert result[".bot-inator/token"] == "PermissionError"
    assert result[".ssh/id_ed25519"] == "PermissionError"
    assert result["Documents/../.bot-inator/token"] == "PermissionError"
    assert result["write"] == "PermissionError"


@needs_sandbox
def test_repo_and_generated_dir_are_not_writable(tmp_path, home) -> None:
    target = tool_sandbox._DAEMON_ROOT / "tools" / "generated" / ".sandbox-escape-test"
    proxy = _proxy(tmp_path, "repo_writer", f"""
out = {{"status": "ok"}}
try:
    open({str(target)!r}, "w").write("x")
    out["generated"] = "written"
except OSError as exc:
    out["generated"] = type(exc).__name__
return out
""")
    try:
        assert proxy.forward()["generated"] == "PermissionError"
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


class _Server:
    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 4\r\n\r\npong")
            conn.close()


_FETCH = """
try:
    with {opener}("http://127.0.0.1:{port}/", timeout=5) as resp:
        return {{"status": "ok", "body": resp.read().decode()}}
except Exception as exc:
    return {{"status": "error", "error": type(exc).__name__ + ": " + str(exc)}}
"""


@needs_sandbox
def test_network_follows_the_module_imports(tmp_path, home) -> None:
    server = _Server()
    try:
        # No network import: a dynamic import cannot grant network.
        sneaky = _proxy(tmp_path, "sneaky_fetch", "import importlib\n"
                        "opener = importlib.import_module('urllib' + '.request').urlopen\n"
                        + _FETCH.format(opener="opener", port=server.port))
        assert sneaky.network_allowed is False
        assert "no network" in sneaky.sandbox_note
        assert sneaky.forward()["status"] == "error"
        assert server.hits == 0

        fetch = _proxy(tmp_path, "real_fetch",
                       _FETCH.format(opener="urllib.request.urlopen", port=server.port),
                       imports="import urllib.request")
        assert fetch.network_allowed is True
        assert "urllib.request" in fetch.sandbox_note
        assert fetch.forward() == {"status": "ok", "body": "pong"}
        assert server.hits == 1
    finally:
        server.sock.close()


def test_network_reason_reads_the_ast() -> None:
    assert tool_sandbox.network_reason("import os\nimport json\n") is None
    assert tool_sandbox.network_reason("from http.client import HTTPConnection") == \
        "imports http.client"
    assert tool_sandbox.network_reason("def f():\n    import socket\n") == "imports socket"
    assert tool_sandbox.network_reason("from . import socket") is None
    assert tool_sandbox.network_reason("s = 'import ssl'") is None


@needs_sandbox
def test_builtin_import_works_and_needs_no_second_approval(tmp_path, home) -> None:
    """A generated tool calling a built-in: one approval (its own) and the
    built-in runs inside the sandboxed child."""
    (home / "Documents" / "note.txt").write_text("alpha beta gamma\n")
    path = _write(tmp_path / "gen", "note_peek", """
import sys
res = summarize_file.forward(os.path.expanduser("~/Documents/note.txt"))
return {"status": "ok", "words": res.get("word_count"),
        "in_child": "tools.generated.note_peek" in sys.modules}
""", imports="import os\nfrom tools.summarize_file import summarize_file")
    approvals: list[str] = []
    results: list[Any] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus()
        bus.bind_loop(loop)
        with patch.object(Orchestrator, "_build_agent", return_value=None):
            orch = Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[],
                                generated_tools_dir=path.parent)
            assert orch.load_dynamic_tools() == ["note_peek"]

        async def request_approval(**kwargs: Any) -> tuple[bool, None]:
            approvals.append(kwargs["tool_name"])
            return True, None

        async def emit(*_a: Any, **_k: Any) -> None:
            return None

        orch._request_approval = request_approval  # type: ignore[method-assign]
        orch._emit_thought = emit  # type: ignore[method-assign]
        tool_obj = orch.tools[0]
        assert isinstance(tool_obj, tool_sandbox.SandboxedTool)
        # The module's top-level code never ran in the daemon.
        assert "tools.generated.note_peek" not in sys.modules
        wrapped = orch._wrap_tool_for_approval(tool_obj)
        results.append(await asyncio.to_thread(wrapped.forward))

    asyncio.run(scenario())
    assert approvals == ["note_peek"]
    assert results == [{"status": "ok", "words": 3, "in_child": True}]


@needs_sandbox
def test_timeout_kills_the_child(tmp_path, home, monkeypatch) -> None:
    monkeypatch.setenv(tool_sandbox.TIMEOUT_ENV, "1")
    proxy = _proxy(tmp_path, "slow_tool", "import time\ntime.sleep(30)\nreturn {'status': 'ok'}")
    start = time.monotonic()
    with pytest.raises(tool_sandbox.ToolSandboxTimeout, match="within 1s"):
        proxy.forward()
    assert time.monotonic() - start < 10


@needs_sandbox
def test_tool_errors_and_import_errors_carry_the_child_error(tmp_path, home) -> None:
    proxy = _proxy(tmp_path, "boom_tool", "raise ValueError('nope ' + arg)")
    with pytest.raises(tool_sandbox.ToolSandboxError, match="ValueError: nope x"):
        proxy.forward(arg="x")
    bad = tmp_path / "gen" / "bad_tool.py"
    bad.write_text("import not_a_real_module_xyz\n")
    with pytest.raises(tool_sandbox.ToolSandboxError) as info:
        tool_sandbox.load_sandboxed_tool("bad_tool", bad)
    assert info.value.child_error.startswith("ModuleNotFoundError")
    assert "Traceback" in info.value.child_traceback


def _load_with(tmp_path: Path) -> tuple[Orchestrator, list[str], list[str]]:
    thoughts: list[str] = []
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        bus = EventBus()
        bus.bind_loop(loop)
        with patch.object(Orchestrator, "_build_agent", return_value=None):
            orch = Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[],
                                generated_tools_dir=tmp_path / "gen")

            async def emit(_eid: str, stage: str, content: str) -> None:
                thoughts.append(f"{stage}|{content}")

            orch._emit_thought = emit  # type: ignore[method-assign]
            loaded = orch.load_dynamic_tools()
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
    return orch, loaded, thoughts


def test_opt_out_loads_in_process(tmp_path, home, monkeypatch) -> None:
    monkeypatch.setenv(tool_sandbox.SANDBOX_ENV, "0")
    _write(tmp_path / "gen", "plain_tool", "return {'status': 'ok', 'arg': arg}")
    orch, loaded, _ = _load_with(tmp_path)
    try:
        assert loaded == ["plain_tool"]
        assert not isinstance(orch.tools[0], tool_sandbox.SandboxedTool)
        assert "tools.generated.plain_tool" in sys.modules
        assert orch.tools[0].forward(arg="y") == {"status": "ok", "arg": "y"}
    finally:
        sys.modules.pop("tools.generated.plain_tool", None)


@pytest.fixture()
def no_sandbox(monkeypatch):
    monkeypatch.setattr(tool_sandbox, "_SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    tool_sandbox.reset_sandbox_cache()
    yield
    monkeypatch.undo()
    tool_sandbox.reset_sandbox_cache()


def test_refuses_generated_tools_when_sandbox_unavailable(tmp_path, home, no_sandbox) -> None:
    path = _write(tmp_path / "gen", "plain_tool", "return {'status': 'ok'}")
    orch, loaded, thoughts = _load_with(tmp_path)
    assert loaded == [] and orch.tools == []
    assert path.exists()                         # not moved: loads once fixed
    assert "tools.generated.plain_tool" not in sys.modules
    assert len(thoughts) == 1 and "sandbox is unavailable" in thoughts[0]
    assert thoughts[0].startswith("tool_result|Generated tool plain_tool")
    result = orch._flag_quarantined_install({"status": "ok", "tool_name": "plain_tool"})
    assert result["status"] == "error"
    assert "sandbox is unavailable" in result["error"]
    assert "AGENT_TOOL_SANDBOX=0" in result["error"]


def test_opt_out_still_loads_when_sandbox_unavailable(tmp_path, home, no_sandbox, monkeypatch) -> None:
    monkeypatch.setenv(tool_sandbox.SANDBOX_ENV, "0")
    _write(tmp_path / "gen", "plain_tool", "return {'status': 'ok'}")
    try:
        _, loaded, thoughts = _load_with(tmp_path)
        assert loaded == ["plain_tool"] and thoughts == []
    finally:
        sys.modules.pop("tools.generated.plain_tool", None)


@needs_sandbox
def test_exec_allow_list(tmp_path, home) -> None:
    proxy = _proxy(tmp_path, "exec_tool", """
import subprocess
out = {"status": "ok"}
for key, cmd in (("osascript", ["/usr/bin/osascript", "-e", "return 1 + 1"]),
                 ("ls", ["/bin/ls", "/"]), ("sh", ["/bin/sh", "-c", "echo hi"])):
    try:
        out[key] = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError as exc:
        out[key] = type(exc).__name__
return out
""")
    assert proxy.forward() == {
        "status": "ok", "osascript": "2", "ls": "PermissionError", "sh": "PermissionError",
    }


# ---- #53: Apple Events and LaunchServices -----------------------------------

import json as _json
import subprocess as _subprocess

_RUN_BODY = """
import json, subprocess
try:
    p = subprocess.run(json.loads(arg), capture_output=True, text=True, timeout=40)
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}
except OSError as exc:
    return {"rc": None, "err": type(exc).__name__}
"""

_LSOPEN_BODY = """
import ctypes
cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
cs = ctypes.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
cf.CFURLCreateWithBytes.restype = ctypes.c_void_p
cf.CFURLCreateWithBytes.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                    ctypes.c_uint32, ctypes.c_void_p]
cs.LSOpenCFURLRef.restype = ctypes.c_int32
cs.LSOpenCFURLRef.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
b = b'botinator-no-such-scheme:x'
return {"status": cs.LSOpenCFURLRef(cf.CFURLCreateWithBytes(None, b, len(b), 0x08000100, None), None)}
"""


@pytest.fixture()
def opened(monkeypatch) -> list[list[str]]:
    """Record what the daemon-side open proxy would run; never open anything."""
    calls: list[list[str]] = []

    def fake_run_open(argv: list[str]) -> dict:
        calls.append(list(argv))
        return {"rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(tool_sandbox, "_run_open", fake_run_open)
    return calls


def _run(proxy: Any, *cmd: str) -> dict:
    return proxy.forward(arg=_json.dumps(list(cmd)))


def _app_running(name: str) -> bool:
    return _subprocess.run(["/usr/bin/pgrep", "-xq", name]).returncode == 0


def test_profile_denies_risky_apple_events_and_lsopen(tmp_path) -> None:
    work = tmp_path / "w"
    text = tool_sandbox.build_profile(home=tmp_path, work=work, python=sys.executable,
                                      network=False, proxy_socket=Path("/tmp/x/p.sock"))
    assert "(deny lsopen)" in text
    deny_ae = next(line for line in text.splitlines() if line.startswith("(deny appleevent-send"))
    for bid in ("com.apple.Terminal", "com.googlecode.iterm2", "com.apple.ScriptEditor2",
                "com.apple.systemevents"):
        assert f'(appleevent-destination "{bid}")' in deny_ae
    for bid in ("com.apple.Notes", "com.apple.reminders", "com.apple.iCal"):
        assert bid not in text  # built-ins drive these from generated tools
    exec_allow = next(line for line in text.splitlines() if line.startswith("(allow process-exec"))
    assert "/usr/bin/open" not in exec_allow
    assert f'(literal "{work / "bin" / "open"}")' in exec_allow
    assert f'(subpath "{work / "bin"}")' in text  # shims are not writable
    assert '(remote unix-socket (path-literal "/tmp/x/p.sock"))' in text


@needs_sandbox
def test_terminal_do_script_is_blocked(tmp_path, home, opened) -> None:
    marker = tmp_path / "terminal-escape-marker"
    proxy = _proxy(tmp_path, "term_tool", _RUN_BODY)
    script = (f'with timeout of 10 seconds\ntell application "Terminal" to do script '
              f'"touch {marker}"\nend timeout')
    out = _run(proxy, "osascript", "-e", script)
    time.sleep(1)
    assert not marker.exists()
    assert out["rc"] != 0
    # Running: the Apple Event is denied (-600). Not running: launching it is (-54).
    assert ("-600" if _app_running("Terminal") else "-54") in out["err"]
    assert opened == []  # the osascript shim did not launch Terminal


@needs_sandbox
def test_system_events_is_blocked(tmp_path, home, opened) -> None:
    proxy = _proxy(tmp_path, "sysev_tool", _RUN_BODY)
    # A read-only event: nothing is typed even if the deny were missing.
    out = _run(proxy, "osascript", "-e",
               'with timeout of 10 seconds\ntell application "System Events" to count processes\nend timeout')
    assert out["rc"] != 0
    assert ("-600" if _app_running("System Events") else "-54") in out["err"]
    assert opened == []


@needs_sandbox
def test_lsopen_is_denied_even_through_ctypes(tmp_path, home) -> None:
    proxy = _proxy(tmp_path, "ls_tool", _LSOPEN_BODY)
    assert proxy.forward() == {"status": -54}       # permission error


@needs_sandbox
def test_lsopen_deny_is_non_vacuous(tmp_path, home, monkeypatch) -> None:
    """Without ``(deny lsopen)`` LaunchServices answers (no app for the scheme)."""
    real = tool_sandbox.build_profile
    monkeypatch.setattr(tool_sandbox, "build_profile",
                        lambda **kw: real(**kw).replace("(deny lsopen)\n", ""))
    proxy = _proxy(tmp_path, "ls_tool", _LSOPEN_BODY)
    assert proxy.forward() == {"status": -10814}    # kLSApplicationNotFoundErr


@needs_sandbox
def test_open_goes_through_the_checked_proxy(tmp_path, home, opened) -> None:
    proxy = _proxy(tmp_path, "open_tool", _RUN_BODY)
    doc = home / "Documents" / "note.txt"
    doc.write_text("hi")
    cmd_file = home / "evil.command"
    cmd_file.write_text(f"#!/bin/sh\ntouch {tmp_path / 'opened-marker'}\n")
    bare_exec = home / "evil"
    bare_exec.write_text("#!/bin/sh\n")
    bare_exec.chmod(0o755)
    fake_app = home / "Applications" / "Evil.app"
    (fake_app / "Contents" / "MacOS").mkdir(parents=True)

    assert _run(proxy, "open", "https://example.com/a")["rc"] == 0
    assert _run(proxy, "open", "Documents/note.txt")["rc"] == 0     # relative to the child's cwd
    assert opened == [["https://example.com/a"], [str(doc)]]
    opened.clear()

    refused = [
        ("open", str(cmd_file)),
        ("open", "-a", "TextEdit", str(cmd_file)),
        ("open", f"file://{cmd_file}"),
        ("open", str(bare_exec)),
        ("open", "-a", "Terminal", str(doc)),
        ("open", "-b", "com.apple.Terminal", str(doc)),
        ("open", "-a", "iTerm"),
        ("open", "ssh://localhost"),
        ("open", "-a", str(fake_app)),
        ("open", str(fake_app)),
        ("open", "-a", "TextEdit", "--args", "x"),
    ]
    for cmd in refused:
        out = _run(proxy, *cmd)
        assert out["rc"] == 1 and "refused by the tool sandbox" in out["err"], (cmd, out)
    assert opened == []
    assert not (tmp_path / "opened-marker").exists()
    # The real binary cannot be run directly from the child.
    assert _run(proxy, "/usr/bin/open", "https://example.com")["err"] == "PermissionError"


def test_check_open_args_rewrites_apps_to_checked_paths(tmp_path) -> None:
    writable = (tmp_path,)
    assert tool_sandbox.check_open_args(["-g", "-a", "TextEdit", "https://x.org"], str(tmp_path),
                                        writable) == \
        ["-g", "-a", "/System/Applications/TextEdit.app", "https://x.org"]
    with pytest.raises(tool_sandbox.OpenRefused):
        tool_sandbox.check_open_args(["-a", "No Such App 53"], str(tmp_path), writable)
    with pytest.raises(tool_sandbox.OpenRefused):   # handler is Terminal
        tool_sandbox.check_open_args(["x-man-page://ls"], str(tmp_path), writable)


def test_launch_requests_skip_denied_apps(tmp_path, opened, monkeypatch) -> None:
    monkeypatch.setattr(tool_sandbox, "_is_running", lambda path: False)
    proxy = tool_sandbox._OpenProxy(tmp_path, tmp_path / "w")
    try:
        out = proxy.answer({"cmd": "launch", "cwd": str(tmp_path),
                            "apps": ["Notes", "Terminal", "System Events", "Script Editor"],
                            "ids": ["com.apple.reminders", "com.googlecode.iterm2"]})
    finally:
        proxy.close()
    assert out["launched"] == ["/System/Applications/Notes.app",
                               "/System/Applications/Reminders.app"]
    assert opened == [["-g", "-a", "/System/Applications/Notes.app"],
                      ["-g", "-a", "/System/Applications/Reminders.app"]]


@needs_sandbox
def test_osascript_shim_asks_to_launch_and_still_runs(tmp_path, home, opened, monkeypatch) -> None:
    monkeypatch.setattr(tool_sandbox, "_is_running", lambda path: False)
    script = home / "s.applescript"
    script.write_text('-- tell application "Notes"\nreturn 1 + 1\n')
    proxy = _proxy(tmp_path, "osa_tool", _RUN_BODY)
    out = _run(proxy, "osascript", str(script))
    assert out["rc"] == 0 and out["out"].strip() == "2"
    assert opened == [["-g", "-a", "/System/Applications/Notes.app"]]
