"""Sandboxed dry run of drafted @tool modules (Phase 5.1).

Structural checks and the critic read the code; neither runs it, so bugs
that only show at runtime slip through (e.g. a file that matches two glob
patterns gets moved twice and the second ``shutil.move`` raises). This
module runs the drafted tool before the user reviews it:

1. :func:`plan_dry_run` asks the model for a small test plan: fixture
   files for a fake home directory, a few calls (kwargs), and what each
   call should do to those files.
2. :func:`run_dry_run` runs each call in a fresh fake home inside macOS
   ``sandbox-exec`` and compares the outcome with the plan.

Isolation is layered, because the code has not been approved yet:

* ``sandbox-exec`` profile: no network, no file writes outside the
  per-call temp dir, and no running other programs (only Python itself).
* What the installed-tool sandbox closes on disk is closed here too
  (#114, ``tools.tool_sandbox.closed_paths``): at the same ``~``-relative
  place in the fake home (``~/.bot-inator``, ``~/.ssh``, ``~/Library``
  for writes, shell start-up files, a bot-inator folder under the home),
  and for reading in the real home (plus the daemon's ``.env``). A draft
  that keeps its file in ``~/.bot-inator`` used to pass here and then
  fail on every call once installed; now it fails here, and
  :func:`refusal_note` tells the drafter where the file can go.
* ``HOME`` points at the fake home, so ``~`` paths land in it, and the
  child starts there, as an installed tool starts in ``$HOME`` (relative
  paths mean the same in both).
* The environment is minimal (no API keys from the daemon's env).
* Inside the probe, ``subprocess`` is replaced by a recorder that does
  nothing and reports success, and ``urlopen`` refuses with a recorded
  error, so tools that shell out to ``osascript`` still run end to end.
* ``tools.mcp`` is a recorder too (#112): ``call_mcp`` returns placeholder
  text and never reaches the daemon or an MCP server.

A refusal no rewrite can fix (#151: macOS refusing an Apple Event to any
sandboxed process, -10004; the sandbox refusing to run a program) is not a
problem sent back to the drafter: :func:`unfixable_refusal` turns it into a
need (``DryRunResult.needs``), kept for the code card by ``ai.tool_needs``,
which says the tool needs to run outside the sandbox. Every other refusal
is unchanged.

If ``sandbox-exec`` is missing or fails its self-test, the dry run is
skipped (returns no problems): unapproved code never runs unsandboxed.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DAEMON_ROOT = Path(__file__).resolve().parent.parent
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_CALL_TIMEOUT_SECONDS = 30
_PLAN_MAX_TOKENS = 900
MAX_FIXTURE_FILES = 20
MAX_CALLS = 3
MAX_FIXTURE_BYTES = 4000
MAX_DRY_RUN_PROBLEMS = 5

PLANNER_SYSTEM_PROMPT = (
    "You write a dry-run test plan for a Python tool before a user "
    "approves it. The tool will run in a sandbox where:\n"
    "- HOME is a fresh, empty folder containing ONLY the files you list;\n"
    "- every shell command (osascript, open, hdiutil, ...) is stubbed: it "
    "does nothing and reports success with empty output;\n"
    "- the network is off;\n"
    "- every MCP tool call (call_mcp) is stubbed: it returns placeholder "
    "text and does nothing.\n\n"
    "Write at most 8 fixture files (each one a different case, never a "
    "numbered series) and 1 to 3 calls that check the spec, including "
    "edge cases a buggy version would get wrong: names that match more "
    "than one rule, different letter case, files that must NOT be "
    "touched, and empty inputs. Use paths relative to HOME for files "
    "(e.g. \"Downloads/inv1041.pdf\") and ~/ paths in kwargs. Expect "
    "file changes only from Python file operations (move, copy, move to "
    "the Trash, write), never from shell commands; a file moved to the "
    "Trash lands in \".Trash/\" under HOME. Only list expectations the spec "
    "clearly implies. expect_exists lists fixture files the call must "
    "leave in place; expect_missing lists fixture files the call must "
    "move or move to the Trash.\n\n"
    "A file value is its text content, or an object "
    '{"content": "<text>", "age_days": <number>} when the spec depends on '
    "how old a file is.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"files": {"<relative path>": "<text content>"}, '
    '"calls": [{"kwargs": {}, "expect_status": "ok" or "error" or null, '
    '"expect_exists": ["<relative path>"], '
    '"expect_missing": ["<relative path>"]}]}'
)

from ai.mlx_engine import THINK_BLOCK_RE as _THINK_BLOCK_RE
from ai.tool_health import _SANDBOX_AE_RE
from tools import tool_sandbox

# Runs inside the sandbox. argv: daemon_root, draft_path, tool_name, kwargs_json
# #123: the installed child's file relay first (moves and move_to_trash go
# to the daemon, which keeps them inside the fake home), the same string.
_PROBE = tool_sandbox.FILE_RELAY_CHILD + r'''
import json, subprocess, sys, threading, time, traceback, urllib.request, urllib.error
root, path, name, kwargs_json = sys.argv[1:5]
sys.path.insert(0, root)
out = {"exception": None, "thread_errors": [], "subprocess": [], "network": [], "mcp": [], "result": None}

class _Done:
    returncode = 0
    stdout = ""
    stderr = ""
    pid = 0
    def __init__(self, args=None, *a, **k):
        self.args = args
    def communicate(self, *a, **k):
        return ("", "")
    def wait(self, *a, **k):
        return 0
    def poll(self):
        return 0
    def check_returncode(self):
        return None

def _record(args=None, *a, **k):
    out["subprocess"].append(args if isinstance(args, (list, str)) else repr(args))
    return _Done(args)

subprocess.run = _record
subprocess.call = lambda *a, **k: (_record(*a, **k), 0)[1]
subprocess.check_call = lambda *a, **k: (_record(*a, **k), 0)[1]
subprocess.check_output = lambda *a, **k: (_record(*a, **k), "")[1]
subprocess.Popen = _record

def _no_network(url, *a, **k):
    out["network"].append(str(getattr(url, "full_url", url)))
    raise urllib.error.URLError("network is disabled in the dry run")
urllib.request.urlopen = _no_network

# #112: MCP calls never reach a server in the dry run (the code is not
# approved yet): ``tools.mcp`` is a recorder that returns a placeholder.
import types as _types
_fake_mcp = _types.ModuleType("tools.mcp")
class McpCallRefused(RuntimeError):
    pass
def _fake_call_mcp(name, arguments=None):
    out["mcp"].append({"name": str(name), "arguments": arguments})
    return "[dry run: the MCP tool %s was not called; placeholder result]" % name
_fake_mcp.call_mcp = _fake_call_mcp
_fake_mcp.McpCallRefused = McpCallRefused
_fake_mcp.RELAY_SOCKET_ENV = "AGENT_TOOL_RELAY_SOCKET"
sys.modules["tools.mcp"] = _fake_mcp

def _hook(args):
    msg = f"{args.exc_type.__name__}: {args.exc_value}"
    frames = [f for f in traceback.extract_tb(args.exc_traceback) if f.filename == path]
    if frames:
        msg += f" (at line {frames[-1].lineno}: `{(frames[-1].line or '').strip()}`)"
    out["thread_errors"].append(msg)
threading.excepthook = _hook

try:
    import ai  # noqa: F401  - same import order as the daemon
    import importlib.util
    spec = importlib.util.spec_from_file_location("draft_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    obj = getattr(mod, name)
    fn = getattr(obj, "forward", obj)
    out["result"] = fn(**json.loads(kwargs_json))
except BaseException as exc:
    msg = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == path]
    if frames:
        f = frames[-1]
        msg += f" (at line {f.lineno}: `{(f.line or '').strip()}`)"
    out["exception"] = msg

deadline = time.time() + 3.0
for t in threading.enumerate():
    if t is not threading.main_thread():
        t.join(timeout=max(0.0, deadline - time.time()))

out["pending_threads"] = [t.name for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
print("__DRY_RUN__" + json.dumps(out, default=str), flush=True)
import os as _os
_os._exit(0)  # do not wait for long timers (a reminder in 5 minutes)
'''


@dataclass
class DryRunCall:
    kwargs: dict[str, Any] = field(default_factory=dict)
    expect_status: Optional[str] = None
    expect_exists: list[str] = field(default_factory=list)
    expect_missing: list[str] = field(default_factory=list)


@dataclass
class DryRunPlan:
    files: dict[str, str]
    calls: list[DryRunCall]
    # Optional age in days per fixture (sets mtime), for "older than N days" specs.
    ages: dict[str, float] = field(default_factory=dict)


def dry_run_enabled() -> bool:
    """``AGENT_DRAFT_DRY_RUN=0`` turns the dry run off (default on)."""
    return os.environ.get("AGENT_DRAFT_DRY_RUN", "1") != "0"


def _safe_rel(path: Any) -> Optional[str]:
    """Normalise a fixture path; ``None`` if it could escape the fake home."""
    text = str(path or "").strip().replace("\\", "/")
    if text.startswith("~/"):
        text = text[2:]
    p = PurePosixPath(text)
    if not text or p.is_absolute() or ".." in p.parts:
        return None
    return str(p)


def parse_plan(raw: str) -> Optional[DryRunPlan]:
    """Validate the planner's JSON; ``None`` when it is unusable."""
    text = _THINK_BLOCK_RE.sub("", raw or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    files: dict[str, str] = {}
    ages: dict[str, float] = {}
    raw_files = data.get("files") or {}
    if isinstance(raw_files, dict):
        for rel, spec in list(raw_files.items())[:MAX_FIXTURE_FILES]:
            safe = _safe_rel(rel)
            if not safe:
                continue
            content = spec
            if isinstance(spec, dict):
                content = spec.get("content", "")
                try:
                    age = float(spec.get("age_days") or 0)
                except (TypeError, ValueError):
                    age = 0.0
                if 0 < age <= 3650:
                    ages[safe] = age
            files[safe] = str(content if content is not None else "")[:MAX_FIXTURE_BYTES]
    calls: list[DryRunCall] = []
    for item in (data.get("calls") or [])[:MAX_CALLS]:
        if not isinstance(item, dict):
            continue
        kwargs = item.get("kwargs") or {}
        if not isinstance(kwargs, dict):
            continue
        status = item.get("expect_status")
        calls.append(DryRunCall(
            kwargs=kwargs,
            expect_status=status if status in {"ok", "error"} else None,
            expect_exists=[s for s in map(_safe_rel, item.get("expect_exists") or []) if s],
            expect_missing=[s for s in map(_safe_rel, item.get("expect_missing") or []) if s],
        ))
    if not calls:
        return None
    # Keep "must still exist" checks only for fixture files. Paths the
    # tool creates depend on naming choices the spec rarely pins down;
    # the planner guessed ``inv1041_copy_1.pdf`` for a file with no name
    # clash and drove three needless repairs (Phase 5.1 real-model run).
    # Selection bugs still show: a fixture that should stay is missing,
    # or one that should go is still there.
    for call in calls:
        call.expect_exists = [rel for rel in call.expect_exists if rel in files]
    return DryRunPlan(files=files, calls=calls, ages=ages)


def _signature(tool_name: str, source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return f"{tool_name}(...)"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == tool_name:
            return f"def {node.name}({ast.unparse(node.args)})"
    return f"{tool_name}(...)"


def plan_dry_run(
    engine: Any, tool_name: str, description: str, expected_logic: str, source: str,
) -> Optional[DryRunPlan]:
    """Ask ``engine`` for a test plan; ``None`` if it fails or is unusable."""
    user = (
        f"tool_name: {tool_name}\ndescription: {description}\n"
        f"expected_logic: {expected_logic}\nsignature: {_signature(tool_name, source)}"
    )
    try:
        raw = engine.generate_chat_sync(
            PLANNER_SYSTEM_PROMPT, user, max_tokens=_PLAN_MAX_TOKENS
        )
    except Exception:
        logger.exception("dry-run planner failed; skipping dry run")
        return None
    plan = parse_plan(raw)
    if plan is None:
        logger.warning("dry-run plan unusable; skipping dry run: %r", (raw or "")[:200])
    return plan


def _fake_closed(home: Path) -> dict[str, list[Path]]:
    """What the installed profile closes, at the same place in the fake home."""
    real_home = tool_sandbox.sandbox_home()
    real = tool_sandbox.closed_paths(
        real_home, extra_read_denied=tool_sandbox._extra_read_denied(),
    )
    return tool_sandbox.moved_home(real, real_home, home)


def _profile(
    work: Path, python: str, home: Optional[Path] = None,
    relay_socket: Optional[Path] = None,
) -> str:
    """The dry-run profile; ``home`` is the fake home (``work/home``).

    #114: after the write allowance come the paths the installed-tool
    sandbox closes for the real home, moved to the same place in the fake
    home (where ``~`` lands), then the real home's own read rule (absolute
    paths: the secrets an unapproved draft must not read). The real home's
    write rule is left out: nothing outside ``work`` is writable here, and
    it would close ``work`` itself when the temp dir lies inside a closed
    folder (a ``TMPDIR`` under ``~/Library``).

    #123: the installed profile's unlink rule too, for the fake home: no
    unlink / rmdir / rename-away there outside the tool's own folders
    (``tmp`` and ``~/Documents/bot-inator``), so a draft that deletes a
    file fails here; ``relay_socket`` is the file relay the moves and
    ``move_to_trash`` use instead.
    """
    base = os.path.realpath(sys.base_prefix)
    home = home or work / "home"
    real_home = tool_sandbox.sandbox_home()
    real = tool_sandbox.closed_paths(
        real_home, extra_read_denied=tool_sandbox._extra_read_denied(),
    )
    fake = tool_sandbox.moved_home(real, real_home, home)
    rules = (
        tool_sandbox.unlink_rules(home, tool_sandbox.own_folders(home, work / "tmp"))
        + tool_sandbox.deny_rules(fake) + tool_sandbox.deny_rules(real, writes=False)
    )
    relay = (
        "(allow network-outbound (remote unix-socket (path-literal "
        f"{tool_sandbox._q(relay_socket)})))\n" if relay_socket is not None else ""
    )
    return (
        "(version 1)\n(allow default)\n(deny network*)\n" + relay + "(deny file-write*)\n"
        f'(allow file-write* (subpath "{work}") (literal "/dev/null"))\n'
        + "".join(rule + "\n" for rule in rules)
        + "(deny process-exec)\n"
        f'(allow process-exec (literal "{python}") (subpath "{base}"))\n'
    )


def _execute(
    tool_name: str, source: str, files: dict[str, str], kwargs: dict,
    ages: Optional[dict[str, float]] = None,
) -> tuple[dict, set[str]]:
    """Run one call in a fresh sandbox. Returns (probe output, files after)."""
    with tempfile.TemporaryDirectory(prefix="dry-run-") as tmp:
        work = Path(os.path.realpath(tmp))
        home = work / "home"
        (work / "tmp").mkdir()
        home.mkdir()
        for rel, content in files.items():
            target = home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            age = (ages or {}).get(rel)
            if age:
                stamp = time.time() - age * 86400
                os.utime(target, (stamp, stamp))
        draft = work / f"{tool_name}.py"
        draft.write_text(source, encoding="utf-8")
        profile = work / "profile.sb"
        python = sys.executable
        own = tool_sandbox.own_folders(home, work / "tmp")
        # #123: the same file relay as an installed tool's, kept to the fake
        # home (and answering nothing but moves: this code is unapproved).
        relay = tool_sandbox._OpenProxy(
            home, work, files=tool_sandbox.FileRelay(home, _fake_closed(home), own),
            file_only=True,
        )
        profile.write_text(_profile(work, python, home, relay.path), encoding="utf-8")
        env = {
            "HOME": str(home),
            "TMPDIR": str(work / "tmp"),
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "AGENT_DISABLE_LISTENERS": "1",
            tool_sandbox.RELAY_SOCKET_ENV: str(relay.path),
            tool_sandbox.OWN_DIRS_ENV: os.pathsep.join(str(p) for p in own),
        }
        try:
            proc = subprocess.run(
                [_SANDBOX_EXEC, "-f", str(profile), python, "-c", _PROBE,
                 str(_DAEMON_ROOT), str(draft), tool_name, json.dumps(kwargs)],
                capture_output=True, text=True, timeout=_CALL_TIMEOUT_SECONDS,
                cwd=str(home), env=env,
            )
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return {"timeout": True}, set()
        finally:
            relay.close()
        after = {
            str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()
        }
    marker = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith("__DRY_RUN__")), None)
    if marker is None:
        return {"no_output": (stderr.strip().splitlines() or ["no output"])[-1]}, after
    try:
        out = json.loads(marker[len("__DRY_RUN__"):])
    except ValueError:
        out = None
    if not isinstance(out, dict):
        # A draft that prints its own marker line and exits.
        return {"no_output": "its last result line was not the probe's"}, after
    out["home"] = str(home)  # the paths in its messages start here (#114)
    return out, after


