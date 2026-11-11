"""AI-driven meta-tool that drafts, reviews, and installs new @tool modules."""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

from tools.draft_checks import check_draft
from tools.draft_critic import critic_enabled, critique, with_review_notes
from tools.draft_dry_run import DryRunPlan, dry_run_enabled, plan_dry_run, run_dry_run

if TYPE_CHECKING:  # pragma: no cover - type-only
    from ai.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Populated by :func:`set_meta_tool_context` at lifespan startup.
# ``_ENGINE`` and ``_DRAFT_ENGINE`` are duck-typed against the
# :class:`~ai.mlx_engine.MLXEngine` surface (``generate_chat_sync`` is
# the only method called here); a :class:`~ai.groq_engine.GroqEngine`
# instance is equally accepted when ``settings.provider == "groq"``.
_ORCHESTRATOR: "Optional[Orchestrator]" = None
_ENGINE: Optional[Any] = None
# Code-specialist engine used exclusively for drafting. Falls back to
# ``_ENGINE`` when the lifespan didn't wire a dedicated drafting model.
_DRAFT_ENGINE: Optional[Any] = None
_GENERATED_DIR: Optional[Path] = None

# Bare module imports we refuse to accept from drafted code. Anything outside
# the Python standard library + ``smolagents`` is out of bounds so generated
# tools stay portable between agent installs.
_ALLOWED_STDLIB: frozenset[str] = frozenset({
    "__future__",
    "ast", "base64", "collections", "contextlib", "csv", "datetime",
    "difflib", "enum", "functools", "glob", "hashlib", "html", "http",
    "io", "itertools", "json", "logging", "math", "os", "pathlib",
    "random", "re", "shlex", "shutil", "socket", "sqlite3", "statistics",
    "string", "subprocess", "sys", "tempfile", "textwrap", "threading",
    "time", "typing", "urllib", "urllib.parse", "urllib.request", "uuid",
    "zipfile",
})
_ALLOWED_THIRD_PARTY: frozenset[str] = frozenset({"smolagents"})
# In-tree packages drafted tools are allowed to import from. Needed so
# composition works: ``from tools.show_notification import show_notification``
# must pass the import check, since the system prompt mandates that
# pattern for reusing built-in tools instead of reinventing primitives.
_ALLOWED_INTERNAL: frozenset[str] = frozenset({"tools"})
_BANNED_SUBSTRINGS: tuple[str, ...] = (
    "__import__(", "eval(", "exec(", "compile(",
    "os.system(", "os.popen(", "pty.spawn(",
)
# Placeholder/reserved domains the model likes to hallucinate when it
# doesn't know a real endpoint. Matched case-insensitively against the
# draft source so the validator catches them before the user ever sees
# the approval dialog. Keep the entries narrow (hostnames, not bare
# words like "example") to avoid false positives in legitimate prose.
_PLACEHOLDER_DOMAINS: tuple[str, ...] = (
    "example.com", "example.org", "example.net",
    "api.example.com", "api.example.org",
    "yourapi.com", "your-api.com",
    "yourdomain.com", "your-domain.com",
    "yourservice.com", "api.yourservice.com",
    "myapi.com", "my-api.com",
    "placeholder.com",
)
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
# Draft attempts before giving up: the first draft plus repair turns.
MAX_DRAFT_ATTEMPTS = 3
# 1200 tokens truncated longer modules mid-function.
_DRAFT_MAX_TOKENS = 2048

_SYSTEM_PROMPT_BASE = """You write a single Python module that defines ONE smolagents @tool.

Hard rules:
* First line must be a one-line docstring.
* `from __future__ import annotations` on line 2.
* Import `from smolagents import tool`.
* Define exactly one function decorated with @tool. The function name MUST match the requested tool_name.
* Use ONLY the Python standard library and `smolagents`. No third-party packages.
* For HTTP calls, use `urllib.request` from the standard library. Do NOT import `requests`, `httpx`, `aiohttp`, `urllib3`, or any other third-party HTTP client.
* Read credentials (API tokens, keys) from `os.environ`; never hard-code them.

Endpoint rules (CRITICAL — read before writing any HTTP code):
* NEVER invent URLs. Every URL you write must be one the user explicitly specified in ``expected_logic``, or a well-known public API on a real domain (e.g. ``api.github.com``, ``api.openweathermap.org``, ``api.openai.com``).
* The following placeholder domains DO NOT EXIST and MUST NOT appear in your draft under any circumstance: ``example.com``, ``example.org``, ``example.net``, ``yourapi.com``, ``api.example.com``, ``api.yourservice.com``, ``localhost`` (unless the user asked for it), ``your-domain.com``, ``your-api.com``. A draft that hits one of these will fail DNS or 404 at runtime and the user will see a silent error.
* If the task requires an HTTP endpoint and you do NOT know a real, verifiable URL for it, DO NOT invent one. Instead, structure the tool to return ``{"status": "error", "message": "<concrete reason this cannot be done without a real endpoint>"}`` so the caller sees a useful failure.
* If the task is fundamentally natural-language (classify, summarize, "understand this message"), and no existing built-in tool covers it, DO NOT fake it with an HTTP call. Return ``{"status": "error", "message": "no local primitive for text analysis is available"}``.
* The function docstring MUST describe what the tool does, its Args, and its Returns.
* Never call exec, eval, __import__, compile, os.system, os.popen, or pty.spawn.
* Return a dict with a `status` key of either "ok" or "error".
* Do not emit backticks, markdown fences, commentary, or any text outside the module source.

Sandbox limits (CRITICAL — your tool runs under macOS sandbox-exec; these fail there no matter how they are written):
* It CANNOT quit or close apps: macOS refuses the quit Apple Event to every sandboxed process (-10004), from AppleScript, `osascript`, `kill` or AppKit alike.
* It CANNOT read Mail's messages (lists, subjects, senders, bodies): Mail refuses message data to sandboxed processes (-10004), even with Automation allowed.
* It CANNOT run programs other than `osascript`, `shortcuts`, `mdfind` and `open` (e.g. no `screencapture`, `kill`, `pkill`, `killall`, `sh`, `curl`).
* It CANNOT open URLs, files or apps except through `open` on PATH (the daemon checks the arguments); no NSWorkspace, `open location` or LaunchServices calls.
* Calling the built-ins below from inside your tool does not help: they run in the same sandbox. The assistant calls them directly instead, so for these tasks do NOT write a tool:
  - quit / close an app: the built-in `quit_app(app_name)`;
  - take a screenshot or screen capture: the built-in `take_screenshot(path)`;
  - list recent emails: the built-in `list_recent_emails(count, mailbox)`; one email's body: `read_email(message_id)`.
* If the requested tool needs one of these anyway, return ``{"status": "error", "message": "<which built-in to call instead>"}`` rather than code that cannot work.

User-visible output rules:
* NEVER use `print()` to communicate with the user. The daemon's stdout is not a user interface.
* To notify the user (timer fires, reminder triggers, task completes, alert), either:
  (a) Shell out to macOS: `subprocess.run(["osascript", "-e", 'display notification "<msg>" with title "<title>" sound name "Ping"'])`, or
  (b) Call an existing built-in tool (see BUILT-IN TOOLS below) by importing and invoking it directly.
* Whichever path you pick, the user must actually see something — do not "log" or "print" completion.

Background work rules:
* When spawning threads for timers, delays, or polling, always mark them as daemons:
  `threading.Thread(target=..., args=..., daemon=True).start()`
  Non-daemon threads block daemon shutdown.
* Prefer `threading.Timer(interval, callback).start()` over hand-rolled `time.sleep` countdown loops when the only goal is "fire once after N seconds".
* Do not tight-loop on `time.sleep(1)` just to decrement a counter — it wastes cycles and produces no useful output.

Composition rules (IMPORTANT — read carefully):
* Your tool SHOULD call other tools whenever they do part of what you need. Tools that compose other tools are strongly preferred over tools that re-implement primitives.
* The AVAILABLE TOOLS list below shows every tool currently registered. Each entry gives the `import path` you use to bring the tool into your module and call it.
* Built-in tools live under `tools.<module>` (e.g. `from tools.show_notification import show_notification`); the module name can differ from the tool name, so copy the exact import line from the AVAILABLE TOOLS list.
* Previously-generated tools live under `tools.generated.<name>` (e.g. `from tools.generated.fetch_weather import fetch_weather`).
* Imported tools are invoked as normal Python callables: `show_notification(title="Done", body="...")`. Check each tool's description for its arguments before calling it.
* MANDATORY: If you call a tool anywhere in your module (including inside nested functions, closures, or threads), you MUST write its exact `from tools.X import X` line at the top of the file with the other imports. Calling a tool that is not imported will raise `NameError` at runtime — the error will be swallowed inside background threads and your tool will appear to succeed while doing nothing.
* Before you emit the module, scan every function call in your body. For each call whose name appears in AVAILABLE TOOLS, verify there is a matching import at the top. If one is missing, add it.
* Do NOT duplicate any existing tool's name. The orchestrator will reject drafts whose name collides with one already registered.
* If the task can be fully accomplished by chaining existing tools, your tool body should consist almost entirely of those calls.

Worked example (structure to mirror when composing built-ins):
    \"\"\"Fire a macOS notification after a delay.\"\"\"
    from __future__ import annotations
    from smolagents import tool
    from tools.show_notification import show_notification
    import threading

    @tool
    def alert_in(seconds: int, title: str, message: str) -> dict:
        \"\"\"Schedule a notification to fire after ``seconds`` seconds.

        Args:
            seconds: Delay before the notification fires.
            title: Notification title.
            message: Notification body.

        Returns:
            dict with ``status`` set to "ok" once the timer is scheduled.
        \"\"\"
        threading.Timer(
            seconds,
            lambda: show_notification(title=title, body=message),
        ).start()
        return {"status": "ok"}
"""


