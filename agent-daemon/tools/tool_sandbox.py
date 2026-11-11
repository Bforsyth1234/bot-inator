"""Run installed generated tools in a sandboxed subprocess (todo #22).

An approved generated tool no longer runs inside the daemon process. The
daemon keeps a thin proxy :class:`SandboxedTool` (same ``name``,
``description``, ``inputs``, ``output_type``); its ``forward`` sends the call
as JSON to a child Python that imports the module and calls the real tool
under macOS ``sandbox-exec``, and returns the child's JSON result.

Even the module's top-level code never runs in the daemon: at load time a
sandboxed ``describe`` child imports it and reports the tool's metadata.

Sandbox profile (:func:`build_profile`):

* writes: allowed under ``$HOME`` and the per-call work dir, denied under
  ``~/Library``, ``~/.ssh``, ``~/.bot-inator``, shell start-up files and the
  bot-inator repo (``tools/generated`` plus the daemon's own source, venv
  and ``.env``: a write there would run unsandboxed on the next start);
* reads: allowed, except ``~/.ssh``, ``~/.bot-inator`` (API token, memory)
  and the daemon's ``.env`` (cloud API keys);
* network: denied unless the module imports a network module
  (:data:`NETWORK_MODULES`, decided from the AST at load time and exposed
  as ``network_allowed`` / ``sandbox_note`` on the proxy). A dynamic import
  cannot grant network: the profile is fixed when the tool loads;
* exec: only the Python interpreter, ``/usr/bin/osascript``,
  ``/usr/bin/shortcuts``, ``/usr/bin/mdfind`` and two shims on ``PATH``
  (``open``, ``osascript``; see below);
* Apple Events (todo #53): denied to Terminal, iTerm, Script Editor, System
  Events and Finder (:data:`DENIED_APPLE_EVENT_TARGETS`), so ``osascript``
  can still notify and drive Notes / Reminders / Calendar but cannot make
  another app run a shell command;
* LaunchServices opens (``lsopen``): denied. Every open/launch of a URL,
  file or app (``/usr/bin/open``, ``NSWorkspace``, ctypes ``LSOpen*``,
  AppleScript ``open location``) fails inside the child. ``open`` on
  ``PATH`` is a shim that asks the daemon over a per-call unix socket; the
  daemon checks the arguments (:func:`check_open_args`: no Terminal-like
  apps, no ``.command``/``.sh``/``.app``/executable targets, no ``ssh:``
  URLs, no apps the child could have written) and runs ``/usr/bin/open``.
  The ``osascript`` shim asks the daemon to launch the apps a script
  ``tell``s (the child cannot launch them itself) and then runs
  ``/usr/bin/osascript``.

The child gets a minimal environment (no API keys) and ``dotenv`` is
disabled inside it, so importing ``config`` cannot pull the daemon's
``.env`` in. ``tools``/``ai`` are stub packages in the child: ``from
tools.show_notification import show_notification`` loads only that module.

Built-ins called from a generated tool run inside the child without a
second approval: the user approved the generated tool's source (which
shows the call) and approves every call of the generated tool itself; the
sandbox still bounds what the built-in can do (e.g. ``remember_preference``
cannot write ``~/.bot-inator``, ``generate_custom_tool`` cannot write
``tools/generated``).

``AGENT_TOOL_SANDBOX=0`` opts out (the old in-process import). When the
sandbox is on but ``sandbox-exec`` is missing or fails its self-test,
:func:`sandbox_available` is False and the orchestrator refuses to load
generated tools: they never run unsandboxed silently.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

SANDBOX_ENV = "AGENT_TOOL_SANDBOX"
TIMEOUT_ENV = "AGENT_TOOL_SANDBOX_TIMEOUT"
LINGER_ENV = "AGENT_TOOL_SANDBOX_LINGER"
DEFAULT_TIMEOUT_SECONDS = 60.0
DESCRIBE_TIMEOUT_SECONDS = 30.0
# A call returns as soon as the tool returns; background threads it started
# (e.g. a reminder timer) may keep the child alive this long.
DEFAULT_LINGER_SECONDS = 3600.0

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_DAEMON_ROOT = Path(__file__).resolve().parent.parent
_MARKER = "__TOOL_SANDBOX__"

# Top-level module names whose import grants the tool network access.
NETWORK_MODULES: frozenset[str] = frozenset({
    "urllib", "http", "socket", "ssl", "requests", "httpx", "aiohttp",
    "urllib3", "websocket", "websockets", "ftplib", "smtplib", "imaplib",
    "poplib", "xmlrpc",
})
ALLOWED_EXECUTABLES: tuple[str, ...] = (
    "/usr/bin/osascript", "/usr/bin/shortcuts", "/usr/bin/mdfind",
)
# Apple Events the child may not send (todo #53): apps that run a shell
# command for the sender (or open a .command file in one).
DENIED_APPLE_EVENT_TARGETS: tuple[str, ...] = (
    "com.apple.Terminal", "com.googlecode.iterm2", "com.apple.ScriptEditor2",
    "com.apple.systemevents", "com.apple.finder",
)
# Apps the open proxy will not open anything with or launch.
DENIED_OPEN_APPS: frozenset[str] = frozenset({
    "com.apple.terminal", "com.googlecode.iterm2", "com.apple.scripteditor2",
    "com.apple.systemevents", "com.apple.automator", "com.apple.automator.automator",
})
# Targets the open proxy refuses by suffix: scripts, apps, bundles that
# install or run, Terminal settings files, and location files (an .inetloc
# can hold an ssh: URL, a .fileloc a .command path).
DENIED_OPEN_SUFFIXES: frozenset[str] = frozenset({
    ".command", ".tool", ".sh", ".bash", ".zsh", ".csh", ".tcsh", ".ksh",
    ".fish", ".py", ".pyw", ".pl", ".rb", ".php", ".js", ".jxa", ".app",
    ".terminal", ".term", ".scpt", ".scptd", ".applescript", ".workflow",
    ".action", ".prefpane", ".saver", ".bundle", ".plugin", ".kext", ".pkg",
    ".mpkg", ".appex", ".xpc", ".itermcolors", ".inetloc", ".webloc",
    ".fileloc", ".url", ".shortcut", ".mobileconfig",
})
DENIED_URL_SCHEMES: frozenset[str] = frozenset({
    "ssh", "telnet", "rlogin", "sftp", "x-man-page", "iterm", "iterm2",
    "applescript", "terminal", "shortcuts", "vnc",
})
# ``open`` flags the proxy passes on (no --args, --env, --stdin, -f, ...).
_OPEN_PLAIN_FLAGS = frozenset("gjnFRWet")
OPEN_TIMEOUT_SECONDS = 30.0
# Under $HOME: writable except these (relative to $HOME).
_WRITE_DENIED_IN_HOME: tuple[str, ...] = ("Library", ".ssh", ".bot-inator")
_SHELL_STARTUP_FILES: tuple[str, ...] = (
    ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".bashrc", ".bash_profile",
    ".profile",
)
_READ_DENIED_IN_HOME: tuple[str, ...] = (".ssh", ".bot-inator")


class ToolSandboxError(RuntimeError):
    """The sandboxed child failed: ``child_error`` is its first error line."""

    def __init__(self, message: str, child_traceback: str = "") -> None:
        super().__init__(message)
        self.child_error = message
        self.child_traceback = child_traceback


class ToolSandboxTimeout(ToolSandboxError):
    pass


# ---- settings -------------------------------------------------------------


def sandbox_enabled() -> bool:
    """``AGENT_TOOL_SANDBOX=0`` runs generated tools in-process (default on)."""
    return os.environ.get(SANDBOX_ENV, "1").strip() != "0"


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def call_timeout() -> float:
    return _float_env(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)


def linger_seconds() -> float:
    return _float_env(LINGER_ENV, DEFAULT_LINGER_SECONDS)


def sandbox_home() -> Path:
    """The ``$HOME`` the child sees and the profile protects (tests patch it)."""
    return Path(os.path.realpath(Path.home()))


# ---- network decision -----------------------------------------------------


def network_reason(source: str) -> Optional[str]:
    """``"imports urllib.request"`` when the module may use the network."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            if name.split(".")[0] in NETWORK_MODULES:
                return f"imports {name}"
    return None