_SANDBOX_OK: Optional[bool] = None

_SELF_TEST_TOOL = (
    "from smolagents import tool\n\n\n@tool\ndef probe_tool() -> dict:\n"
    '    """Self-test.\n\n    Returns:\n        dict.\n    """\n'
    "    import os\n"
    "    open(os.path.expanduser('~/written.txt'), 'w').write('x')\n"
    "    try:\n        os.remove(os.path.expanduser('~/keep.txt'))\n"
    "        return {'status': 'unlinked'}\n"
    "    except OSError:\n        pass\n"
    "    try:\n        open('/tmp/bot-inator-dry-run-escape.txt', 'w')\n"
    "        return {'status': 'escaped'}\n"
    "    except OSError:\n        return {'status': 'ok'}\n"
)


def sandbox_available() -> bool:
    """Check once that the sandbox runs a tool and blocks writes outside it."""
    global _SANDBOX_OK
    if _SANDBOX_OK is not None:
        return _SANDBOX_OK
    ok = False
    if sys.platform == "darwin" and Path(_SANDBOX_EXEC).exists():
        try:
            # #123: and refuses to unlink a file in the fake home.
            out, after = _execute("probe_tool", _SELF_TEST_TOOL, {"keep.txt": "x"}, {})
            result = out.get("result") or {}
            ok = result.get("status") == "ok" and "written.txt" in after and "keep.txt" in after
            if not ok:
                logger.warning("dry-run sandbox self-test failed: %r", out)
        except Exception:
            logger.exception("dry-run sandbox self-test crashed")
    else:
        logger.warning("sandbox-exec not available; drafted tools will not be dry-run")
    _SANDBOX_OK = ok
    return ok


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class DryRunResult:
    """Outcome of a dry run, split by how much the evidence can be trusted.

    ``hard``: facts observed while running the code (an exception, a
    background-thread error, a timeout, a return value that is not a
    status dict). These drive repairs.

    ``soft``: mismatches against the planner's expectations. The planner
    is a model guessing what the spec implies and was wrong in about half
    the real-model ``file_invoices`` runs (e.g. expecting ``inv1041_copy.pdf``
    to stay although ``inv*.pdf`` matches it), so these never drive
    repairs; they are shown to the human reviewer as notes.
    """

    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    # #151: refusals no rewrite can fix (Apple Event -10004, a program the
    # sandbox will not run). Not repairs: the code card says the tool needs
    # to run outside the sandbox (``ai.tool_needs``).
    needs: list[Any] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return self.hard + self.soft


