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
* Built-in tools live under `tools.<name>` (e.g. `from tools.show_notification import show_notification`).
* Previously-generated tools live under `tools.generated.<name>` (e.g. `from tools.generated.fetch_weather import fetch_weather`).
* Imported tools are invoked as normal Python callables: `show_notification(title="Done", message="...")`. Check each tool's description for its arguments before calling it.
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
            lambda: show_notification(title=title, message=message),
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
    for banned in _BANNED_SUBSTRINGS:
        if banned in source:
            raise ToolGenerationError(f"draft contains banned token: {banned}")
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


def _format_available_tools_context() -> str:
    """Render the ``AVAILABLE TOOLS`` section for the drafter's system prompt.

    Enumerates every tool registered on the orchestrator — both the
    static built-ins and any previously-generated tools — annotating
    each with its import path so the drafter can compose against them.
    Falls back to a short note when the orchestrator is unavailable
    (e.g. in unit tests).
    """
    if _ORCHESTRATOR is None:
        return "AVAILABLE TOOLS\n(unavailable in this context)\n"

    builtin_names = getattr(_ORCHESTRATOR, "_builtin_tool_names", frozenset())
    dynamic_names = getattr(_ORCHESTRATOR, "_dynamic_tool_names", set())

    builtin_lines: list[str] = []
    dynamic_lines: list[str] = []
    for tool_obj in getattr(_ORCHESTRATOR, "tools", []):
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
        if name in builtin_names:
            path = f"tools.{name}"
            builtin_lines.append(
                f"- {name}  (import: from {path} import {name})\n"
                f"    {first_line[:220]}"
            )
        elif name in dynamic_names:
            path = f"tools.generated.{name}"
            dynamic_lines.append(
                f"- {name}  (import: from {path} import {name})\n"
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
    raw = engine.generate_chat_sync(
        system_prompt, user_msg, max_tokens=1200
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


def _dup_tokens(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords + short tokens."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return {t for t in raw if t and len(t) > 2 and t not in _DUP_STOPWORDS}


def _find_similar_existing_tool(
    tool_name: str, description: str
) -> Optional[str]:
    """Return an existing tool name that likely covers the requested task.

    Matches when any significant token from the requested ``tool_name``
    or ``description`` also appears in an existing tool's name or
    description. Used as a semantic-duplicate guard in
    :func:`generate_custom_tool` — exact-name duplicates are caught
    earlier by the on-disk path check.
    """
    if _ORCHESTRATOR is None:
        return None
    request_tokens = _dup_tokens(tool_name) | _dup_tokens(description)
    if not request_tokens:
        return None

    for tool_obj in getattr(_ORCHESTRATOR, "tools", []):
        name = getattr(tool_obj, "name", getattr(tool_obj, "__name__", ""))
        if not name:
            continue
        desc = (
            getattr(tool_obj, "description", None)
            or getattr(tool_obj, "__doc__", None)
            or ""
        )
        existing_tokens = _dup_tokens(name) | _dup_tokens(desc)
        if request_tokens & existing_tokens:
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

    source = ""
    try:
        source = _draft_with_engine(
            _DRAFT_ENGINE or _ENGINE,
            tool_name, description, expected_logic,
        )
        _validate_source(tool_name, source)
    except ToolGenerationError as exc:
        dumped = _dump_rejected_draft(tool_name, source, str(exc))
        head = "\n".join(source.splitlines()[:10]) if source else "(empty)"
        logger.warning(
            "Meta-tool draft rejected for %s: %s\n  dumped=%s\n  first lines:\n%s",
            tool_name, exc, dumped, head,
        )
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
        try:
            _validate_source(tool_name, final_source)
        except ToolGenerationError as exc:
            return {
                "status": "error",
                "tool_name": tool_name,
                "error": f"user-edited draft rejected: {exc}",
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