# ---- profile --------------------------------------------------------------


def _q(path: Any) -> str:
    return json.dumps(str(path))


def _protected_repo_dir(home: Path) -> Path:
    """The repo root, or just the daemon dir if the repo root contains $HOME."""
    repo = Path(os.path.realpath(_DAEMON_ROOT.parent))
    if home == repo or repo in home.parents:
        return Path(os.path.realpath(_DAEMON_ROOT))
    return repo


def build_profile(
    *, home: Path, work: Path, python: str, network: bool,
    extra_read_denied: tuple[Path, ...] = (),
    proxy_socket: Optional[Path] = None,
) -> str:
    base = os.path.realpath(sys.base_prefix)
    daemon = Path(os.path.realpath(_DAEMON_ROOT))
    shims = work / "bin"
    write_denied = [home / d for d in _WRITE_DENIED_IN_HOME]
    write_denied.append(_protected_repo_dir(home))
    write_denied.append(daemon / "tools" / "generated")
    write_denied.append(shims)
    write_literals = [home / f for f in _SHELL_STARTUP_FILES]
    read_denied = [home / d for d in _READ_DENIED_IN_HOME]
    read_literals = [daemon / ".env", *extra_read_denied]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (subpath {_q(home)}) (subpath {_q(work)}) '
        '(literal "/dev/null") (literal "/dev/tty"))',
        "(deny file-write* "
        + " ".join(f"(subpath {_q(p)})" for p in write_denied)
        + " "
        + " ".join(f"(literal {_q(p)})" for p in write_literals)
        + ")",
        "(deny file-read* "
        + " ".join(f"(subpath {_q(p)})" for p in read_denied)
        + " "
        + " ".join(f"(literal {_q(p)})" for p in read_literals)
        + ")",
    ]
    if not network:
        lines.append("(deny network*)")
        if proxy_socket is not None:
            lines.append(
                "(allow network-outbound (remote unix-socket (path-literal "
                f"{_q(proxy_socket)})))"
            )
    # #53: no opening/launching through LaunchServices (the daemon's open
    # proxy does it after checking the arguments) and no Apple Events to
    # apps that would run a shell command for the tool.
    lines.append("(deny lsopen)")
    lines.append(
        "(deny appleevent-send "
        + " ".join(f"(appleevent-destination {_q(b)})" for b in DENIED_APPLE_EVENT_TARGETS)
        + ")"
    )
    lines.append("(deny process-exec)")
    lines.append(
        f"(allow process-exec (literal {_q(python)}) (subpath {_q(base)}) "
        + " ".join(f"(literal {_q(p)})" for p in ALLOWED_EXECUTABLES)
        + " "
        + " ".join(f"(literal {_q(shims / n)})" for n in _SHIM_NAMES)
        + ")"
    )
    return "\n".join(lines) + "\n"