class ToolGenerationError(RuntimeError):
    """Raised when the draft fails static validation or the user denies."""


def set_meta_tool_context(
    *,
    orchestrator: "Orchestrator",
    engine: Any,
    generated_dir: Path,
    drafting_engine: Optional[Any] = None,
) -> None:
    """Wire the module-level singletons the @tool relies on at runtime.

    ``drafting_engine`` is the code-specialist model used to write new
    tool source. When omitted, falls back to ``engine`` so tests and
    headless invocations keep working with a single MLX instance.
    """
    global _ORCHESTRATOR, _ENGINE, _DRAFT_ENGINE, _GENERATED_DIR
    _ORCHESTRATOR = orchestrator
    _ENGINE = engine
    _DRAFT_ENGINE = drafting_engine or engine
    _GENERATED_DIR = Path(generated_dir)
    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)


# ---- idea provenance (#61) -------------------------------------------------
#
# At install time ``generate_custom_tool`` writes
# ``<generated>/.provenance/<tool_name>.json``: where the idea came from, so
# the Tool Manager detail view can say WHEN a generated tool runs. The dot
# directory is skipped by ``Orchestrator.load_dynamic_tools`` (it ignores
# every dot-name and directory) and is never added to the nested git repo
# (``_git_commit`` stages explicit paths only): provenance can quote local
# event text, and it is metadata, not reviewed code. Tools installed before
# #61 have no file and show "unknown".

PROVENANCE_DIRNAME = ".provenance"
_EVIDENCE_MAX_CHARS = 1000
# The idea behind the current pattern turn, set by the orchestrator's
# ``pattern_detected`` branch via :func:`set_current_idea`. Paired with the
# turn's event id so a stale idea from an earlier turn is never attached.
_CURRENT_IDEA: Optional[tuple[str, dict[str, Any]]] = None


def set_current_idea(idea: Optional[dict[str, Any]], *, event_id: str) -> None:
    """Record the ``pattern_detected`` metadata driving turn ``event_id``.

    ``None`` clears it. Only the idea fields provenance needs are kept.
    """
    global _CURRENT_IDEA
    if not idea:
        _CURRENT_IDEA = None
        return
    keep = ("source", "trigger", "tool_name", "description", "expected_logic", "evidence")
    _CURRENT_IDEA = (str(event_id), {k: idea[k] for k in keep if idea.get(k) is not None})


def clear_current_idea() -> None:
    set_current_idea(None, event_id="")


def provenance_path(generated_dir: Path, tool_name: str) -> Path:
    return Path(generated_dir) / PROVENANCE_DIRNAME / f"{tool_name}.json"


def _current_idea_for_turn(event_id: Optional[str]) -> Optional[dict[str, Any]]:
    if _CURRENT_IDEA is None or event_id is None:
        return None
    idea_event, idea = _CURRENT_IDEA
    return dict(idea) if idea_event == event_id else None


def _plain_summary(tool_name: str, description: str, source: str) -> str:
    """The #64 plain-language summary for ``source`` ("" on any failure)."""
    try:
        from ai import tool_summary

        return tool_summary.for_code_review(_ORCHESTRATOR, tool_name, description, source)
    except Exception:
        logger.exception("plain summary failed for %s", tool_name)
        return ""


def _build_provenance(
    tool_name: str, description: str, expected_logic: str, user_edited: bool,
    summary: str = "",
) -> dict[str, Any]:
    from datetime import datetime, timezone

    orch = _ORCHESTRATOR
    event_id = getattr(orch, "_current_event_id", None)
    turn_kind = getattr(orch, "_turn_kind", None) or "unknown"
    idea = _current_idea_for_turn(event_id)
    if idea and isinstance(idea.get("evidence"), str):
        idea["evidence"] = idea["evidence"][:_EVIDENCE_MAX_CHARS]
    return {
        "version": 1,
        "tool_name": tool_name,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "turn_kind": turn_kind,
        "event_id": event_id,
        "source": (idea or {}).get("source") or ("chat" if turn_kind == "chat" else None),
        "trigger": (idea or {}).get("trigger"),
        "idea": idea,
        "requested": {"description": description, "expected_logic": expected_logic},
        "user_edited": bool(user_edited),
        # #64: the plain-language summary the review card showed (rebuilt
        # from the installed source when the user edited the draft).
        "summary": summary or None,
    }


