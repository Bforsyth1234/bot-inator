"""AI-driven meta-tool that drafts, reviews, and installs new @tool modules."""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
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
) -> str:
    """Drive the configured drafting engine to write a @tool module synchronously.

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
_DUP_EXEMPT_TOOLS: frozenset[str] = frozenset({"generate_custom_tool"})


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


def _git_identity_args() -> list[str]:
    """Return ``-c user.name=... -c user.email=...`` for the nested commit.

    Prefers the host's global git identity so the agent's commits blend with
    the user's normal history. Falls back to a synthetic ``bot-inator``
    identity when no global config is set (fresh machine, CI, etc.).
    """
    def _global(key: str) -> str:
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


def _git_commit(directory: Path, filename: str, message: str) -> tuple[bool, str]:
    """Best-effort nested-repo commit. Returns ``(ok, summary)``.

    Initialises the nested repo on first use. Never raises — git failures
    are surfaced through the returned summary string so the meta-tool can
    fold them into its own thought stream.
    """
    if not directory.exists():
        return False, f"directory missing: {directory}"
    try:
        if not (directory / ".git").exists():
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=directory, check=True, capture_output=True,
                text=True, timeout=10,
            )
        ident = _git_identity_args()
        subprocess.run(
            ["git", *ident, "add", filename],
            cwd=directory, check=True, capture_output=True,
            text=True, timeout=10,
        )
        result = subprocess.run(
            ["git", *ident, "commit", "-m", message],
            cwd=directory, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "").strip()[:240]
        return True, (result.stdout or "").strip().splitlines()[0][:240]
    except FileNotFoundError:
        return False, "git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "git command timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"git error: {exc}"


def _draft_until_clean(
    engine: Any, tool_name: str, description: str, expected_logic: str,
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
    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        source = _draft_with_engine(
            engine, tool_name, description, expected_logic,
            previous_source=source, problems=problems,
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
    only need a direct text answer. The function drives the main MLX
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

    # Semantic-duplicate guard: if an existing tool's name/description
    # shares a significant keyword with the requested one (e.g. a pattern
    # recognizer proposal for ``auto_screenshot`` when ``screenshot_tool``
    # already exists), skip drafting and route the turn to the existing
    # tool via the auto-rerun flag. The exact-name path below still
    # handles identical names for the cheapest short-circuit.
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

    # Skip drafting if a tool with this name already lives on disk. The
    # orchestrator hot-loads ``tools/generated/*.py`` at startup, so the
    # tool is already callable. Flag an auto-rerun so the orchestrator
    # transparently uses it against the original prompt.
    existing_path = _GENERATED_DIR / f"{tool_name}.py"
    if existing_path.exists():
        loaded = sorted(getattr(_ORCHESTRATOR, "_dynamic_tool_names", set()))
        _mark_for_rerun(tool_name)
        return {
            "status": "exists",
            "tool_name": tool_name,
            "path": str(existing_path),
            "loaded_tools": loaded,
            "message": (
                f"Tool {tool_name!r} already exists. Emit final_answer "
                "acknowledging this; the orchestrator will invoke it "
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
):
    """Cross the async boundary to request review from the UI."""
    assert _ORCHESTRATOR is not None
    loop = _ORCHESTRATOR.event_bus.loop
    if loop is None:
        raise ToolGenerationError("event-loop not bound to orchestrator")
    coro = _ORCHESTRATOR.request_code_approval(
        tool_name=tool_name, description=description, code=source,
    )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=_ORCHESTRATOR.code_approval_timeout + 5)