def _extra_read_denied() -> tuple[Path, ...]:
    """The API token file when ``AGENT_TOKEN_PATH`` moves it out of ~/.bot-inator."""
    override = os.environ.get("AGENT_TOKEN_PATH", "").strip()
    if not override:
        return ()
    return (Path(os.path.realpath(Path(override).expanduser())),)


# ---- open / launch proxy (todo #53) ----------------------------------------

_SHIM_NAMES: tuple[str, ...] = ("open", "osascript")

_SHIM_COMMON = r'''
import json, os, re, socket, sys
SOCK = %(sock)r
def _ask(req):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(%(timeout)r)
    s.connect(SOCK)
    s.sendall(json.dumps(req).encode("utf-8") + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode("utf-8") or "{}")
'''

_OPEN_SHIM = r'''
def main():
    try:
        resp = _ask({"cmd": "open", "argv": sys.argv[1:], "cwd": os.getcwd()})
    except (OSError, ValueError) as exc:
        sys.stderr.write("open: sandbox open proxy unavailable: %s\n" % exc)
        return 1
    sys.stdout.write(resp.get("stdout") or "")
    sys.stderr.write(resp.get("stderr") or "")
    return int(resp.get("rc", 1))
sys.exit(main())
'''

# The child cannot launch apps (lsopen is denied), so before running a
# script the shim asks the daemon to launch the apps it talks to.
_OSASCRIPT_SHIM = r'''
_APP = re.compile(r'application\s+(id\s+)?"([^"]+)"|Application\(\s*[\'"]([^\'"]+)[\'"]', re.I)
def _scan(args):
    texts, apps, ids = [], [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-e" and i + 1 < len(args):
            texts.append(args[i + 1]); i += 2; continue
        if a in ("-l", "-s") and i + 1 < len(args):
            i += 2; continue
        if a == "--":
            break
        if not a.startswith("-"):
            try:
                if os.path.getsize(a) < 1 << 20:
                    with open(a, "r", encoding="utf-8", errors="replace") as fh:
                        texts.append(fh.read())
            except OSError:
                pass
            break
        i += 1
    for text in texts:
        for m in _APP.finditer(text):
            if m.group(1):
                ids.append(m.group(2))
            else:
                apps.append(m.group(2) or m.group(3))
    return sorted(set(apps)), sorted(set(ids))
apps, ids = _scan(sys.argv[1:])
if apps or ids:
    try:
        _ask({"cmd": "launch", "apps": apps, "ids": ids})
    except (OSError, ValueError):
        pass
os.execv("/usr/bin/osascript", ["osascript"] + sys.argv[1:])
'''