def _save_provenance(
    tool_name: str, description: str, expected_logic: str, user_edited: bool,
    summary: str = "",
) -> Optional[Path]:
    """Best-effort: a provenance failure never fails the install."""
    if _GENERATED_DIR is None:
        return None
    import json

    try:
        record = _build_provenance(
            tool_name, description, expected_logic, user_edited, summary,
        )
        path = provenance_path(_GENERATED_DIR, tool_name)
        _atomic_write(path, json.dumps(record, indent=2, default=str))
        return path
    except Exception:
        logger.exception("Failed to save provenance for %s", tool_name)
        return None


def _strip_fences(text: str) -> str:
    """Pull Python source out of a fenced markdown block if the model added one.

    Tolerates a missing closing fence (model ran out of tokens or stopped
    on its own), optional ``python``/``py`` language tag, and optional
    leading whitespace on the opening fence line.
    """
    fence = re.search(
        r"```(?:python|py)?[ \t]*\n(.*?)(?:```|\Z)",
        text,
        re.DOTALL,
    )
    return fence.group(1).strip() if fence else text.strip()


def _dump_rejected_draft(tool_name: str, source: str, reason: str) -> Optional[Path]:
    """Persist a rejected draft for post-mortem inspection. Best-effort."""
    if _GENERATED_DIR is None:
        return None
    try:
        reject_dir = _GENERATED_DIR / "_rejected"
        reject_dir.mkdir(parents=True, exist_ok=True)
        import time
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = reject_dir / f"{tool_name}.{stamp}.py"
        header = f"# rejected: {reason}\n# tool_name: {tool_name}\n\n"
        path.write_text(header + source, encoding="utf-8")
        return path
    except Exception:
        logger.exception("Failed to dump rejected draft for %s", tool_name)
        return None


def _validate_identifier(name: str) -> None:
    if not _IDENTIFIER_RE.match(name):
        raise ToolGenerationError(
            f"tool_name {name!r} must match [a-z_][a-z0-9_]*"
        )