def _refusal(text: str, out: dict, *, raised: bool) -> str:
    homes = [Path(h) for h in (out.get("home"),) if h]
    return tool_sandbox.protected_path_note(
        text, homes + [tool_sandbox.sandbox_home()], raised=raised,
    )


def _result_text(out: dict) -> str:
    result = out.get("result")
    return "" if result is None else json.dumps(result, default=str, ensure_ascii=False)


def refusal_note(out: dict) -> str:
    """What the tool sandbox refused in this call, for the drafter (#114).

    "" unless the call's error or a background-thread error names a path
    the installed-tool sandbox closes, or its returned value reports a
    failed file operation on one (see
    :func:`tools.tool_sandbox.protected_path_note`). :func:`run_dry_run`
    adds it to the hard problems once per run.
    """
    errors = [str(out.get("exception") or ""), *map(str, out.get("thread_errors") or [])]
    for text in errors:
        note = _refusal(text, out, raised=True) if text else ""
        if note:
            return note
    return _refusal(_result_text(out), out, raised=False)


def unfixable_refusal(text: str, out: Optional[dict] = None) -> Optional[Any]:
    """#151: a ``tool_needs.Need`` when ``text`` is a sandbox refusal no rewrite
    can fix: macOS refusing an Apple Event to any sandboxed process (-10004,
    "privilege violation"), or the sandbox refusing to run a program. A
    refused PATH is not one of these (the drafter can move the file, #114),
    nor is anything else; those stay hard problems."""
    from ai import tool_health, tool_needs

    if not text:
        return None
    if _SANDBOX_AE_RE.search(text):
        return tool_needs.Need(tool_needs.OUTSIDE_SANDBOX, tool_needs.REASON_DRY_RUN_APPLE_EVENT)
    if out is not None and _refusal(text, out, raised=True):
        return None
    if tool_health._SANDBOX_EXEC_RE.search(text):
        return tool_needs.Need(tool_needs.OUTSIDE_SANDBOX, tool_needs.REASON_DRY_RUN_EXEC)
    return None