def _write_shims(bin_dir: Path, sock: Path, python: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    common = _SHIM_COMMON % {"sock": str(sock), "timeout": OPEN_TIMEOUT_SECONDS + 15}
    for name, body in (("open", _OPEN_SHIM), ("osascript", _OSASCRIPT_SHIM)):
        path = bin_dir / name
        path.write_text(f"#!{python} -IS\n{common}{body}", encoding="utf-8")
        path.chmod(0o755)


class OpenRefused(ValueError):
    """The open proxy refused the arguments (the reason is the message)."""


def _workspace() -> Any:
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError:  # pragma: no cover - pyobjc ships with the daemon
        return None
    return NSWorkspace.sharedWorkspace()


def _bundle_id(app_path: str) -> str:
    try:
        from Foundation import NSBundle  # type: ignore
        bundle = NSBundle.bundleWithPath_(app_path)
        return str(bundle.bundleIdentifier() or "") if bundle is not None else ""
    except ImportError:  # pragma: no cover
        return ""


def _url_path(url: Any) -> Optional[str]:
    if url is None:
        return None
    path = url.path()
    return os.path.realpath(str(path)) if path else None


def _inside(path: str, roots: tuple[Path, ...]) -> bool:
    p = Path(os.path.realpath(path))
    return any(p == r or r in p.parents for r in roots)


def _check_app(app_path: Optional[str], what: str, writable: tuple[Path, ...]) -> str:
    """Refuse a denied app or one the sandboxed child could have written."""
    if not app_path:
        raise OpenRefused(f"{what}: application not found")
    if _bundle_id(app_path).lower() in DENIED_OPEN_APPS:
        raise OpenRefused(f"{what}: {Path(app_path).name} is not allowed from a sandboxed tool")
    if _inside(app_path, writable):
        raise OpenRefused(f"{what}: {app_path} is in a folder the tool can write")
    return app_path


def _resolve_app_name(name: str, cwd: str) -> Optional[str]:
    if "/" in name:
        path = os.path.realpath(os.path.join(cwd, os.path.expanduser(name)))
        return path if os.path.exists(path) else None
    ws = _workspace()
    found = ws.fullPathForApplication_(name) if ws is not None else None
    return os.path.realpath(str(found)) if found else None


def _resolve_bundle_id(bid: str) -> Optional[str]:
    if bid.lower() in DENIED_OPEN_APPS:
        raise OpenRefused(f"-b {bid} is not allowed from a sandboxed tool")
    ws = _workspace()
    return _url_path(ws.URLForApplicationWithBundleIdentifier_(bid)) if ws is not None else None


def _default_handler(url_string: str) -> Optional[str]:
    ws = _workspace()
    if ws is None:
        return None
    from Foundation import NSURL  # type: ignore
    url = NSURL.URLWithString_(url_string)
    return _url_path(ws.URLForApplicationToOpenURL_(url)) if url is not None else None


def _check_path(path: str, *, explicit_app: bool, writable: tuple[Path, ...]) -> None:
    name = Path(path).name
    suffix = Path(path.rstrip("/")).suffix.lower()
    if suffix in DENIED_OPEN_SUFFIXES:
        raise OpenRefused(f"refusing to open {name}: {suffix} files can run code")
    try:
        st = os.stat(path)
    except OSError:
        return  # /usr/bin/open reports the missing file
    if stat.S_ISREG(st.st_mode) and st.st_mode & 0o111:
        raise OpenRefused(f"refusing to open {name}: it is executable")
    ws = _workspace()
    if not explicit_app and ws is not None:
        from Foundation import NSURL  # type: ignore
        handler = _url_path(ws.URLForApplicationToOpenURL_(NSURL.fileURLWithPath_(path)))
        if handler:
            _check_app(handler, f"opening {name}", writable)


def check_open_args(argv: list[str], cwd: str, writable: tuple[Path, ...]) -> list[str]:
    """Validate a sandboxed tool's ``open`` arguments; return the argv to run.

    Raises :class:`OpenRefused`. Paths are made absolute (the daemon's cwd is
    not the child's) and ``-a``/``-b`` become ``-a <checked app path>``.
    """
    flags: list[str] = []
    app: Optional[str] = None
    targets: list[tuple[str, bool]] = []  # (target, forced URL)
    i, url_next, rest = 0, False, False
    while i < len(argv):
        a = str(argv[i])
        i += 1
        if rest or not a.startswith("-") or a == "-":
            targets.append((a, url_next))
            url_next = False
            continue
        if a == "--":
            rest = True
            continue
        if a.startswith("--") or len(a) != 2:
            raise OpenRefused(f"open option {a} is not allowed from a sandboxed tool")
        flag = a[1]
        if flag in "ab":
            if i >= len(argv):
                raise OpenRefused(f"open {a} needs a value")
            value = str(argv[i])
            i += 1
            if app is not None:
                raise OpenRefused("only one -a/-b is allowed")
            path = _resolve_app_name(value, cwd) if flag == "a" else _resolve_bundle_id(value)
            app = _check_app(path, f"open {a} {value}", writable)
        elif flag == "u":
            url_next = True
        elif flag in _OPEN_PLAIN_FLAGS:
            flags.append(a)
        else:
            raise OpenRefused(f"open option {a} is not allowed from a sandboxed tool")
    out_targets: list[str] = []
    for target, forced_url in targets:
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", target)
        is_url = forced_url or (m is not None and not os.path.exists(os.path.join(cwd, target)))
        if is_url:
            scheme = (m.group(1) if m else urlparse(target).scheme).lower()
            if scheme == "file":
                path = os.path.realpath(unquote(urlparse(target).path))
                _check_path(path, explicit_app=app is not None, writable=writable)
                out_targets.append(path)
                continue
            if scheme in DENIED_URL_SCHEMES:
                raise OpenRefused(f"refusing to open a {scheme}: URL from a sandboxed tool")
            if app is None:
                handler = _default_handler(target)
                if handler:
                    _check_app(handler, f"opening a {scheme}: URL", writable)
            out_targets.append(target)
        else:
            path = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
            _check_path(path, explicit_app=app is not None, writable=writable)
            out_targets.append(path)
    # Targets are absolute paths or scheme: URLs, so none can pass as a flag.
    return flags + (["-a", app] if app else []) + out_targets


def _run_open(argv: list[str]) -> dict:
    """Run ``/usr/bin/open`` unsandboxed (tests replace this)."""
    try:
        done = subprocess.run(
            ["/usr/bin/open", *argv], capture_output=True, text=True,
            timeout=OPEN_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"rc": 1, "stdout": "", "stderr": "open: timed out\n"}
    return {"rc": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _is_running(app_path: str) -> bool:
    bid = _bundle_id(app_path)
    if not bid:
        return False
    try:
        from AppKit import NSRunningApplication  # type: ignore
    except ImportError:  # pragma: no cover
        return False
    return len(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid) or []) > 0


def _launch_apps(names: list[str], ids: list[str], cwd: str, writable: tuple[Path, ...]) -> list[str]:
    """Launch (in the background) the allowed apps an osascript call targets."""
    launched: list[str] = []
    for kind, value in [("a", n) for n in names[:8]] + [("b", b) for b in ids[:8]]:
        try:
            path = _resolve_app_name(str(value), cwd) if kind == "a" else _resolve_bundle_id(str(value))
            path = _check_app(path, f"launch {value}", writable)
        except OpenRefused as exc:
            logger.info("sandbox launch refused: %s", exc)
            continue
        if not _is_running(path):
            _run_open(["-g", "-a", path])
            launched.append(path)
    return launched


class _OpenProxy:
    """Per-call unix socket the child's ``open``/``osascript`` shims talk to."""

    def __init__(self, home: Path, work: Path) -> None:
        self.writable = (home, work)
        # AF_UNIX paths are capped at 104 bytes: keep it short, under /tmp.
        self.dir = Path(os.path.realpath(tempfile.mkdtemp(prefix="tsb-", dir="/tmp")))
        self.path = self.dir / "p.sock"
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(8)
        threading.Thread(target=self._serve, daemon=True, name="sandbox-open-proxy").start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            data = b""
            while not data.endswith(b"\n") and len(data) < 1 << 20:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            conn.settimeout(OPEN_TIMEOUT_SECONDS + 10)
            conn.sendall(json.dumps(self.answer(json.loads(data or b"{}"))).encode("utf-8") + b"\n")
        except Exception as exc:  # noqa: BLE001 - never kill the proxy thread
            logger.debug("sandbox open proxy: %s", exc)
        finally:
            conn.close()

    def answer(self, req: dict) -> dict:
        cwd = str(req.get("cwd") or self.writable[0])
        if req.get("cmd") == "open":
            try:
                argv = check_open_args([str(a) for a in req.get("argv") or []], cwd, self.writable)
            except OpenRefused as exc:
                logger.info("sandboxed open refused: %s", exc)
                return {"rc": 1, "stdout": "", "stderr": f"open: refused by the tool sandbox: {exc}\n"}
            return _run_open(argv)
        if req.get("cmd") == "launch":
            names = [str(a) for a in req.get("apps") or []]
            ids = [str(a) for a in req.get("ids") or []]
            return {"rc": 0, "launched": _launch_apps(names, ids, cwd, self.writable)}
        return {"rc": 1, "stderr": "unknown request\n"}

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


# ---- child ----------------------------------------------------------------

# argv: daemon_root, module_path, tool_name, mode ("describe" | "call"),
# linger_seconds. stdin: {"args": [...], "kwargs": {...}, "exclude": [...]}.
_CHILD = r'''
import json, os, sys, threading, time, traceback, types
root, path, name, mode, linger = sys.argv[1:6]
_res = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)  # the tool's own prints go to stderr, not the result channel
def _emit(obj):
    _res.write("__TOOL_SANDBOX__" + json.dumps(obj, default=str) + "\n")
    _res.flush()
try:
    payload = json.loads(sys.stdin.read() or "{}")
    sys.path.insert(0, root)
    try:
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False  # never load the daemon's .env
    except ImportError:
        pass
    for _pkg, _sub in (("tools", "tools"), ("tools.generated", "tools/generated"), ("ai", "ai")):
        _m = types.ModuleType(_pkg)
        _m.__path__ = [os.path.join(root, _sub)]
        sys.modules[_pkg] = _m
    import importlib.util
    from smolagents import Tool
    _mod_name = "tools.generated." + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(_mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_mod_name] = mod
    spec.loader.exec_module(mod)
    cands = [a for a in vars(mod).values() if isinstance(a, Tool)]
    obj = next((a for a in cands if a.name == name), None)
    if obj is None and mode == "describe":
        exclude = set(payload.get("exclude") or [])
        obj = next((a for a in cands if a.name not in exclude), None)
    if mode == "describe":
        if obj is None:
            _emit({"ok": True, "found": False})
        else:
            _emit({"ok": True, "found": True, "meta": {
                "name": obj.name, "description": obj.description,
                "inputs": obj.inputs, "output_type": obj.output_type,
                "output_schema": getattr(obj, "output_schema", None),
            }})
        os._exit(0)
    if obj is None:
        raise LookupError("tool %s not found in %s" % (name, path))
    result = obj.forward(*(payload.get("args") or []), **(payload.get("kwargs") or {}))
    _emit({"ok": True, "result": result})
except BaseException as exc:
    _emit({"ok": False,
           "error": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
           "traceback": traceback.format_exc()})
    os._exit(0)
deadline = time.time() + float(linger)
for t in threading.enumerate():
    if t is not threading.main_thread():
        t.join(timeout=max(0.0, deadline - time.time()))
os._exit(0)
'''


def _child_env(home: Path, work: Path) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "TMPDIR": str(work / "tmp") + "/",
        "PATH": f"{work / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HF_HUB_OFFLINE": "1",
        "AGENT_DISABLE_LISTENERS": "1",
    }
    for key in ("USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _spawn(
    mode: str, path: Path, tool_name: str, payload: dict, *,
    network: bool, timeout: float, linger: float = 0.0,
    home: Optional[Path] = None,
) -> dict:
    """Run the child once; return its result object or raise ToolSandboxError."""
    home = Path(os.path.realpath(home or sandbox_home()))
    work = Path(os.path.realpath(tempfile.mkdtemp(prefix="tool-sandbox-")))
    (work / "tmp").mkdir()
    python = sys.executable
    proxy = _OpenProxy(home, work)
    _write_shims(work / "bin", proxy.path, python)
    profile = work / "profile.sb"
    profile.write_text(build_profile(
        home=home, work=work, python=python, network=network,
        extra_read_denied=_extra_read_denied(), proxy_socket=proxy.path,
    ), encoding="utf-8")
    stderr_path = work / "stderr.txt"
    stderr_file = open(stderr_path, "wb")
    try:
        proc = subprocess.Popen(
            [_SANDBOX_EXEC, "-f", str(profile), python, "-c", _CHILD,
             str(_DAEMON_ROOT), str(Path(path).resolve()), tool_name, mode, str(linger)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file,
            cwd=str(home), env=_child_env(home, work), start_new_session=True,
        )
    except BaseException:
        stderr_file.close()
        proxy.close()
        shutil.rmtree(work, ignore_errors=True)
        raise
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def read() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            text = raw.decode("utf-8", "replace")
            if text.startswith(_MARKER):
                lines.put(text[len(_MARKER):])
        lines.put(None)

    threading.Thread(target=read, daemon=True, name=f"sandbox-{tool_name}").start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, default=str).encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        line = lines.get(timeout=timeout)
    except queue.Empty:
        _kill(proc)
        _cleanup(proc, stderr_file, work, 5.0, proxy)
        raise ToolSandboxTimeout(
            f"TimeoutError: {tool_name} did not finish within {timeout:g}s (sandboxed)"
        )
    if line is None:
        proc.wait(timeout=5)
        stderr_file.close()
        tail = stderr_path.read_text("utf-8", "replace").strip().splitlines()
        proxy.close()
        shutil.rmtree(work, ignore_errors=True)
        raise ToolSandboxError(
            f"SandboxError: {tool_name} exited without a result "
            f"({tail[-1] if tail else f'exit code {proc.returncode}'})"
        )
    # The child may linger for background threads; reap it off-thread.
    threading.Thread(
        target=_cleanup, args=(proc, stderr_file, work, linger + 10.0, proxy),
        daemon=True, name=f"sandbox-reap-{tool_name}",
    ).start()
    out = json.loads(line)
    if not out.get("ok"):
        raise ToolSandboxError(str(out.get("error") or "error"), out.get("traceback") or "")
    return out


