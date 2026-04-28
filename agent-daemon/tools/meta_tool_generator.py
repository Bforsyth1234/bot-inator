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
    from ai.mlx_engine import MLXEngine
    from ai.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Populated by :func:`set_meta_tool_context` at lifespan startup.
_ORCHESTRATOR: "Optional[Orchestrator]" = None
_ENGINE: "Optional[MLXEngine]" = None
# Code-specialist engine used exclusively for drafting. Falls back to
# ``_ENGINE`` when the lifespan didn't wire a dedicated drafting model.
_DRAFT_ENGINE: "Optional[MLXEngine]" = None
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
_BANNED_SUBSTRINGS: tuple[str, ...] = (
    "__import__(", "eval(", "exec(", "compile(",
    "os.system(", "os.popen(", "pty.spawn(",
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

Composition rules:
* Prefer composing the BUILT-IN TOOLS below over reinventing their functionality. If a built-in does what you need, import and call it directly rather than re-implementing.
* Do not duplicate built-in tool names. The orchestrator will reject drafts whose name collides with a built-in.
"""


class ToolGenerationError(RuntimeError):
    """Raised when the draft fails static validation or the user denies."""


def set_meta_tool_context(
    *,
    orchestrator: "Orchestrator",
    engine: "MLXEngine",
    generated_dir: Path,
    drafting_engine: "Optional[MLXEngine]" = None,
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
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ToolGenerationError(f"draft is not valid Python: {exc}") from exc

    allowed = _ALLOWED_STDLIB | _ALLOWED_THIRD_PARTY
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


def _decorator_name(dec: ast.expr) -> str:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return f"{getattr(dec.value, 'id', '')}.{dec.attr}".lstrip(".")
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    return ""


def _format_builtin_tools_context() -> str:
    """Render a concise ``BUILT-IN TOOLS`` section for the system prompt.

    Pulls ``name`` + ``description`` from every built-in tool currently
    registered on the orchestrator so the drafter knows what it can
    compose with. Falls back to a short note when the orchestrator is
    unavailable (e.g. in unit tests).
    """
    if _ORCHESTRATOR is None:
        return "BUILT-IN TOOLS\n(unavailable in this context)\n"

    builtin_names = getattr(_ORCHESTRATOR, "_builtin_tool_names", frozenset())
    lines: list[str] = ["BUILT-IN TOOLS (prefer composing these; do not duplicate their names):"]
    for tool_obj in getattr(_ORCHESTRATOR, "tools", []):
        name = getattr(tool_obj, "name", getattr(tool_obj, "__name__", ""))
        if not name or name not in builtin_names:
            continue
        desc = (
            getattr(tool_obj, "description", None)
            or getattr(tool_obj, "__doc__", None)
            or ""
        )
        first_line = desc.strip().splitlines()[0] if desc.strip() else "(no description)"
        lines.append(f"- {name}: {first_line[:180]}")
    if len(lines) == 1:
        lines.append("(no built-in tools registered)")
    return "\n".join(lines) + "\n"


def _draft_with_engine(
    engine: "MLXEngine",
    tool_name: str,
    description: str,
    expected_logic: str,
) -> str:
    """Drive the main engine to write a @tool module synchronously.

    Called from the smolagents agent worker thread (spawned by
    ``asyncio.to_thread`` in :meth:`Orchestrator._run_agent`), so we drop
    into the engine's sync API directly — this routes through the shared
    :attr:`MLXEngine.generation_lock`, serializing against the agent's own
    inference loop instead of racing it on the Metal heap.
    """
    system_prompt = (
        _SYSTEM_PROMPT_BASE + "\n" + _format_builtin_tools_context()
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