def _validate_source(tool_name: str, source: str) -> None:
    """Static safety checks on the drafted module. Raises on failure."""
    # ``__import__(`` stays a plain substring ban; the other banned calls
    # are matched on the AST below. Substring matching rejected
    # legitimate drafts: ``re.compile(`` contains ``compile(`` and
    # ``ast.literal_eval(`` contains ``eval(`` (Phase 5.1 real-model runs).
    if "__import__(" in source:
        raise ToolGenerationError("draft contains banned token: __import__(")
    lowered = source.lower()
    for domain in _PLACEHOLDER_DOMAINS:
        if domain in lowered:
            raise ToolGenerationError(
                f"draft references placeholder/fake domain {domain!r}; "
                "tools must use real endpoints or return a structured error"
            )
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ToolGenerationError(f"draft is not valid Python: {exc}") from exc

    allowed = _ALLOWED_STDLIB | _ALLOWED_THIRD_PARTY | _ALLOWED_INTERNAL
    found_tool_fn = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in allowed:
                    raise ToolGenerationError(
                        f"disallowed import: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                raise ToolGenerationError("relative imports are not allowed")
            module = (node.module or "").split(".", 1)[0]
            if module and module not in allowed:
                raise ToolGenerationError(
                    f"disallowed import: {node.module}"
                )
        elif isinstance(node, ast.Call) and _banned_call(node):
            raise ToolGenerationError(
                f"draft calls banned function: {_banned_call(node)}"
            )
        elif isinstance(node, ast.Call):
            # Block ``subprocess.<anything>(..., shell=True)``: subprocess is
            # allowlisted for legitimate uses (e.g. osascript notifications),
            # but ``shell=True`` turns any string argument into a shell
            # injection primitive.
            call_name = _shell_call_name(node)
            if call_name in {"run", "call", "check_call", "check_output", "Popen"}:
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        raise ToolGenerationError(
                            "subprocess call with shell=True is not allowed"
                        )
        elif isinstance(node, ast.FunctionDef) and node.name == tool_name:
            for dec in node.decorator_list:
                dec_name = _decorator_name(dec)
                if dec_name in {"tool", "smolagents.tool"}:
                    found_tool_fn = True
                    break
    if not found_tool_fn:
        raise ToolGenerationError(
            f"no @tool function named {tool_name!r} found in draft"
        )


_BANNED_BUILTIN_CALLS = frozenset({"eval", "exec", "compile", "__import__"})
_BANNED_MODULE_CALLS = frozenset({("os", "system"), ("os", "popen"), ("pty", "spawn")})


def _banned_call(node: ast.Call) -> str:
    """Name of a banned call (``eval(...)``, ``os.system(...)``), else ""."""
    func = node.func
    if isinstance(func, ast.Name) and func.id in _BANNED_BUILTIN_CALLS:
        return f"{func.id}("
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else ""
        if (owner, func.attr) in _BANNED_MODULE_CALLS:
            return f"{owner}.{func.attr}("
        if owner == "builtins" and func.attr in _BANNED_BUILTIN_CALLS:
            return f"builtins.{func.attr}("
    return ""


def _shell_call_name(node: ast.Call) -> str:
    """Return the called function name, or "" for non-Name/Attribute calls."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _decorator_name(dec: ast.expr) -> str:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return f"{getattr(dec.value, 'id', '')}.{dec.attr}".lstrip(".")
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    return ""


def _call_signature(tool_obj: Any) -> str:
    """``title: string, body: string`` from a smolagents Tool's inputs."""
    inputs = getattr(tool_obj, "inputs", None)
    if not isinstance(inputs, dict):
        return "..."
    parts = []
    for arg, spec in inputs.items():
        kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
        optional = isinstance(spec, dict) and spec.get("nullable")
        parts.append(f"{arg}: {kind}" + (" = optional" if optional else ""))
    return ", ".join(parts)


def _builtin_import_path(name: str, tool_obj: Any) -> str:
    """Module path that really defines built-in ``tool_obj``.

    Several tools can share one module (``tools.windows`` holds
    ``list_windows`` and ``open_app``), so ``tools.<name>`` is only a
    fallback: a generated tool that copied a wrong import line would fail
    to load.
    """
    import sys

    for mod_name, module in list(sys.modules.items()):
        if (
            mod_name.startswith("tools.")
            and not mod_name.startswith("tools.generated")
            and getattr(module, name, None) is tool_obj
        ):
            return mod_name
    return f"tools.{name}"


def _format_available_tools_context() -> str:
    """Render the ``AVAILABLE TOOLS`` section for the drafter's system prompt.

    Enumerates every tool registered on the orchestrator — both the
    static built-ins and any previously-generated tools — annotating
    each with its import path so the drafter can compose against them.
    Falls back to a short note when the orchestrator is unavailable
    (e.g. in unit tests).
    """
    if _ORCHESTRATOR is None:
        # No running daemon (scripts, tests): list the static built-ins.
        from tools import AVAILABLE_TOOLS
        all_tools = list(AVAILABLE_TOOLS)
        builtin_names = frozenset(getattr(t, "name", "") for t in all_tools)
        dynamic_names: set[str] = set()
    else:
        all_tools = list(getattr(_ORCHESTRATOR, "tools", []))
        builtin_names = getattr(_ORCHESTRATOR, "_builtin_tool_names", frozenset())
        dynamic_names = getattr(_ORCHESTRATOR, "_dynamic_tool_names", set())

    builtin_lines: list[str] = []
    dynamic_lines: list[str] = []
    for tool_obj in all_tools:
        name = getattr(tool_obj, "name", getattr(tool_obj, "__name__", ""))
        if not name:
            continue
        desc = (
            getattr(tool_obj, "description", None)
            or getattr(tool_obj, "__doc__", None)
            or ""
        )
        first_line = (
            desc.strip().splitlines()[0]
            if desc.strip()
            else "(no description)"
        )
        # Exact keyword arguments: drafts used to call
        # ``show_notification(title=..., message=...)`` (the real name is
        # ``body``), which raised TypeError inside a background thread.
        call = f"{name}({_call_signature(tool_obj)})"
        if name in builtin_names:
            path = _builtin_import_path(name, tool_obj)
            builtin_lines.append(
                f"- {name}  (import: from {path} import {name})\n"
                f"    call: {call}\n"
                f"    {first_line[:220]}"
            )
        elif name in dynamic_names:
            path = f"tools.generated.{name}"
            dynamic_lines.append(
                f"- {name}  (import: from {path} import {name})\n"
                f"    call: {call}\n"
                f"    {first_line[:220]}"
            )

    sections: list[str] = [
        "AVAILABLE TOOLS (call these from your tool body whenever they help):"
    ]
    if builtin_lines:
        sections.append("Built-in tools:")
        sections.extend(builtin_lines)
    if dynamic_lines:
        sections.append("Previously-generated tools:")
        sections.extend(dynamic_lines)
    if not builtin_lines and not dynamic_lines:
        sections.append("(no tools currently registered)")
    return "\n".join(sections) + "\n"


def _draft_with_engine(
    engine: Any,
    tool_name: str,
    description: str,
    expected_logic: str,
    previous_source: str = "",
    problems: Optional[list[str]] = None,
    installed_source: str = "",
    failure_note: str = "",
) -> str:
    """Drive the configured drafting engine to write a @tool module synchronously.

    ``installed_source`` + ``failure_note`` make this a repair of an
    installed tool (#90): every attempt sees the installed code and the
    error it hit at run time.

    Called from the smolagents agent worker thread (spawned by
    ``asyncio.to_thread`` in :meth:`Orchestrator._run_agent`), so we drop
    into the engine's sync API directly. For MLX engines this routes
    through the shared :attr:`MLXEngine.generation_lock`, serializing
    against the agent's own inference loop instead of racing it on the
    Metal heap; for :class:`~ai.groq_engine.GroqEngine` the call is a
    network round-trip with no shared resource to guard.
    """
    system_prompt = (
        _SYSTEM_PROMPT_BASE + "\n" + _format_available_tools_context()
    )
    user_msg = (
        f"tool_name: {tool_name}\n"
        f"description: {description}\n"
        f"expected_logic: {expected_logic}\n\n"
        "Emit the full module now. Output nothing but the Python source."
    )
    if installed_source and failure_note:
        user_msg += _repair_context(installed_source, failure_note)
    if previous_source and problems:
        # Repair turn (Phase 5.1): show the model its own draft and the
        # exact check failures so it fixes them instead of starting over.
        user_msg += (
            "\n\nYour previous draft:\n" + previous_source +
            "\n\nIt failed these checks:\n" +
            "\n".join(f"- {p}" for p in problems) +
            "\n\nFix every problem above and emit the full corrected "
            "module. Output nothing but the Python source."
        )
    raw = engine.generate_chat_sync(
        system_prompt, user_msg, max_tokens=_DRAFT_MAX_TOKENS
    )
    return _strip_fences(raw)


def _atomic_write(path: Path, source: str) -> None:
    """Write ``source`` to ``path`` via a temp file + ``os.replace``.

    Uses a random suffix to prevent predictable temp file names (mitigates
    symlink attacks on shared systems). The temp file is created with
    restrictive permissions (0600) via mkstemp.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a random hex suffix instead of predictable prefix to mitigate
    # symlink race attacks on multi-user systems
    import secrets
    suffix = f".{secrets.token_hex(8)}.tmp"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".", suffix=suffix, dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())  # Ensure data hits disk before rename
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# Tokens that carry no semantic weight for duplicate detection. Dropping
# them avoids matching every tool against every other tool just because
# they share filler words like "tool" or "auto".
_DUP_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "auto", "automate", "automatic", "automation",
    "custom", "for", "from", "handler", "helper", "manager", "me", "my",
    "new", "of", "on", "or", "that", "the", "this", "to", "tool", "tools",
    "user", "users", "util", "utility", "with",
})

# Jaccard overlap (name + description tokens) at which an existing tool
# counts as covering the request.
_DUP_SIMILARITY = 0.5

# Tools that are never "the same" as a requested tool.
# The meta-tools (incl. #90's repair tool, registered in wave 15) are never
# "a similar existing tool" for a new idea.
_DUP_EXEMPT_TOOLS: frozenset[str] = frozenset({"generate_custom_tool", "repair_generated_tool"})


def _dup_stem(token: str) -> str:
    """Crude stem so ``reader``/``read`` and ``generator``/``generate`` match."""
    for suffix in ("ing", "or", "er", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    return token[:-1] if token.endswith("e") and len(token) > 4 else token


def _dup_tokens(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords + short tokens, stem."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return {
        _dup_stem(t) for t in raw if t and len(t) > 2 and t not in _DUP_STOPWORDS
    }


def _find_similar_existing_tool(
    tool_name: str, description: str
) -> Optional[str]:
    """Return an existing tool name that likely covers the requested task.

    Matches when either:

    * every significant token of the requested ``tool_name`` appears in an
      existing tool's name (``auto_screenshot`` vs ``screenshot_tool``), or
    * the requested name+description and the existing name+description
      share at least :data:`_DUP_SIMILARITY` of their tokens (Jaccard).

    A single shared word is not enough: that matched almost every request
    (``create_invoice_template`` against ``read_clipboard``). The
    meta-tool itself is never a match. Used as a semantic-duplicate guard
    in :func:`generate_custom_tool`; exact-name duplicates are caught
    earlier by the on-disk path check.
    """
    if _ORCHESTRATOR is None:
        return None
    request_name = _dup_tokens(tool_name)
    request_all = request_name | _dup_tokens(description)
    if not request_all:
        return None

    for tool_obj in getattr(_ORCHESTRATOR, "tools", []):
        name = getattr(tool_obj, "name", getattr(tool_obj, "__name__", ""))
        if not name or name in _DUP_EXEMPT_TOOLS:
            continue
        desc = (
            getattr(tool_obj, "description", None)
            or getattr(tool_obj, "__doc__", None)
            or ""
        )
        existing_name = _dup_tokens(name)
        if request_name and request_name <= existing_name:
            return name
        existing_all = existing_name | _dup_tokens(desc)
        overlap = len(request_all & existing_all) / len(request_all | existing_all)
        if overlap >= _DUP_SIMILARITY:
            return name
    return None


def _tool_description(tool_obj: Any) -> str:
    """First line of a tool object's description or docstring."""
    desc = (
        getattr(tool_obj, "description", None)
        or getattr(tool_obj, "__doc__", None)
        or ""
    )
    return desc.strip().splitlines()[0].strip() if desc.strip() else ""


def _file_tool_description(path: Path, tool_name: str) -> str:
    """Docstring summary of ``tool_name`` in ``path``, parsed without importing."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return ""
    funcs = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    funcs.sort(key=lambda n: n.name != tool_name)
    for node in [*funcs, tree]:
        doc = ast.get_docstring(node) or ""
        if doc.strip():
            return doc.strip().splitlines()[0].strip()
    return ""


def _existing_generated_tool(tool_name: str) -> Optional[dict[str, str]]:
    """Return ``{"path", "description", "where"}`` when ``tool_name`` is taken.

    A generated tool counts as existing when ``<tool_name>.py`` is in the
    generated dir or the orchestrator has it loaded as a dynamic tool.
    Built-ins are not generated tools; the similarity guard and the
    loader's shadow check cover those.
    """
    if _GENERATED_DIR is None:
        return None
    path = _GENERATED_DIR / f"{tool_name}.py"
    on_disk = path.is_file()
    loaded = tool_name in getattr(_ORCHESTRATOR, "_dynamic_tool_names", set())
    if not (on_disk or loaded):
        return None
    description = ""
    for tool_obj in getattr(_ORCHESTRATOR, "tools", []):
        if getattr(tool_obj, "name", getattr(tool_obj, "__name__", "")) == tool_name:
            description = _tool_description(tool_obj)
            break
    if not description and on_disk:
        description = _file_tool_description(path, tool_name)
    where = " and ".join(
        w for w, flag in (("on disk", on_disk), ("loaded", loaded)) if flag
    )
    return {"path": str(path), "description": description, "where": where}


def _name_collision_error(tool_name: str, existing: dict[str, str]) -> dict[str, Any]:
    """Error result for a ``generate_custom_tool`` name that is already taken."""
    desc = existing["description"] or "(no description)"
    return {
        "status": "error",
        "tool_name": tool_name,
        "error": "name_collision",
        "existing_tool": tool_name,
        "existing_description": existing["description"],
        "path": existing["path"],
        "message": (
            f"A generated tool named {tool_name!r} already exists "
            f"({existing['where']}): {desc} Not drafting a replacement. "
            f"If {tool_name!r} already does the job, call it directly. "
            "Otherwise pick a different name for the new tool, or ask the "
            f"user to delete {tool_name!r} in the Tool Manager first."
        ),
    }


_GIT_NOT_INSTALLED = (
    "git isn't installed (install the Xcode command line tools: "
    "xcode-select --install); the tool was installed without version history"
)
_git_ok_cache: Optional[bool] = None


def _git_available() -> bool:
    """True when a real git can run without macOS's "install developer
    tools" dialog.

    On a Mac without the Xcode command line tools ``/usr/bin/git`` is a
    stub that pops that dialog when run (#96: the bundled app reaches such
    Macs). ``xcode-select -p`` answers without prompting. Cached per process.
    """
    global _git_ok_cache
    if _git_ok_cache is not None:
        return _git_ok_cache
    git = shutil.which("git")
    if git is None:
        _git_ok_cache = False
    elif os.path.realpath(git) == "/usr/bin/git":
        try:
            probe = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True,
                                   text=True, timeout=5, stdin=subprocess.DEVNULL)
            _git_ok_cache = probe.returncode == 0
        except Exception:
            _git_ok_cache = False
    else:
        _git_ok_cache = True
    return _git_ok_cache


def _git_identity_args() -> list[str]:
    """Return ``-c user.name=... -c user.email=...`` for the nested commit.

    Prefers the host's global git identity so the agent's commits blend with
    the user's normal history. Falls back to a synthetic ``bot-inator``
    identity when no global config is set (fresh machine, CI, etc.).
    """
    def _global(key: str) -> str:
        if not _git_available():
            return ""
        try:
            out = subprocess.run(
                ["git", "config", "--global", "--get", key],
                capture_output=True, text=True, timeout=3,
            )
            return out.stdout.strip()
        except Exception:
            return ""

    name = _global("user.name") or "bot-inator"
    email = _global("user.email") or "bot-inator@local"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


# Host config that must not reach the nested repo: a signing setup can
# prompt (pinentry) or hang until the timeout, and a hooks path can run
# arbitrary host hooks or veto the commit.
_GIT_ISOLATION_ARGS: tuple[str, ...] = (
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", f"core.hooksPath={os.devnull}",
)


def _git_failure(step: str, result: "subprocess.CompletedProcess[str]") -> str:
    """Summary for a failed git step, carrying git's own stderr/stdout."""
    detail = (result.stderr or result.stdout or "").strip()
    return f"git {step} failed (exit {result.returncode}): {detail}"[:240]


def _git_commit(directory: Path, filename: str, message: str) -> tuple[bool, str]:
    """Best-effort nested-repo commit. Returns ``(ok, summary)``.

    Initialises the nested repo on first use. Never raises — git failures
    are surfaced through the returned summary string (including git's
    stderr) so the meta-tool can fold them into its own thought stream.
    Host commit signing and hooks are disabled for these commands.
    """
    if not directory.exists():
        return False, f"directory missing: {directory}"
    if not _git_available():
        return False, _GIT_NOT_INSTALLED
    try:
        def _run(*args: str) -> "subprocess.CompletedProcess[str]":
            return subprocess.run(
                ["git", *_GIT_ISOLATION_ARGS, *args],
                cwd=directory, capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )

        if not (directory / ".git").exists():
            result = _run("init", "--quiet")
            if result.returncode != 0:
                return False, _git_failure("init", result)
        ident = _git_identity_args()
        result = _run(*ident, "add", "--", filename)
        if result.returncode != 0:
            return False, _git_failure("add", result)
        result = _run(*ident, "commit", "-m", message)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "").strip()[:240]
        lines = (result.stdout or "").strip().splitlines()
        return True, (lines[0] if lines else "committed")[:240]
    except FileNotFoundError:
        return False, "git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "git command timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"git error: {exc}"