def _cleanup(
    proc: subprocess.Popen, stderr_file: Any, work: Path, wait: float,
    proxy: Optional["_OpenProxy"] = None,
) -> None:
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    try:
        stderr_file.close()
        text = (work / "stderr.txt").read_text("utf-8", "replace").strip()
        if text:
            logger.debug("sandboxed tool stderr: %s", text[-2000:])
    except OSError:
        pass
    if proxy is not None:
        proxy.close()
    shutil.rmtree(work, ignore_errors=True)


# ---- self-test ------------------------------------------------------------

_SANDBOX_OK: Optional[bool] = None
_SANDBOX_PROBLEM: str = ""
_SELF_TEST_LOCK = threading.Lock()

_SELF_TEST_TOOL = (
    "from smolagents import tool\n\n\n@tool\ndef sandbox_probe() -> dict:\n"
    '    """Self-test.\n\n    Returns:\n        dict.\n    """\n'
    "    import os\n"
    "    open(os.path.expanduser('~/written.txt'), 'w').write('x')\n"
    "    try:\n"
    "        os.makedirs(os.path.expanduser('~/Library'), exist_ok=True)\n"
    "        open(os.path.expanduser('~/Library/escape.txt'), 'w').write('x')\n"
    "        return {'status': 'escaped'}\n"
    "    except OSError:\n        return {'status': 'ok'}\n"
)