def _split_unfixable(texts: list[str], out: dict) -> tuple[list[str], list[Any]]:
    kept: list[str] = []
    needs: list[Any] = []
    for text in texts:
        need = unfixable_refusal(text, out)
        if need is None:
            kept.append(text)
        elif need not in needs:
            needs.append(need)
    return kept, needs


def evaluate_call(call: DryRunCall, out: dict, after: set[str], label: str) -> DryRunResult:
    """Turn one probe result into hard and soft problems (and #151 needs)."""
    if out.get("timeout"):
        return DryRunResult(hard=[f"{label}: did not finish within {_CALL_TIMEOUT_SECONDS}s"])
    if "no_output" in out:
        return DryRunResult(hard=[f"{label}: the process exited without a result ({out['no_output']})"])
    problems: list[str] = []
    soft: list[str] = []
    raised = [str(out["exception"])] if out.get("exception") else []
    raised, needs = _split_unfixable(raised, out)
    threads, thread_needs = _split_unfixable([str(e) for e in out.get("thread_errors") or []], out)
    needs += [n for n in thread_needs if n not in needs]
    for exc in raised:
        problems.append(f"{label}: raised {exc}")
    for err in threads:
        problems.append(f"{label}: a background thread raised {err}")
    result = out.get("result")
    if isinstance(result, dict) and result.get("status") == "error":
        # The draft caught the refusal itself and reported it.
        need = unfixable_refusal(_result_text(out))
        if need is not None and need not in needs:
            needs.append(need)
    if not out.get("exception"):
        if not isinstance(result, dict) or "status" not in result:
            problems.append(
                f"{label}: returned {_short(result)} instead of a dict with a 'status' key"
            )
        elif _refusal(_result_text(out), out, raised=False):
            # #114: the draft caught the sandbox's refusal and reported it. A
            # well-formed result, but the tool cannot do its job like this.
            problems.append(
                f"{label}: returned {_short(result)}, which shows a path the "
                "tool sandbox refuses"
            )
        elif (
            call.expect_status
            and result.get("status") != call.expect_status
            and not out.get("network")  # network is off, so errors are expected
            and not out.get("mcp")  # #112: MCP results are placeholders
        ):
            soft.append(
                f"{label}: returned status {result.get('status')!r} "
                f"({_short(result)}) but the spec implies {call.expect_status!r}"
            )
    for rel in call.expect_exists:
        if rel not in after:
            soft.append(f"{label}: expected {rel} to exist afterwards, but it does not")
    for rel in call.expect_missing:
        if rel in after:
            soft.append(f"{label}: expected {rel} to be gone afterwards, but it is still there")
    if any(p.startswith(label + ": expected") for p in soft):
        soft.append(f"{label}: files after the call: {sorted(after)}")
    return DryRunResult(hard=problems, soft=soft, needs=needs)