def _repair_context(installed_source: str, failure_note: str) -> str:
    """Repair-turn context (#90): the installed module and its run-time error."""
    return (
        "\n\nThis tool is already installed and now FAILS when it runs. "
        "The installed module:\n" + installed_source +
        "\n\nThe error it hit:\n- " + failure_note +
        "\n\nFix the cause of that error. Keep the tool name, its arguments "
        "and its behaviour. Do not add network access, apps or file paths the "
        "installed module does not use unless the fix needs them."
    )


def _draft_until_clean(
    engine: Any, tool_name: str, description: str, expected_logic: str,
    *, installed_source: str = "", failure_note: str = "",
) -> tuple[str, int]:
    """Draft, check, critique and repair up to :data:`MAX_DRAFT_ATTEMPTS` times.

    Each attempt runs, stopping at the first stage with problems: the
    structural checks (:func:`tools.draft_checks.check_draft`), a
    sandboxed dry run (:mod:`tools.draft_dry_run`; the test plan is made
    once and reused so every attempt faces the same expectations), and
    the logic critic (:func:`tools.draft_critic.critique`). Problems are
    fed back to the drafter for the next attempt.

    Returns ``(source, attempts)`` for the first draft with no problems.
    When attempts run out, the last draft that passed the structural
    checks is returned with the critic's remaining notes as a comment
    header, so the human reviewer sees them (the critic is advisory).
    Raises :class:`ToolGenerationError` when no draft passed the
    structural checks. Each failed draft is dumped to ``_rejected/``.
    """
    source = ""
    problems: list[str] = []
    best_clean: Optional[tuple[str, list[str]]] = None
    use_critic = critic_enabled()
    use_dry_run = dry_run_enabled()
    soft_notes: list[str] = []
    plan: Optional[DryRunPlan] = None
    plan_attempted = False
    repair: dict[str, str] = (
        {"installed_source": installed_source, "failure_note": failure_note}
        if installed_source and failure_note else {}
    )
    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        source = _draft_with_engine(
            engine, tool_name, description, expected_logic,
            previous_source=source, problems=problems, **repair,
        )
        problems = check_draft(tool_name, source, static_validator=_validate_source)
        stage = "checks"
        if not problems and use_dry_run:
            if not plan_attempted:
                plan_attempted = True
                _report_progress(f"Planning a sandboxed dry run of {tool_name}.")
                plan = plan_dry_run(engine, tool_name, description, expected_logic, source)
            if plan is not None:
                result = run_dry_run(tool_name, source, plan)
                problems = result.hard
                soft_notes = result.soft
                stage = "dry run"
                best_clean = (source, problems + soft_notes)
        if not problems and use_critic:
            _report_progress(f"Reviewing draft {attempt} of {tool_name} against its spec.")
            problems = critique(engine, tool_name, description, expected_logic, source)
            stage = "review"
            best_clean = (source, problems + soft_notes)
        if not problems:
            if attempt > 1:
                _report_progress(
                    f"Draft of {tool_name} passed after {attempt} attempts."
                )
            # Dry-run expectation mismatches are advisory: shown, not repaired.
            return with_review_notes(source, soft_notes), attempt
        reason = "; ".join(problems)
        dumped = _dump_rejected_draft(tool_name, source, f"{stage}: {reason}")
        logger.warning(
            "Draft %d/%d for %s failed %s: %s (dumped=%s)",
            attempt, MAX_DRAFT_ATTEMPTS, tool_name, stage, reason, dumped,
        )
        _report_progress(
            f"Draft {attempt}/{MAX_DRAFT_ATTEMPTS} of {tool_name} failed {stage}: "
            f"{reason}" + (" Repairing." if attempt < MAX_DRAFT_ATTEMPTS else "")
        )
    if best_clean is not None:
        clean_source, notes = best_clean
        return with_review_notes(clean_source, notes), MAX_DRAFT_ATTEMPTS
    raise ToolGenerationError(
        f"draft still failing after {MAX_DRAFT_ATTEMPTS} attempts: "
        + "; ".join(problems)
    )