def sandbox_available() -> bool:
    """Check once that the sandbox runs a tool and blocks a ~/Library write."""
    global _SANDBOX_OK, _SANDBOX_PROBLEM
    with _SELF_TEST_LOCK:
        if _SANDBOX_OK is not None:
            return _SANDBOX_OK
        ok, problem = False, ""
        if sys.platform != "darwin" or not Path(_SANDBOX_EXEC).exists():
            problem = f"{_SANDBOX_EXEC} is not available"
        else:
            with tempfile.TemporaryDirectory(prefix="tool-sandbox-test-") as tmp:
                home = Path(os.path.realpath(tmp))
                (home / "Library").mkdir()
                probe = home / "sandbox_probe.py"
                probe.write_text(_SELF_TEST_TOOL, encoding="utf-8")
                try:
                    out = _spawn("call", probe, "sandbox_probe", {}, network=False,
                                 timeout=DESCRIBE_TIMEOUT_SECONDS, home=home)
                    status = (out.get("result") or {}).get("status")
                    ok = status == "ok" and (home / "written.txt").exists() \
                        and not (home / "Library" / "escape.txt").exists()
                    if not ok:
                        problem = f"self-test returned {out.get('result')!r}"
                except Exception as exc:
                    problem = f"self-test failed: {exc}"
        if not ok:
            logger.warning("generated-tool sandbox unavailable: %s", problem)
        _SANDBOX_OK, _SANDBOX_PROBLEM = ok, problem
        return ok