def run_dry_run(tool_name: str, source: str, plan: DryRunPlan) -> DryRunResult:
    """Run every planned call (empty result = pass, or skipped without a sandbox)."""
    if not sandbox_available():
        return DryRunResult()
    total = DryRunResult()
    notes: list[str] = []
    for i, call in enumerate(plan.calls, 1):
        label = f"dry run {i} {tool_name}(**{_short(call.kwargs, 80)}) with files {sorted(plan.files)}"
        try:
            out, after = _execute(tool_name, source, plan.files, call.kwargs, plan.ages)
        except Exception:
            logger.exception("dry run %d crashed; skipping", i)
            continue
        one = evaluate_call(call, out, after, label)
        total.hard.extend(one.hard)
        total.soft.extend(one.soft)
        total.needs.extend(n for n in one.needs if n not in total.needs)
        note = refusal_note(out)
        if note and one.hard and note not in notes:
            notes.append(note)
    # #114: the sandbox's reason goes last, once, and is never cut off: it
    # is the line that tells the drafter what to change.
    total.hard = total.hard[:MAX_DRY_RUN_PROBLEMS] + notes
    total.soft = total.soft[:MAX_DRY_RUN_PROBLEMS]
    _record_needs(tool_name, source, total.needs)
    return total


def _record_needs(tool_name: str, source: str, needs: list[Any]) -> None:
    """#151: keep this draft's needs for its code card (``ai.tool_needs``)."""
    try:
        from ai import tool_needs

        tool_needs.record_dry_run(tool_name, source, needs)
    except Exception:
        logger.debug("could not record dry-run needs", exc_info=True)


def remove_sandbox_cache() -> None:
    """Reset the cached self-test result (tests)."""
    global _SANDBOX_OK
    _SANDBOX_OK = None


__all__ = [
    "DryRunCall", "DryRunPlan", "DryRunResult", "dry_run_enabled", "evaluate_call",
    "parse_plan", "plan_dry_run", "refusal_note", "run_dry_run", "sandbox_available",
    "unfixable_refusal",
]