def _report_progress(message: str) -> None:
    """Best-effort ``tool_result`` thought so the UI shows repair progress."""
    orch = _ORCHESTRATOR
    loop = getattr(getattr(orch, "event_bus", None), "loop", None)
    if orch is None or loop is None:
        return
    event_id = getattr(orch, "_current_event_id", "evt_unknown")
    try:
        asyncio.run_coroutine_threadsafe(
            orch._emit_thought(event_id, "tool_result", message), loop,
        )
    except Exception:
        logger.debug("progress report failed", exc_info=True)


@tool
def generate_custom_tool(
    tool_name: str,
    description: str,
    expected_logic: str,
) -> dict[str, Any]:
    """Draft a new smolagents @tool module, request user review, and install it.

    Call this when the user's request needs a capability that is not
    covered by any existing tool and that a small Python module can
    provide (timers, calculators, file utilities, stdlib HTTP calls,
    etc.). Also appropriate when the pattern recognizer has proposed a
    concrete automation. Do NOT call this for conversational turns that
    only need a direct text answer. Never call it to quit or close an app,
    take a screenshot, or list or read emails: a generated tool runs in a
    sandbox that cannot do those, so call the built-ins ``quit_app``,
    ``take_screenshot``, ``list_recent_emails`` and ``read_email`` instead.
    The function drives the main MLX
    engine to write a stdlib-only Python module, runs static safety
    checks on the draft, asks the user to review (and optionally edit)
    the source via a ``code_approval_request`` frame, atomically writes
    the approved source into ``agent-daemon/tools/generated/<tool_name>.py``,
    commits the file to the nested git repo, and loads it into the active
    smolagents agent.

    Args:
        tool_name: Snake-case identifier for the new tool function
            (e.g. ``start_timer``, ``summarize_file``).
        description: One-sentence summary of what the tool does.
        expected_logic: Short paragraph describing the intended behaviour,
            including inputs, outputs, and any edge cases the tool must
            handle.

    Returns:
        A dict with ``status`` ("ok", "denied", "error"), ``tool_name``,
        and — on success — a ``path`` pointing to the installed module.
    """
    if _ORCHESTRATOR is None or _ENGINE is None or _GENERATED_DIR is None:
        return {
            "status": "error",
            "tool_name": tool_name,
            "error": "meta-tool context not initialised",
        }
    try:
        _validate_identifier(tool_name)
    except ToolGenerationError as exc:
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}

    # Name-collision guard (todo #24): a generated tool with this exact
    # name already exists (on disk or loaded). Reject before any drafting
    # or approval work so the agent picks a new name or the user deletes
    # the old tool first. Runs before the similarity guard, which would
    # otherwise route an exact-name match to the old tool silently.
    existing = _existing_generated_tool(tool_name)
    if existing is not None:
        return _name_collision_error(tool_name, existing)

    # Semantic-duplicate guard: if an existing tool's name/description
    # shares a significant keyword with the requested one (e.g. a pattern
    # recognizer proposal for ``auto_screenshot`` when ``screenshot_tool``
    # already exists), skip drafting and route the turn to the existing
    # tool via the auto-rerun flag. Exact-name collisions are rejected
    # earlier by the name-collision check.
    similar = _find_similar_existing_tool(tool_name, description)
    if similar:
        _mark_for_rerun(similar)
        return {
            "status": "similar_exists",
            "tool_name": tool_name,
            "existing_tool": similar,
            "message": (
                f"The existing tool {similar!r} already covers this task. "
                "Do NOT create a duplicate. Emit final_answer acknowledging "
                f"this; the orchestrator will invoke {similar!r} "
                "automatically."
            ),
        }

    try:
        source, attempts = _draft_until_clean(
            _DRAFT_ENGINE or _ENGINE, tool_name, description, expected_logic,
        )
    except ToolGenerationError as exc:
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    except Exception as exc:
        logger.exception("Meta-tool draft failed")
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}

    response = _request_code_approval(tool_name, description, source)
    if not response.approved:
        return {
            "status": "denied",
            "tool_name": tool_name,
            "user_note": response.user_note,
        }

    final_source = response.edited_code or source
    if response.edited_code:
        edit_problems = check_draft(
            tool_name, final_source, static_validator=_validate_source,
        )
        if edit_problems:
            return {
                "status": "error",
                "tool_name": tool_name,
                "error": "user-edited draft rejected: " + "; ".join(edit_problems),
            }

    target_path = _GENERATED_DIR / f"{tool_name}.py"
    try:
        _atomic_write(target_path, final_source)
    except Exception as exc:
        logger.exception("Failed to install generated tool")
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    _save_provenance(
        tool_name, description, expected_logic, bool(response.edited_code),
        _plain_summary(tool_name, description, final_source),
    )

    git_ok, git_summary = _git_commit(
        _GENERATED_DIR,
        f"{tool_name}.py",
        f"Auto-generated tool: {tool_name}",
    )
    if not git_ok:
        logger.warning("Nested git commit failed for %s: %s", tool_name, git_summary)

    try:
        _ORCHESTRATOR.load_dynamic_tools()
    except Exception as exc:
        logger.exception("load_dynamic_tools failed after install")
        return {
            "status": "error",
            "tool_name": tool_name,
            "error": f"installed but failed to load: {exc}",
        }

    # The running agent's tool schema was frozen at the start of this
    # run, so ``tool_name`` is NOT yet callable in the same turn even
    # though the file is on disk and the next run's agent will see it.
    # Flag the orchestrator for an auto-rerun with the original prompt
    # so the newly-installed tool is invoked transparently. The message
    # instructs the model to finish cleanly with final_answer.
    _mark_for_rerun(tool_name)
    return {
        "status": "ok",
        "tool_name": tool_name,
        "path": str(target_path),
        "git": git_summary,
        "git_ok": git_ok,
        "draft_attempts": attempts,
        "message": (
            f"Tool {tool_name!r} was drafted, approved, and installed. "
            "Immediately emit final_answer with a brief confirmation; "
            "the orchestrator will automatically invoke the new tool "
            "to fulfil the original request."
        ),
    }


