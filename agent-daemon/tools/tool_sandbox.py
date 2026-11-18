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
  and ``.env``: a write there would run unsandboxed on the next start), and
  for the app-bundled daemon (#96) its ``AGENT_DATA_DIR`` and moved
  generated-tools dir;
* reads: allowed, except ``~/.ssh``, ``~/.bot-inator`` (API token, memory)
  and the daemon's ``.env`` (cloud API keys; ``<AGENT_DATA_DIR>/.env`` too).
  These two file rules come from :func:`closed_paths`; the draft dry run
  (``tools.draft_dry_run``) closes the same paths (#114), so a draft that
  keeps a file in ``~/.bot-inator`` fails before review, not after install;
* deletes (#123, user decision B): no ``file-write-unlink`` under ``$HOME``
  outside the tool's own folders (its temp dir and ``~/Documents/bot-inator``,
  :func:`unlink_rules`). That operation also covers renames, so the child's
  moves under ``$HOME`` and :func:`move_to_trash` go to the daemon over the
  relay socket (:class:`FileRelay`): a move never replaces a file, and the
  only delete is a move into ``~/.Trash``. The dry run has the same rule;
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
* MCP relay (todo #112): ``from tools.mcp import call_mcp`` sends
  ``{"cmd": "mcp"}`` on the same socket. The daemon runs the MCP tool only
  if its name is a string literal in a ``call_mcp(...)`` call in the
  reviewed source (:func:`mcp_tool_names`, read at load) and it is enabled
  now; the gate (card or "runs without asking") is the orchestrator's
  (``Orchestrator.relay_mcp_call`` -> ``McpManager.relay_call``). The child
  never sees the server config, the token or a server process. Time spent
  in a relayed call (an approval card can wait minutes) does not count
  toward the call timeout.

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
import errno
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
import time
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
# #112: at most this much relayed-MCP time (cards waiting for the user, the
# MCP calls themselves) is added to one call's timeout.
MAX_RELAY_PAUSE_SECONDS = 1800.0
# Under $HOME: writable except these (relative to $HOME).
_WRITE_DENIED_IN_HOME: tuple[str, ...] = ("Library", ".ssh", ".bot-inator")
_SHELL_STARTUP_FILES: tuple[str, ...] = (
    ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".bashrc", ".bash_profile",
    ".profile",
)
_READ_DENIED_IN_HOME: tuple[str, ...] = (".ssh", ".bot-inator")
# #114: where the drafting prompt and :func:`protected_path_note` send a
# tool's own files (state, lists, logs): writable under the profile, and a
# folder the user can see. Advice, not a rule: a tool may write elsewhere
# under $HOME.
TOOL_FILES_DIR = "~/Documents/bot-inator"


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


# ---- MCP relay names (#112) -------------------------------------------------

MCP_CALL_FUNCTION = "call_mcp"


def mcp_tool_names(source: str) -> frozenset[str]:
    """MCP tool names the module passes LITERALLY to ``call_mcp(...)``.

    Only a string constant as the first argument (or ``name=``) counts: a
    name built at run time (concatenation, f-string, a variable) is never
    in this set, so the relay refuses it. ``from tools.mcp import call_mcp
    as x`` aliases and ``<module>.call_mcp(...)`` are recognised.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    callers = {MCP_CALL_FUNCTION}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.mcp":
            callers |= {a.asname for a in node.names if a.name == MCP_CALL_FUNCTION and a.asname}
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            (isinstance(func, ast.Name) and func.id in callers)
            or (isinstance(func, ast.Attribute) and func.attr == MCP_CALL_FUNCTION)
        ):
            continue
        arg = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "name"), None
        )
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
            names.add(arg.value)
    return frozenset(names)


class McpRelayRefused(RuntimeError):
    """The relay did not run the MCP tool (the reason is the message)."""


def _default_mcp_relay(tool_name: str, allowed: frozenset[str], name: str, arguments: dict) -> str:
    """Hand a relayed call to the running orchestrator (raises when refused)."""
    from tools import meta_tool_generator

    orch = getattr(meta_tool_generator, "_ORCHESTRATOR", None)
    relay = getattr(orch, "relay_mcp_call", None)
    if relay is None:
        raise McpRelayRefused("no MCP tools are available (the daemon has no MCP client)")
    return relay(tool_name, name, arguments, allowed=allowed)


# ---- profile --------------------------------------------------------------


def _q(path: Any) -> str:
    # ensure_ascii=False (#114): sandbox-exec does not decode ``\uXXXX``, so
    # an escaped non-ASCII path named a folder that does not exist and its
    # rule did nothing (a repo under ``~/Développement``: ``.env`` readable).
    return json.dumps(str(path), ensure_ascii=False)


def _protected_repo_dir(home: Path) -> Path:
    """The repo root, or just the daemon dir if the repo root contains $HOME."""
    repo = Path(os.path.realpath(_DAEMON_ROOT.parent))
    if home == repo or repo in home.parents:
        return Path(os.path.realpath(_DAEMON_ROOT))
    return repo


def closed_paths(
    home: Path, *, extra_write_denied: tuple[Path, ...] = (),
    extra_read_denied: tuple[Path, ...] = (),
) -> dict[str, list[Path]]:
    """What the profile closes on disk for ``home``.

    ``write`` and ``read`` are folders (everything under them);
    ``write_files`` and ``read_files`` are single files. This is the one
    list of what a generated tool may not touch: ``home``'s protected
    folders and shell start-up files, the bot-inator tree, and the secrets.
    :func:`build_profile` turns it into the installed tool's two ``deny``
    lines (:func:`deny_rules`); the draft dry run (``tools.draft_dry_run``,
    #114) closes the same paths, so a draft is refused before review where
    the installed tool would be.
    """
    daemon = Path(os.path.realpath(_DAEMON_ROOT))
    return {
        "write": [
            *(home / d for d in _WRITE_DENIED_IN_HOME),
            _protected_repo_dir(home),
            daemon / "tools" / "generated",
            *_moved_write_denied(),
            *extra_write_denied,
        ],
        "write_files": [home / f for f in _SHELL_STARTUP_FILES],
        "read": [home / d for d in _READ_DENIED_IN_HOME],
        "read_files": [daemon / ".env", *_moved_read_denied(), *extra_read_denied],
    }


def deny_rules(
    closed: dict[str, list[Path]], *, writes: bool = True, reads: bool = True,
) -> list[str]:
    """``closed`` as profile lines: ``deny file-write*``, then ``deny
    file-read*``. They go after any ``allow file-write*``. A line with no
    path is left out (a bare ``deny file-read*`` would close every file).
    """
    rules: list[str] = []
    for kind, wanted in (("write", writes), ("read", reads)):
        folders = [f"(subpath {_q(p)})" for p in closed[kind]]
        files = [f"(literal {_q(p)})" for p in closed[kind + "_files"]]
        if wanted and (folders or files):
            rules.append(
                f"(deny file-{kind}* " + " ".join(folders) + " " + " ".join(files) + ")"
            )
    return rules


def moved_home(
    closed: dict[str, list[Path]], old: Path, new: Path,
) -> dict[str, list[Path]]:
    """``closed`` as it lies under another home: each path inside ``old`` at
    the same place inside ``new``; paths outside ``old`` are left out.

    The dry run's ``~`` is a fake home. What the profile closes under the
    real one (``~/Library``, a repo at ``~/dev/bot-inator``) has to be
    closed at the same ``~``-relative place there, or a draft that writes
    ``~/dev/bot-inator/notes.txt`` passes the dry run and fails installed.
    """
    def move(path: Path) -> Optional[Path]:
        try:
            rel = Path(path).relative_to(old)
        except ValueError:
            return None
        return new / rel if rel.parts else None

    return {
        key: [p for p in map(move, paths) if p is not None]
        for key, paths in closed.items()
    }


def build_profile(
    *, home: Path, work: Path, python: str, network: bool,
    extra_read_denied: tuple[Path, ...] = (),
    proxy_socket: Optional[Path] = None,
) -> str:
    base = os.path.realpath(sys.base_prefix)
    shims = work / "bin"
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (subpath {_q(home)}) (subpath {_q(work)}) '
        '(literal "/dev/null") (literal "/dev/tty"))',
        # #123: no unlink / rmdir / rename-away under $HOME outside the
        # tool's own folders; moves and the Trash go through the relay.
        *unlink_rules(home, own_folders(home, work / "tmp")),
        *deny_rules(closed_paths(
            home, extra_write_denied=(shims,), extra_read_denied=extra_read_denied,
        )),
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


def _moved_write_denied() -> list[Path]:
    """#96: the app-bundled daemon's writable home (``AGENT_DATA_DIR``) and a
    moved generated-tools dir. Same reason as ``tools/generated`` in the
    tree: a write there would run unsandboxed on the next start."""
    import config

    paths = [config.default_generated_tools_dir()]
    if config.data_dir() is not None:
        paths.append(config.data_dir())
    return [Path(os.path.realpath(p)) for p in paths]


def _moved_read_denied() -> list[Path]:
    """#96: the data dir's ``.env`` (cloud API keys), like the tree's."""
    import config

    if config.data_dir() is None:
        return []
    return [Path(os.path.realpath(config.dotenv_path()))]


def _extra_read_denied() -> tuple[Path, ...]:
    """The API token file when ``AGENT_TOKEN_PATH`` moves it out of ~/.bot-inator."""
    override = os.environ.get("AGENT_TOKEN_PATH", "").strip()
    if not override:
        return ()
    return (Path(os.path.realpath(Path(override).expanduser())),)


# ---- a refusal, in words (#114) ----------------------------------------------

_REFUSED_RE = re.compile(
    r"operation not permitted|permission denied|\[Errno (?:1|13)\]|PermissionError",
    re.I,
)
# Any sign that a file operation failed. A refused ``stat`` in a folder that
# is closed for reading does not look like a permission error: the file
# "is missing", or ``mkdir(exist_ok=True)`` raises ``FileExistsError``.
_OS_ERROR_RE = re.compile(
    r"\[Errno \d+\]|operation not permitted|permission denied|file exists|"
    r"no such file|unable to open database|"
    r"(?:Permission|FileExists|FileNotFound|NotADirectory|OS)Error",
    re.I,
)
# A name starts a path component, and ends one (``~/.bot-inator-tools`` and
# ``~/Library2`` are other folders; ``/Volumes/x/Users/me`` is not ``/Users/me``).
_PATH_START = r"(?<![\w.\-])"
_HOME_START = r"(?<![\w./\-])"
_PATH_END = r"""(?=$|[/'"`\s\\:;,)\]}])"""


def _closed_names(home: Path) -> dict[str, list[str]]:
    """:func:`closed_paths` for ``home`` as ``~``-relative names."""
    moved = moved_home(closed_paths(home), home, Path("~"))
    return {key: [p.as_posix()[2:] for p in paths] for key, paths in moved.items()}


def protected_path_note(text: str, homes: Any, *, raised: bool) -> str:
    """A sentence for the drafting model when ``text`` shows a generated tool
    failing on a path the profile closes; "" otherwise.

    ``text`` comes from a run under the profile (the draft dry run passes
    the draft's). ``raised=True``: it is the message of an error the run
    raised, with the failing source line. ``raised=False``: it is a value
    the tool returned, which counts only when it carries an OS error: a
    tool may name a closed folder in what it returns without having touched
    it (a listing of ``~``, or the very error this note asks for).

    ``~/.ssh`` and ``~/.bot-inator`` are closed for reading too, so their
    bare names are enough (``".bot-inator"`` in the failing line). The
    other closed paths (``~/Library``, shell start-up files, the bot-inator
    folder) are closed for writing: they count only under one of ``homes``
    or ``~``, next to a permission error.
    """
    names = _closed_names(sandbox_home())
    failed = raised or _OS_ERROR_RE.search(text) is not None

    def found(pattern: str) -> bool:
        # re.I: the Mac's disk is case-insensitive (~/library is ~/Library).
        return re.search(pattern + _PATH_END, text, re.I) is not None

    roots = [re.escape(str(h).rstrip("/")) for h in homes if str(h).strip("/")]
    under_home = _HOME_START + "(?:" + "|".join([*roots, "~"]) + ")/"
    if failed:
        for name in names["read"]:
            bare = _PATH_START if name.startswith(".") else under_home
            if found(bare + re.escape(name)):
                return _refusal_sentence(f"read or write ~/{name}", f"~/{name}")
        for name in names["read_files"]:
            if found(under_home + re.escape(name)):
                return _refusal_sentence(f"read ~/{name}", f"~/{name}")
    if _REFUSED_RE.search(text) is not None:
        for name in names["write"]:
            if found(under_home + re.escape(name)):
                return _refusal_sentence(f"write under ~/{name}", f"~/{name}")
        for name in names["write_files"]:
            if found(under_home + re.escape(name)):
                return _refusal_sentence(f"write ~/{name}", f"~/{name}")
        if raised and _DELETE_CALL_RE.search(text):
            return TRASH_SENTENCE
    return ""


# #123: a refused delete, as the failing source line shows it.
_DELETE_CALL_RE = re.compile(
    r"\b(?:os\.(?:remove|unlink|rmdir|removedirs)|shutil\.rmtree|rmtree|removedirs)\s*\(|"
    r"\.(?:unlink|rmdir)\s*\(|refused by the tool sandbox"
)
TRASH_SENTENCE = (
    "The tool sandbox does not let a generated tool delete files or folders "
    "(os.remove, os.unlink, os.rmdir, shutil.rmtree, Path.unlink) outside its "
    f"own folders ({TOOL_FILES_DIR}/ and the temp dir), and a move may not "
    "replace an existing file. To delete, move the items to the Trash: "
    "`from tools.tool_sandbox import move_to_trash` and call "
    "`move_to_trash(path)` (or a list of paths); it returns where each one went."
)


def _refusal_sentence(cannot: str, path: str) -> str:
    return (
        f"The tool sandbox does not let a generated tool {cannot}, so this "
        "fails after install too. Keep files the tool creates for itself "
        f"under {TOOL_FILES_DIR}/ instead, even if the spec names {path}; if "
        f'the task needs {path} itself, return a {{"status": "error"}} dict '
        "that says a generated tool cannot use it."
    )


# ---- deletes go to the Trash (#123) -------------------------------------------
#
# User decision B (2026-11-18): a generated tool may move things to the Trash
# (recoverable), never unlink outside its own folders. The kernel rule is
# ``file-write-unlink``, and on macOS that one operation covers unlink(2),
# rmdir(2) AND both names of a rename(2): the source name, and a destination
# name that already exists (tested with ``sandbox-exec -p``, see
# tests/test_trash_only_tools.py). So "deny unlink under $HOME" also denies
# every in-sandbox move, the Trash move included: a rename into ~/.Trash is
# an unlink of the source name. A profile rule cannot tell a move into the
# Trash from any other move.
#
# Hence the relay: under $HOME, outside the tool's own folders, the child's
# ``os.rename`` / ``os.replace`` / ``shutil.move`` and :func:`move_to_trash`
# ask the daemon over the per-call socket (the one the ``open`` shim and
# ``call_mcp`` use), and :class:`FileRelay` does the rename after checking
# it: both ends under $HOME, none in a folder the profile closes, nothing
# replaced (a rename onto an existing name deletes it), nothing moved into
# the tool's own folders (where it could then be unlinked). Moves keep
# working; deletes can only go to the Trash.

TRASH_DIRNAME = ".Trash"
# Where the child finds its own folders (``os.pathsep``-joined).
OWN_DIRS_ENV = "AGENT_TOOL_OWN_DIRS"
RELAY_SOCKET_ENV = "AGENT_TOOL_RELAY_SOCKET"
_RELAY_TIMEOUT_SECONDS = 60.0


class TrashRefused(PermissionError):
    """The relay did not move the item (the reason is the message)."""


def own_folders(home: Path, tmp: Path) -> tuple[Path, ...]:
    """Where a generated tool may unlink: its temp dir and :data:`TOOL_FILES_DIR`."""
    return (Path(tmp), Path(home) / TOOL_FILES_DIR[2:])


def unlink_rules(home: Path, own: tuple[Path, ...]) -> list[str]:
    """Profile lines: no unlink / rmdir / rename-away under ``home``, except
    in ``own``. They go after the ``allow file-write*`` line (the last
    matching rule wins) and before :func:`deny_rules` (closed stays closed)."""
    return [
        f"(deny file-write-unlink (subpath {_q(home)}))",
        "(allow file-write-unlink " + " ".join(f"(subpath {_q(p)})" for p in own) + ")",
    ]


def _relay_ask(sock: str, req: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(_RELAY_TIMEOUT_SECONDS)
    try:
        s.connect(sock)
        s.sendall(json.dumps(req).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode("utf-8") or "{}")


def _flat_paths(paths: tuple[Any, ...]) -> list[str]:
    flat: list[Any] = []
    for p in paths:
        flat.extend(p) if isinstance(p, (list, tuple, set, frozenset)) else flat.append(p)
    return [os.path.abspath(os.path.expanduser(os.fspath(p))) for p in flat]


def move_to_trash(*paths: Any) -> list[str]:
    """Move files or folders to the Trash; return where each one went.

    The one way a generated tool deletes (#123): ``from tools.tool_sandbox
    import move_to_trash``. Accepts paths or lists of paths. Everything in
    the Trash can be dragged back out. Raises :class:`TrashRefused` (a
    ``PermissionError``) naming the first path that was refused; items
    before it are already in the Trash.
    """
    flat = _flat_paths(paths)
    if not flat:
        return []
    sock = os.environ.get(RELAY_SOCKET_ENV, "")
    req = {"cmd": "trash", "paths": flat}
    if sock:
        resp = _relay_ask(sock, req)
    else:
        # In the daemon (a trusted tool, #104, or AGENT_TOOL_SANDBOX=0): the
        # same checks, against the real home.
        resp = FileRelay.for_home(sandbox_home()).answer(req)
    if not resp.get("ok"):
        message = str(resp.get("error") or "refused by the tool sandbox")
        if isinstance(resp.get("errno"), int) and resp["errno"] != errno.EPERM:
            raise OSError(resp["errno"], message)
        raise TrashRefused(message)
    return [str(p) for p in resp.get("trashed") or []]


def _fd_path(fd: int) -> str:
    import fcntl

    raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
    return raw.rstrip(b"\0").decode("utf-8", "surrogateescape")


def _within(path: str, root: str) -> bool:
    """``path`` is ``root`` or inside it (the Mac's disk ignores case)."""
    p, r = path.casefold().rstrip("/"), root.casefold().rstrip("/")
    return p == r or p.startswith(r + "/")


class _Pinned:
    """A path's parent folder held open (so a symlink swapped in after the
    check cannot redirect the rename) plus its last name."""

    def __init__(self, path: str) -> None:
        path = os.path.abspath(path)
        self.name = os.path.basename(path.rstrip("/"))
        if self.name in ("", ".", ".."):
            raise TrashRefused(f"{path}: not a file or folder name")
        parent = os.path.realpath(os.path.dirname(path.rstrip("/")))
        self.fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.dir = _fd_path(self.fd)
        except OSError:
            os.close(self.fd)
            raise
        self.full = os.path.join(self.dir, self.name)

    def exists(self) -> bool:
        try:
            os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def close(self) -> None:
        os.close(self.fd)


class FileRelay:
    """The daemon side of the child's moves and :func:`move_to_trash` (#123).

    ``home`` is the home the profile protects (a fake one in the dry run),
    ``closed`` what it closes there (:func:`closed_paths`), ``own`` the
    tool's own folders (:func:`own_folders`), ``trash_dir`` where deletes
    go (``home/.Trash``).
    """

    def __init__(
        self, home: Path, closed: dict[str, list[Path]], own: tuple[Path, ...],
        trash_dir: Optional[Path] = None,
    ) -> None:
        self.home = os.path.realpath(home)
        self.closed = [os.path.realpath(p) for k in ("write", "write_files") for p in closed[k]]
        self.own = [os.path.realpath(p) for p in own]
        self.trash_dir = os.path.realpath(trash_dir or Path(self.home) / TRASH_DIRNAME)

    @classmethod
    def for_home(cls, home: Path, tmp: Optional[Path] = None) -> "FileRelay":
        home = Path(os.path.realpath(home))
        tmp = Path(tmp) if tmp is not None else Path(os.path.realpath(tempfile.gettempdir()))
        closed = closed_paths(home)
        return cls(home, closed, own_folders(home, tmp))

    # -- checks --

    def _check(self, p: _Pinned, what: str) -> None:
        if not _within(p.full, self.home) or _within(self.home, p.full):
            raise TrashRefused(f"{what} {p.full}: only files inside your home folder")
        for c in self.closed:
            if _within(p.full, c) or _within(c, p.full):
                raise TrashRefused(
                    f"{what} {p.full}: the tool sandbox protects {c}"
                )

    def _check_dest(self, p: _Pinned) -> None:
        self._check(p, "moving to")
        for o in self.own:
            if _within(p.full, o):
                raise TrashRefused(
                    f"moving to {p.full}: a user's file may not go into the tool's "
                    "own folder (it could be deleted there); use move_to_trash"
                )

    # -- requests --

    def answer(self, req: dict) -> dict:
        try:
            if req.get("cmd") == "trash":
                paths = req.get("paths")
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    return {"ok": False, "error": "bad trash request"}
                return {"ok": True, "trashed": [self.trash(p) for p in paths[:1000]]}
            if req.get("cmd") == "move":
                src, dst = req.get("src"), req.get("dst")
                if not isinstance(src, str) or not isinstance(dst, str):
                    return {"ok": False, "error": "bad move request"}
                return {"ok": True, "moved": self.move(src, dst)}
        except TrashRefused as exc:
            logger.info("sandboxed file relay refused: %s", exc)
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            # The child raises the same kind (FileNotFoundError for a
            # missing source, as a plain rename would).
            return {"ok": False, "errno": exc.errno,
                    "error": f"{exc.strerror or exc}: {exc.filename or ''}".strip()}
        return {"ok": False, "error": "unknown request"}

    def move(self, src: str, dst: str) -> str:
        """Rename ``src`` to ``dst`` (a free name); never replaces anything."""
        s = _Pinned(src)
        try:
            d = _Pinned(dst)
            try:
                self._check(s, "moving")
                self._check_dest(d)
                if not s.exists():
                    raise FileNotFoundError(2, "No such file or directory", s.full)
                if d.exists():
                    raise TrashRefused(
                        f"moving to {d.full}: that name is taken, and replacing it "
                        "would delete it; pick a free name, or move the old one "
                        "to the Trash first (move_to_trash)"
                    )
                os.rename(s.name, d.name, src_dir_fd=s.fd, dst_dir_fd=d.fd)
                return d.full
            finally:
                d.close()
        finally:
            s.close()

    def trash(self, path: str) -> str:
        """Move ``path`` into the Trash under a free name; return the new path."""
        s = _Pinned(path)
        try:
            self._check(s, "moving to the Trash")
            if _within(s.full, self.trash_dir) or _within(self.trash_dir, s.full):
                raise TrashRefused(f"{s.full} is already in the Trash (emptying it is not allowed)")
            if not s.exists():
                raise FileNotFoundError(2, "No such file or directory", s.full)
            os.makedirs(self.trash_dir, exist_ok=True)
            tfd = os.open(self.trash_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                name = self._free_name(tfd, s.name)
                try:
                    os.rename(s.name, name, src_dir_fd=s.fd, dst_dir_fd=tfd)
                except PermissionError:
                    return self._finder_trash(s)
                return os.path.join(_fd_path(tfd), name)
            finally:
                os.close(tfd)
        finally:
            s.close()

    @staticmethod
    def _free_name(dir_fd: int, name: str) -> str:
        stem, dot, ext = name.rpartition(".") if not name.startswith(".") else ("", "", name)
        if not dot:
            stem, ext = name, ""
        else:
            ext = "." + ext
        candidate, n = name, 1
        stamp = time.strftime("%H.%M.%S")
        while True:
            try:
                os.stat(candidate, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                return candidate
            n += 1
            candidate = f"{stem} {stamp}{'' if n == 2 else f' {n}'}{ext}"

    def _finder_trash(self, s: _Pinned) -> str:
        """The ~/.Trash rename was refused (macOS privacy): the system's own
        Trash call, for the real home only, after re-checking the folder."""
        if self.home != os.path.realpath(Path.home()):
            raise TrashRefused(f"moving {s.full} to the Trash was refused")
        try:
            from Foundation import NSURL, NSFileManager  # type: ignore
        except ImportError:  # pragma: no cover - pyobjc ships with the daemon
            raise TrashRefused(f"moving {s.full} to the Trash was refused")
        if _fd_path(s.fd) != s.dir:
            raise TrashRefused(f"{s.full}: its folder moved during the check")
        ok, url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(s.full), None, None,
        )
        if not ok:
            raise TrashRefused(f"moving {s.full} to the Trash failed: {err}")
        return str(url.path()) if url is not None else ""


def trashes(source: str) -> bool:
    """The reviewed source calls :func:`move_to_trash` (the card says so)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name == "move_to_trash":
                return True
    return False


def _path_like(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(("/", "~"))


def trash_card_note(*args: Any, **kwargs: Any) -> str:
    """The call card's line for a tool that calls :func:`move_to_trash`."""
    n = 0
    for value in [*args, *kwargs.values()]:
        if isinstance(value, (list, tuple)) and value and all(_path_like(v) for v in value):
            n += len(value)
        elif _path_like(value):
            n += 1
    if n:
        items = "item" if n == 1 else "items"
        return (f"Moves {n} {items} to the Trash (nothing is deleted for good; "
                "you can drag it back out)")
    return ("Moves the items it picks to the Trash (nothing is deleted for good; "
            "you can drag them back out)")


# The child's half (installed child and dry-run probe alike: one string, so
# both behave the same). Under ``home`` and outside the tool's own folders a
# rename is an unlink the profile denies, so these go over the relay.
FILE_RELAY_CHILD = r'''
def _bot_inator_file_relay():
    import errno, json, os, shutil, socket
    sock = os.environ.get("AGENT_TOOL_RELAY_SOCKET", "")
    own = [os.path.realpath(p) for p in os.environ.get("AGENT_TOOL_OWN_DIRS", "").split(os.pathsep) if p]
    if not sock:
        return
    def ask(req):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(60)
        try:
            s.connect(sock)
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        return json.loads(buf.decode("utf-8") or "{}")
    def native_ok(src):
        if not isinstance(src, (str, os.PathLike)):
            return True
        src = os.fspath(src)
        if isinstance(src, bytes):
            return True
        parent = os.path.realpath(os.path.dirname(os.path.abspath(src).rstrip("/")))
        low = parent.casefold()
        return any(low == o.casefold() or low.startswith(o.casefold() + "/") for o in own)
    def relay_move(src, dst):
        resp = ask({"cmd": "move", "src": os.path.abspath(os.fspath(src)),
                    "dst": os.path.abspath(os.fspath(dst))})
        if not resp.get("ok"):
            code = resp.get("errno")
            if isinstance(code, int) and code != errno.EPERM:
                raise OSError(code, str(resp.get("error")), os.fspath(src))
            raise PermissionError(errno.EPERM, "refused by the tool sandbox: " + str(resp.get("error")), os.fspath(src))
        return resp.get("moved")
    def wrap(native):
        def op(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            if src_dir_fd is not None or dst_dir_fd is not None or native_ok(src):
                return native(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            relay_move(src, dst)
        op.__name__ = native.__name__
        return op
    os.rename = wrap(os.rename)
    os.replace = wrap(os.replace)
    native_move = shutil.move
    def move(src, dst, copy_function=shutil.copy2):
        if native_ok(src):
            return native_move(src, dst, copy_function=copy_function)
        real = os.fspath(dst)
        if os.path.isdir(real):
            real = os.path.join(real, os.path.basename(os.fspath(src).rstrip("/")))
        relay_move(src, real)
        return real
    shutil.move = move
_bot_inator_file_relay()
del _bot_inator_file_relay
'''


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

    def __init__(
        self, home: Path, work: Path, mcp_relay: Any = None, *,
        files: Optional["FileRelay"] = None, file_only: bool = False,
    ) -> None:
        self.writable = (home, work)
        # #123: moves and move_to_trash; ``file_only`` (the dry run, whose
        # code is not approved yet) answers nothing else.
        self.files = files
        self.file_only = file_only
        # #112: ``mcp_relay(name, arguments) -> text`` (raises to refuse);
        # None = no MCP calls in this run (describe, self-test).
        self.mcp_relay = mcp_relay
        self._relay_lock = threading.Lock()
        self._relay_active = 0
        self._relay_started = 0.0
        self._relay_seconds = 0.0
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
        if req.get("cmd") in ("trash", "move"):
            if self.files is None:
                return {"ok": False, "error": "file moves are not available here"}
            return self.files.answer(req)
        if self.file_only:
            return {"rc": 1, "ok": False, "error": "not available in the dry run"}
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
        if req.get("cmd") == "mcp":
            return self._answer_mcp(req)
        return {"rc": 1, "stderr": "unknown request\n"}

    def _answer_mcp(self, req: dict) -> dict:
        name, arguments = req.get("name"), req.get("arguments")
        if self.mcp_relay is None:
            return {"ok": False, "error": "MCP calls are not available here"}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return {"ok": False, "error": "bad MCP request"}
        self._relay_enter()
        try:
            return {"ok": True, "text": str(self.mcp_relay(name, arguments))}
        except McpRelayRefused as exc:
            logger.info("sandboxed MCP call %s refused: %s", name, exc)
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - report, never crash the proxy
            logger.exception("sandboxed MCP call %s failed", name)
            return {"ok": False, "error": f"MCP relay failed: {type(exc).__name__}"}
        finally:
            self._relay_exit()

    def _relay_enter(self) -> None:
        with self._relay_lock:
            if self._relay_active == 0:
                self._relay_started = time.monotonic()
            self._relay_active += 1

    def _relay_exit(self) -> None:
        with self._relay_lock:
            self._relay_active -= 1
            if self._relay_active == 0:
                self._relay_seconds += time.monotonic() - self._relay_started

    def relay_time(self) -> float:
        """Seconds spent in relayed MCP calls so far (the running one too)."""
        with self._relay_lock:
            ongoing = time.monotonic() - self._relay_started if self._relay_active else 0.0
            return self._relay_seconds + ongoing

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


# ---- child ----------------------------------------------------------------

# argv: daemon_root, module_path, tool_name, mode ("describe" | "call"),
# linger_seconds. stdin: {"args": [...], "kwargs": {...}, "exclude": [...]}.
_CHILD = FILE_RELAY_CHILD + r'''
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
    # #96: the module's own dir too (the app-bundled daemon keeps generated
    # tools outside the tree), so one generated tool can import another.
    sys.modules["tools.generated"].__path__.insert(0, os.path.dirname(path))
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


def _child_env(home: Path, work: Path, relay_socket: Optional[Path] = None) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "TMPDIR": str(work / "tmp") + "/",
        "PATH": f"{work / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HF_HUB_OFFLINE": "1",
        "AGENT_DISABLE_LISTENERS": "1",
    }
    if relay_socket is not None:
        # #112: where ``tools.mcp.call_mcp`` asks (a path, not a secret);
        # #123: also the file relay's (moves, move_to_trash).
        env[RELAY_SOCKET_ENV] = str(relay_socket)
    env[OWN_DIRS_ENV] = os.pathsep.join(str(p) for p in own_folders(home, work / "tmp"))
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
    home: Optional[Path] = None, mcp_relay: Any = None,
) -> dict:
    """Run the child once; return its result object or raise ToolSandboxError."""
    home = Path(os.path.realpath(home or sandbox_home()))
    work = Path(os.path.realpath(tempfile.mkdtemp(prefix="tool-sandbox-")))
    (work / "tmp").mkdir()
    python = sys.executable
    files = FileRelay(home, closed_paths(home), own_folders(home, work / "tmp"))
    proxy = _OpenProxy(home, work, mcp_relay, files=files)
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
            cwd=str(home), env=_child_env(home, work, proxy.path), start_new_session=True,
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
        line = _wait_line(lines, timeout, proxy)
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


def _wait_line(lines: "queue.Queue[Optional[str]]", timeout: float, proxy: "_OpenProxy") -> Optional[str]:
    """The child's result line; raises ``queue.Empty`` after ``timeout`` s
    of the child's own time. Time in relayed MCP calls (#112: a card may
    wait for the user) is not counted, up to :data:`MAX_RELAY_PAUSE_SECONDS`
    per call, so a loop of relayed calls cannot keep a child alive."""
    start = time.monotonic()
    while True:
        paused = min(proxy.relay_time(), MAX_RELAY_PAUSE_SECONDS)
        left = start + timeout + paused - time.monotonic()
        if left <= 0:
            raise queue.Empty
        try:
            return lines.get(timeout=min(max(left, 0.05), 0.5))
        except queue.Empty:
            continue


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
    "        os.remove(os.path.expanduser('~/keep.txt'))\n"
    "        return {'status': 'unlinked'}\n"
    "    except OSError:\n"
    "        pass\n"
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
                (home / "keep.txt").write_text("x", encoding="utf-8")  # #123
                probe = home / "sandbox_probe.py"
                probe.write_text(_SELF_TEST_TOOL, encoding="utf-8")
                try:
                    out = _spawn("call", probe, "sandbox_probe", {}, network=False,
                                 timeout=DESCRIBE_TIMEOUT_SECONDS, home=home)
                    status = (out.get("result") or {}).get("status")
                    ok = status == "ok" and (home / "written.txt").exists() \
                        and not (home / "Library" / "escape.txt").exists() \
                        and (home / "keep.txt").exists()
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

    def __init__(
        self, meta: dict, source_path: Path, network: Optional[str],
        mcp_names: frozenset[str] = frozenset(), trash: bool = False,
    ) -> None:
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
        # #112: the MCP tools the reviewed source names literally (fixed at
        # load, like ``network_allowed``).
        self.mcp_names = frozenset(mcp_names)
        if trash:
            # #123: the reviewed source calls move_to_trash; the call card
            # (the orchestrator reads ``card_note``) says so.
            self.card_note = trash_card_note
        super().__init__()

    def _mcp_relay(self, name: str, arguments: dict) -> str:
        if name not in self.mcp_names:
            raise McpRelayRefused(
                f"{name} is not named as a literal call_mcp(...) argument in "
                f"{self.name}'s reviewed source"
            )
        return _default_mcp_relay(self.name, self.mcp_names, name, arguments)

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
            mcp_relay=self._mcp_relay,
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
    return SandboxedTool(
        out["meta"], Path(path), network, mcp_tool_names(source), trashes(source),
    )


__all__ = [
    "DENIED_APPLE_EVENT_TARGETS", "FileRelay", "TrashRefused", "move_to_trash",
    "own_folders", "trash_card_note", "trashes", "unlink_rules",
    "McpRelayRefused", "NETWORK_MODULES", "OpenRefused",
    "SandboxedTool", "TOOL_FILES_DIR", "mcp_tool_names",
    "check_open_args", "ToolSandboxError", "ToolSandboxTimeout",
    "build_profile", "closed_paths", "deny_rules", "load_sandboxed_tool",
    "moved_home", "network_reason", "protected_path_note", "reset_sandbox_cache",
    "sandbox_available", "sandbox_enabled", "sandbox_problem",
]