def sandbox_problem() -> str:
    return _SANDBOX_PROBLEM


def reset_sandbox_cache() -> None:
    """Forget the self-test result (tests)."""
    global _SANDBOX_OK, _SANDBOX_PROBLEM
    with _SELF_TEST_LOCK:
        _SANDBOX_OK, _SANDBOX_PROBLEM = None, ""


# ---- proxy tool -----------------------------------------------------------

try:
    from smolagents import Tool as _Tool  # type: ignore
except ImportError:  # pragma: no cover - smolagents is a hard dependency
    _Tool = object  # type: ignore[assignment,misc]


class SandboxedTool(_Tool):  # type: ignore[misc,valid-type]
    """Daemon-side proxy: each call runs the real tool in a sandboxed child."""

    skip_forward_signature_validation = True
    sandboxed = True

    def __init__(self, meta: dict, source_path: Path, network: Optional[str]) -> None:
        self.name = meta["name"]
        self.description = meta["description"]
        self.inputs = meta["inputs"]
        self.output_type = meta["output_type"]
        if meta.get("output_schema") is not None:
            self.output_schema = meta["output_schema"]
        self.source_path = Path(source_path)
        self.network_allowed = network is not None
        self.sandbox_note = (
            f"runs under sandbox-exec; network allowed ({network})"
            if network else "runs under sandbox-exec; no network"
        )
        super().__init__()

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        if self.network_allowed:
            # #82: the tool may send its arguments anywhere; log the run.
            from ai import egress_log

            egress_log.record_tool_run(
                egress_log.KIND_SANDBOXED_TOOL, self.name,
                {"args": list(args), "kwargs": kwargs}, note=self.sandbox_note,
            )
        out = _spawn(
            "call", self.source_path, self.name,
            {"args": list(args), "kwargs": kwargs},
            network=self.network_allowed, timeout=call_timeout(),
            linger=linger_seconds(),
        )
        return out.get("result")


def load_sandboxed_tool(
    name: str, path: Path, exclude: frozenset[str] = frozenset(),
) -> Optional[SandboxedTool]:
    """Describe ``path`` in a sandboxed child and return its proxy.

    ``None`` when the module defines no Tool; raises :class:`ToolSandboxError`
    (carrying the child's error and traceback) when importing it fails.
    """
    source = Path(path).read_text(encoding="utf-8")
    network = network_reason(source)
    out = _spawn(
        "describe", Path(path), name, {"exclude": sorted(exclude)},
        network=network is not None, timeout=DESCRIBE_TIMEOUT_SECONDS,
    )
    if not out.get("found"):
        return None
    return SandboxedTool(out["meta"], Path(path), network)


__all__ = [
    "DENIED_APPLE_EVENT_TARGETS", "NETWORK_MODULES", "OpenRefused", "SandboxedTool",
    "check_open_args", "ToolSandboxError", "ToolSandboxTimeout",
    "build_profile", "load_sandboxed_tool", "network_reason", "reset_sandbox_cache",
    "sandbox_available", "sandbox_enabled", "sandbox_problem",
]