def _mark_for_rerun(tool_name: str) -> None:
    """Best-effort signal to the orchestrator to auto-rerun the turn."""
    if _ORCHESTRATOR is None:
        return
    mark = getattr(_ORCHESTRATOR, "mark_tool_ready_for_rerun", None)
    if callable(mark):
        try:
            mark(tool_name)
        except Exception:
            logger.exception("mark_tool_ready_for_rerun failed")


def _request_code_approval(
    tool_name: str, description: str, source: str,
    *, previous_code: Optional[str] = None, notice: str = "",
):
    """Cross the async boundary to request review from the UI.

    A repair (#90) passes the installed source as ``previous_code`` (the
    card shows a diff) and a ``notice`` that leads the summary (the error
    behind the repair and anything the new code newly touches).
    """
    assert _ORCHESTRATOR is not None
    loop = _ORCHESTRATOR.event_bus.loop
    if loop is None:
        raise ToolGenerationError("event-loop not bound to orchestrator")
    extra: dict[str, Any] = {}
    summary = _plain_summary(tool_name, description, source)
    if notice:
        summary = notice + ("\n\n" + summary if summary else "")
    if summary:
        extra["summary"] = summary
    if previous_code is not None and _accepts_kwarg(
        _ORCHESTRATOR.request_code_approval, "previous_code",
    ):
        extra["previous_code"] = previous_code
    coro = _ORCHESTRATOR.request_code_approval(
        tool_name=tool_name, description=description, code=source, **extra,
    )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=_ORCHESTRATOR.code_approval_timeout + 5)


def _accepts_kwarg(fn: Any, name: str) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


# ---- repair an installed tool (#90) -----------------------------------------
#
# OFFER, NEVER AUTO-FIX: ``repair_tool`` drafts a fix through the same
# checks, dry run and critic as a new tool (``_draft_until_clean``), then
# asks the code card with the installed source as ``previous_code``. Nothing
# is written or committed before the card is approved; a denial leaves the
# file and the nested repo's history as they were. Every repair is a commit,
# and ``restore_previous_version`` (a user click in Tool detail) puts the
# file's prior committed version back.


def widening(previous: str, new: str) -> list[str]:
    """What ``new`` touches that ``previous`` did not, as card sentences:
    an added network import, new apps, new paths (#90 decision 4)."""
    from ai import tool_summary

    before = tool_summary.touches_from_source(previous)
    after = tool_summary.touches_from_source(new)
    out: list[str] = []
    if after["network"] and not before["network"]:
        out.append(f"This repair ADDS network access ({after['network']}).")
    apps = [a for a in after["apps"] if a not in before["apps"]]
    if apps:
        out.append("This repair controls apps the tool did not before: " + ", ".join(apps) + ".")
    paths = [p for p in after["paths"] if p not in before["paths"]]
    if paths:
        out.append("This repair touches paths the tool did not before: " + ", ".join(paths) + ".")
    if after["writes_files"] and not before["writes_files"]:
        out.append("This repair can change files; the installed tool could not.")
    return out


def _failure_note(failure: dict[str, Any]) -> str:
    cls = str(failure.get("error_class") or "ToolError")
    message = str(failure.get("message") or "").strip()
    note = message if message.startswith(cls) else f"{cls}: {message}".rstrip(": ")
    try:
        from ai import privacy

        # A cloud drafting engine gets the error class and message only
        # (argument values were never stored), with known personal text
        # scrubbed (#31/#51/#52).
        note = privacy.scrub_idea_text(note, "drafting")
    except Exception:
        logger.debug("failure note scrub failed", exc_info=True)
    return note


def _spec_for(tool_name: str, source: str) -> tuple[str, str]:
    """``(description, expected_logic)`` from the provenance, else the docstring."""
    import json

    assert _GENERATED_DIR is not None
    try:
        data = json.loads(provenance_path(_GENERATED_DIR, tool_name).read_text(encoding="utf-8"))
        requested = data.get("requested") or {}
        desc = str(requested.get("description") or "").strip()
        logic = str(requested.get("expected_logic") or "").strip()
        if desc or logic:
            return desc or logic, logic or desc
    except (OSError, ValueError, AttributeError):
        pass
    try:
        tree = ast.parse(source)
        funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        funcs.sort(key=lambda n: n.name != tool_name)
        doc = (ast.get_docstring(funcs[0]) if funcs else None) or ast.get_docstring(tree) or ""
    except SyntaxError:
        doc = ""
    doc = doc.strip() or f"The installed tool {tool_name}."
    return doc.splitlines()[0].strip(), doc


def _note_repair_in_provenance(tool_name: str, failure: dict[str, Any], user_edited: bool) -> None:
    import json
    from datetime import datetime, timezone

    if _GENERATED_DIR is None:
        return
    path = provenance_path(_GENERATED_DIR, tool_name)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
        repairs = data.setdefault("repairs", [])
        repairs.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error_class": failure.get("error_class"),
            "user_edited": bool(user_edited),
        })
        del repairs[:-20]
        _atomic_write(path, json.dumps(data, indent=2, default=str))
    except Exception:
        logger.exception("Failed to note the repair of %s in provenance", tool_name)


def _health_log() -> Any:
    return getattr(_ORCHESTRATOR, "tool_health", None)


