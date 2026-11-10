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
* ``HOME`` points at the fake home, so ``~`` paths land in it.
* The environment is minimal (no API keys from the daemon's env).
* Inside the probe, ``subprocess`` is replaced by a recorder that does
  nothing and reports success, and ``urlopen`` refuses with a recorded
  error, so tools that shell out to ``osascript`` still run end to end.

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
    "- the network is off.\n\n"
    "Write at most 8 fixture files (each one a different case, never a "
    "numbered series) and 1 to 3 calls that check the spec, including "
    "edge cases a buggy version would get wrong: names that match more "
    "than one rule, different letter case, files that must NOT be "
    "touched, and empty inputs. Use paths relative to HOME for files "
    "(e.g. \"Downloads/inv1041.pdf\") and ~/ paths in kwargs. Expect "
    "file changes only from Python file operations (move, copy, delete, "
    "write), never from shell commands. Only list expectations the spec "
    "clearly implies. expect_exists lists fixture files the call must "
    "leave in place; expect_missing lists fixture files the call must "
    "move or delete.\n\n"
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

# Runs inside the sandbox. argv: daemon_root, draft_path, tool_name, kwargs_json
_PROBE = r'''
import json, subprocess, sys, threading, time, traceback, urllib.request, urllib.error
root, path, name, kwargs_json = sys.argv[1:5]
sys.path.insert(0, root)
out = {"exception": None, "thread_errors": [], "subprocess": [], "network": [], "result": None}

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


def _profile(work: Path, python: str) -> str:
    base = os.path.realpath(sys.base_prefix)
    return (
        "(version 1)\n(allow default)\n(deny network*)\n(deny file-write*)\n"
        f'(allow file-write* (subpath "{work}") (literal "/dev/null"))\n'
        "(deny process-exec)\n"
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
        profile.write_text(_profile(work, python), encoding="utf-8")
        env = {
            "HOME": str(home),
            "TMPDIR": str(work / "tmp"),
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "AGENT_DISABLE_LISTENERS": "1",
        }
        try:
            proc = subprocess.run(
                [_SANDBOX_EXEC, "-f", str(profile), python, "-c", _PROBE,
                 str(_DAEMON_ROOT), str(draft), tool_name, json.dumps(kwargs)],
                capture_output=True, text=True, timeout=_CALL_TIMEOUT_SECONDS,
                cwd=str(work), env=env,
            )
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return {"timeout": True}, set()
        after = {
            str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()
        }
    marker = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith("__DRY_RUN__")), None)
    if marker is None:
        return {"no_output": (stderr.strip().splitlines() or ["no output"])[-1]}, after
    return json.loads(marker[len("__DRY_RUN__"):]), after


_SANDBOX_OK: Optional[bool] = None

_SELF_TEST_TOOL = (
    "from smolagents import tool\n\n\n@tool\ndef probe_tool() -> dict:\n"
    '    """Self-test.\n\n    Returns:\n        dict.\n    """\n'
    "    import os\n"
    "    open(os.path.expanduser('~/written.txt'), 'w').write('x')\n"
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
            out, after = _execute("probe_tool", _SELF_TEST_TOOL, {}, {})
            result = out.get("result") or {}
            ok = result.get("status") == "ok" and "written.txt" in after
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

    @property
    def all(self) -> list[str]:
        return self.hard + self.soft


def evaluate_call(call: DryRunCall, out: dict, after: set[str], label: str) -> DryRunResult:
    """Turn one probe result into hard and soft problems."""
    if out.get("timeout"):
        return DryRunResult(hard=[f"{label}: did not finish within {_CALL_TIMEOUT_SECONDS}s"])
    if "no_output" in out:
        return DryRunResult(hard=[f"{label}: the process exited without a result ({out['no_output']})"])
    problems: list[str] = []
    soft: list[str] = []
    if out.get("exception"):
        problems.append(f"{label}: raised {out['exception']}")
    for err in out.get("thread_errors") or []:
        problems.append(f"{label}: a background thread raised {err}")
    result = out.get("result")
    if not out.get("exception"):
        if not isinstance(result, dict) or "status" not in result:
            problems.append(
                f"{label}: returned {_short(result)} instead of a dict with a 'status' key"
            )
        elif (
            call.expect_status
            and result.get("status") != call.expect_status
            and not out.get("network")  # network is off, so errors are expected
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
    return DryRunResult(hard=problems, soft=soft)


def run_dry_run(tool_name: str, source: str, plan: DryRunPlan) -> DryRunResult:
    """Run every planned call (empty result = pass, or skipped without a sandbox)."""
    if not sandbox_available():
        return DryRunResult()
    total = DryRunResult()
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
    total.hard = total.hard[:MAX_DRY_RUN_PROBLEMS]
    total.soft = total.soft[:MAX_DRY_RUN_PROBLEMS]
    return total


def remove_sandbox_cache() -> None:
    """Reset the cached self-test result (tests)."""
    global _SANDBOX_OK
    _SANDBOX_OK = None


__all__ = [
    "DryRunCall", "DryRunPlan", "DryRunResult", "dry_run_enabled", "evaluate_call",
    "parse_plan", "plan_dry_run", "run_dry_run", "sandbox_available",
]