def repair_tool(tool_name: str, failure: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Draft, review and (on approval) install a fix for an installed tool.

    ``failure`` is ``{"error_class", "message"}`` (as recorded by
    :mod:`ai.tool_health`); ``None`` uses the tool's latest recorded
    failure. Returns ``status`` "ok" | "denied" | "not_repairable" | "error".
    """
    from ai import tool_health

    if _ORCHESTRATOR is None or _ENGINE is None or _GENERATED_DIR is None:
        return {"status": "error", "tool_name": tool_name,
                "error": "meta-tool context not initialised"}
    try:
        _validate_identifier(tool_name)
    except ToolGenerationError as exc:
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    path = _GENERATED_DIR / f"{tool_name}.py"
    if (_GENERATED_DIR / "disabled" / f"{tool_name}.py").exists() and not path.exists():
        return {"status": "error", "tool_name": tool_name, "error": "tool is disabled"}
    if not path.is_file():
        return {"status": "error", "tool_name": tool_name,
                "error": "no installed generated tool with that name"}
    if failure is None:
        log = _health_log()
        failure = log.latest_failure(tool_name) if log is not None else None
    if not failure:
        return {"status": "error", "tool_name": tool_name,
                "error": "no recorded failure to repair"}
    message = str(failure.get("message") or "")
    cls = str(failure.get("error_class") or tool_health.error_class_of(message))
    repairable, category, reason = tool_health.classify_failure(message, cls)
    if not repairable:
        return {"status": "not_repairable", "tool_name": tool_name,
                "category": category, "reason": reason}
    failure = {**failure, "error_class": cls, "message": message}

    installed = path.read_text(encoding="utf-8")
    description, expected_logic = _spec_for(tool_name, installed)
    note = _failure_note(failure)
    _report_progress(f"Drafting a repair of {tool_name} for: {note}")
    try:
        source, attempts = _draft_until_clean(
            _DRAFT_ENGINE or _ENGINE, tool_name, description, expected_logic,
            installed_source=installed, failure_note=note,
        )
    except ToolGenerationError as exc:
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    except Exception as exc:
        logger.exception("Repair draft failed")
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    if source.strip() == installed.strip():
        return {"status": "error", "tool_name": tool_name,
                "error": "the repair draft is identical to the installed source"}

    widened = widening(installed, source)
    notice = "\n".join(
        [f"⚠ {line}" for line in widened]
        + [f"REPAIR of the installed tool {tool_name}. It failed with: {message or cls}",
           "Below: the changes against the installed version. Deny keeps the tool as it is."]
    )
    response = _request_code_approval(
        tool_name, description, source, previous_code=installed, notice=notice,
    )
    if not response.approved:
        return {"status": "denied", "tool_name": tool_name,
                "user_note": response.user_note}

    final_source = response.edited_code or source
    if response.edited_code:
        edit_problems = check_draft(tool_name, final_source, static_validator=_validate_source)
        if edit_problems:
            return {"status": "error", "tool_name": tool_name,
                    "error": "user-edited draft rejected: " + "; ".join(edit_problems)}
    try:
        _atomic_write(path, final_source)
    except Exception as exc:
        logger.exception("Failed to write the repaired tool")
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    _note_repair_in_provenance(tool_name, failure, bool(response.edited_code))
    git_ok, git_summary = _git_commit(
        _GENERATED_DIR, f"{tool_name}.py", f"Repair tool: {tool_name} ({cls})",
    )
    if not git_ok:
        logger.warning("Nested git commit failed for repair of %s: %s", tool_name, git_summary)
    log = _health_log()
    if log is not None:
        log.note_change(tool_name, "repaired")
    try:
        _ORCHESTRATOR.load_dynamic_tools()
    except Exception as exc:
        logger.exception("load_dynamic_tools failed after repair")
        return {"status": "error", "tool_name": tool_name,
                "error": f"repaired but failed to load: {exc}"}
    return {
        "status": "ok", "tool_name": tool_name, "path": str(path),
        "git": git_summary, "git_ok": git_ok, "draft_attempts": attempts,
        "widened": widened,
        "message": (
            f"Tool {tool_name!r} was repaired and reloaded. Emit final_answer "
            "with a brief confirmation; do not call the tool in this turn."
        ),
    }


@tool
def repair_generated_tool(tool_name: str) -> dict[str, Any]:
    """Draft a fix for an installed generated tool that keeps failing, and ask the user to review it.

    Call this only when an idea asks to repair a tool. The fix is drafted
    from the tool's recorded failure and its original spec, checked, dry
    run, and shown to the user as a diff against the installed code;
    nothing changes unless the user approves.

    Args:
        tool_name: Name of the installed generated tool to repair.

    Returns:
        dict with ``status`` ("ok", "denied", "not_repairable", "error") and ``tool_name``.
    """
    return repair_tool(tool_name)


def _git_read(directory: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    if not _git_available():
        return subprocess.CompletedProcess(["git", *args], 1, "", _GIT_NOT_INSTALLED)
    return subprocess.run(
        ["git", *_GIT_ISOLATION_ARGS, *args], cwd=directory, capture_output=True,
        text=True, timeout=10, stdin=subprocess.DEVNULL,
    )


def previous_version(generated_dir: Path, tool_name: str) -> Optional[tuple[str, str]]:
    """``(commit, source)``: the newest committed version of
    ``<tool_name>.py`` that differs from the file on disk, or ``None``."""
    generated_dir = Path(generated_dir)
    path = generated_dir / f"{tool_name}.py"
    if not path.is_file() or not (generated_dir / ".git").exists():
        return None
    current = path.read_text(encoding="utf-8").strip()
    try:
        log = _git_read(generated_dir, "log", "--format=%H", "-n", "50", "--", f"{tool_name}.py")
        if log.returncode != 0:
            return None
        for sha in log.stdout.split():
            shown = _git_read(generated_dir, "show", f"{sha}:{tool_name}.py")
            if shown.returncode == 0 and shown.stdout.strip() != current:
                return sha, shown.stdout
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("reading the history of %s failed", tool_name)
    return None


def restore_previous_version(
    tool_name: str, *, generated_dir: Optional[Path] = None, orchestrator: Any = None,
) -> dict[str, Any]:
    """Put back the file's prior committed version, commit, reload (#90).

    A user click in Tool detail, no card: the prior version was reviewed
    when it was installed. Returns ``status`` "ok" | "error".
    """
    gen = Path(generated_dir) if generated_dir is not None else _GENERATED_DIR
    orch = orchestrator if orchestrator is not None else _ORCHESTRATOR
    if gen is None:
        return {"status": "error", "tool_name": tool_name, "error": "no generated tools dir"}
    try:
        _validate_identifier(tool_name)
    except ToolGenerationError as exc:
        return {"status": "error", "tool_name": tool_name, "error": str(exc)}
    found = previous_version(gen, tool_name)
    if found is None:
        return {"status": "error", "tool_name": tool_name, "error": "no previous version"}
    sha, source = found
    path = gen / f"{tool_name}.py"
    _atomic_write(path, source)
    git_ok, git_summary = _git_commit(
        gen, f"{tool_name}.py", f"Restore previous version: {tool_name} ({sha[:7]})",
    )
    log = getattr(orch, "tool_health", None)
    if log is not None:
        log.note_change(tool_name, "restored")
    loaded = None
    if orch is not None and hasattr(orch, "load_dynamic_tools"):
        try:
            loaded = tool_name in orch.load_dynamic_tools()
        except Exception as exc:
            logger.exception("load_dynamic_tools failed after restore")
            return {"status": "error", "tool_name": tool_name,
                    "error": f"restored but failed to load: {exc}"}
    return {"status": "ok", "tool_name": tool_name, "restored_commit": sha,
            "git_ok": git_ok, "git": git_summary, "loaded": loaded}
