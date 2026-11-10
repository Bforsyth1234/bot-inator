"""Plain-language summary of a suggested tool (todo #64).

When ``generate_custom_tool`` is about to ask the user (the idea card) and
again when the drafted code is reviewed, the card leads with 2-4 short
sentences: what the tool does for the user, when it would run, what it
touches (files, apps, network) and whether each use asks first.

* **Idea card.** One call to the analysis engine from the idea text
  (``tool_name``, ``description``, ``expected_logic``), answered as three
  labelled lines (``DOES`` / ``WHEN`` / ``TOUCHES``). Any failure (the
  engine raises, is not loaded, takes longer than
  :data:`SUMMARY_TIMEOUT_SECONDS`, or returns junk) gives the
  deterministic :func:`fallback_summary` built from ``description``. The
  approval is never blocked on the summary.
* **Code-review card.** "What it touches" is read from the drafted source
  (:func:`touches_from_source`), not from the model: network from the
  sandbox's own import check (``tools.tool_sandbox.network_reason``), apps
  from ``osascript`` / ``open`` / ``shortcuts`` usage, folders from path
  literals.
* **Privacy (#31/#51/#52).** The prompt goes through
  ``ai.privacy.scrub_idea_text`` for the ``"analysis"`` role, so a cloud
  analysis engine never sees personal text an idea quoted.

The "asks first" sentence is never the model's: it comes from
``tools.READ_ONLY_TOOL_NAMES`` (invariant 7a).
"""
from __future__ import annotations

import ast
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

from . import privacy
from .mlx_engine import THINK_BLOCK_RE

logger = logging.getLogger(__name__)

# The engine role whose provider decides redaction: the orchestrator's
# ``analysis_engine`` writes the summary.
SUMMARY_ROLE = "analysis"
SUMMARY_TIMEOUT_SECONDS = 5.0
SUMMARY_MAX_TOKENS = 160
_PART_MAX_CHARS = 220
_REMEMBERED_MAX = 32

SUMMARY_SYSTEM_PROMPT = (
    "An assistant on the user's Mac wants to build a small automation tool. "
    "Describe the tool to the user, who is not a programmer. Answer with "
    "exactly three lines and nothing else:\n"
    "DOES: one short sentence saying what the tool does for the user.\n"
    "WHEN: one short sentence saying when it would run.\n"
    "TOUCHES: the folders, files, apps or internet services it would use, "
    "as a short phrase (write 'nothing else on your Mac' if none).\n"
    "Use plain everyday words. No code, no function or parameter names, no "
    "markdown, and do not quote messages or emails."
)

ASKS_FIRST = "Each use asks for your approval first."
RUNS_FREELY = "It only reads, so it runs without asking."
FALLBACK_WHEN = (
    "It would run when you ask for it in chat or when the assistant sees a "
    "moment it fits."
)
FALLBACK_TOUCHES = (
    "You will see what it touches when its code is ready for review."
)

_LINE_RE = re.compile(
    r"^\W*(DOES|WHEN|TOUCHES)\W*[:\-–]\s*(.+?)\s*$", re.IGNORECASE
)
_SNAKE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_MARKUP_RE = re.compile(r"[`*#]+|\(\s*\)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_LOWERCASE_FIRST = frozenset({"The", "Your", "Nothing", "Only", "Files", "A", "An", "No", "Just"})


@dataclass(frozen=True)
class ToolSummary:
    """The parts of one summary. ``source`` is ``"engine"`` or ``"fallback"``."""

    tool_name: str
    description: str
    does: str
    when: str
    touches: str  # a full sentence
    source: str = "fallback"

    def text(self, touches: Optional[str] = None) -> str:
        """The summary shown to the user; ``touches`` replaces that sentence."""
        parts = [self.does, self.when, touches or self.touches, _asks_sentence(self.tool_name)]
        return " ".join(p for p in parts if p)


# ---- text helpers ----------------------------------------------------------


def _asks_sentence(tool_name: str) -> str:
    try:
        from tools import READ_ONLY_TOOL_NAMES

        read_only = tool_name in READ_ONLY_TOOL_NAMES
    except Exception:  # pragma: no cover - defensive
        read_only = False
    return RUNS_FREELY if read_only else ASKS_FIRST


def _plain(text: Any, tool_name: str = "") -> str:
    """One clean sentence-ish string: no markup, no code identifiers."""
    out = str(text or "")
    if tool_name:
        out = re.sub(rf"\b{re.escape(tool_name)}\b", "this tool", out)
    out = _MARKUP_RE.sub("", out)
    out = _SNAKE_RE.sub(lambda m: m.group(0).replace("_", " "), out)
    out = " ".join(out.split()).strip(" \"'“”")
    if len(out) > _PART_MAX_CHARS:
        cut = out[:_PART_MAX_CHARS]
        out = cut[: cut.rfind(" ")] if " " in cut else cut
        out = out.rstrip(" ,;:") + "…"
    return out


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?…" else text + "."


def _words_of(tool_name: str) -> str:
    return " ".join(str(tool_name or "").replace("_", " ").split())


def fallback_summary(tool_name: str, description: str) -> ToolSummary:
    """Deterministic summary from ``description`` (no model)."""
    does = _plain(description, tool_name)
    if does:
        # Keep the first two sentences of a long description.
        does = " ".join(_SENTENCE_END_RE.split(does)[:2])
    else:
        name = _words_of(tool_name)
        does = f"A new tool: {name}" if name else "A new tool for the assistant"
    return ToolSummary(
        tool_name=str(tool_name or ""),
        description=str(description or ""),
        does=_sentence(does),
        when=FALLBACK_WHEN,
        touches=FALLBACK_TOUCHES,
        source="fallback",
    )


# ---- engine ----------------------------------------------------------------


def build_prompt(tool_name: str, description: str, expected_logic: str) -> str:
    """The user half of the summary prompt, as the analysis engine may see it.

    Under a cloud analysis engine, personal text the idea quoted is replaced
    by ``privacy.scrub_idea_text`` (#51) unless the user opted in (#52).
    """
    desc = privacy.scrub_idea_text(str(description or ""), SUMMARY_ROLE)
    logic = privacy.scrub_idea_text(str(expected_logic or ""), SUMMARY_ROLE)
    return (
        f"Tool name: {_words_of(tool_name)}\n"
        f"What it should do: {desc}\n"
        f"How it should work: {logic}"
    )


def parse_engine_output(raw: Any, tool_name: str, description: str) -> Optional[ToolSummary]:
    """A :class:`ToolSummary` from the engine's three lines, or ``None`` for junk."""
    text = THINK_BLOCK_RE.sub("", str(raw or "")).strip()
    if not text or "<think>" in text:
        return None
    parts: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE_RE.match(line)
        if match and match.group(1).upper() not in parts:
            parts[match.group(1).upper()] = _plain(match.group(2), tool_name)
    does = parts.get("DOES", "")
    if len(does.split()) < 3:
        return None
    base = fallback_summary(tool_name, description)
    when = parts.get("WHEN", "")
    touches = parts.get("TOUCHES", "").rstrip(".")
    if touches.split(" ")[0] in _LOWERCASE_FIRST:
        touches = touches[0].lower() + touches[1:]
    return ToolSummary(
        tool_name=base.tool_name,
        description=base.description,
        does=_sentence(does),
        when=_sentence(when) if len(when.split()) >= 3 else base.when,
        touches=f"It would touch {touches}." if touches else base.touches,
        source="engine",
    )


def _call_engine(engine: Any, prompt: str, timeout: float) -> str:
    """``engine.generate_chat_sync`` in a helper thread, bounded by ``timeout``.

    Raises on error or timeout. An engine that is not loaded is not asked:
    loading a model would delay the approval card far longer than a summary
    is worth.
    """
    if engine is None or getattr(engine, "loaded", True) is False:
        raise RuntimeError("summary engine not loaded")
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["out"] = engine.generate_chat_sync(
                SUMMARY_SYSTEM_PROMPT, prompt, SUMMARY_MAX_TOKENS
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            box["err"] = exc

    worker = threading.Thread(target=run, name="tool-summary", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"summary engine took longer than {timeout}s")
    if "err" in box:
        raise box["err"]
    return str(box.get("out") or "")


def summarize_idea(
    engine: Any,
    tool_name: str,
    description: str,
    expected_logic: str = "",
    *,
    timeout: Optional[float] = None,
) -> ToolSummary:
    """Summary for the idea card. Blocking (call from a worker thread);
    never raises and never takes much longer than ``timeout``."""
    try:
        raw = _call_engine(
            engine,
            build_prompt(tool_name, description, expected_logic),
            SUMMARY_TIMEOUT_SECONDS if timeout is None else timeout,
        )
        parsed = parse_engine_output(raw, tool_name, description)
        if parsed is not None:
            return parsed
        logger.info("Tool summary for %s: engine output unusable, using fallback", tool_name)
    except Exception as exc:
        logger.info("Tool summary for %s: %s; using fallback", tool_name, exc)
    return fallback_summary(tool_name, description)


# ---- what the drafted code touches -----------------------------------------

_TELL_APP_RE = re.compile(r'tell\s+application\s+(?:id\s+)?\\?"([^"\\]{1,60})\\?"', re.IGNORECASE)
_PATH_RE = re.compile(r"^(~|/Users|/tmp|/private|/var|/Applications|/Library|/Volumes)(/[^\n]{0,80})?$")
_HOME_FOLDERS = ("Desktop", "Downloads", "Documents", "Pictures", "Movies", "Music")
_WRITE_CALLS = frozenset({
    "write_text", "write_bytes", "unlink", "rename", "replace", "rmdir", "mkdir",
    "makedirs", "remove", "move", "copy", "copy2", "copyfile", "copytree", "rmtree",
    "touch",
})
_MAX_LISTED = 4


def _const_strings(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _add(items: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in items:
        items.append(value)


def touches_from_source(source: str) -> dict[str, Any]:
    """What a drafted module touches, read from its source (no model).

    ``{"network": "imports urllib.request" | None, "apps": [...],
    "paths": [...], "writes_files": bool, "builtin_tools": [...]}``.
    Unparseable source gives the empty answer.
    """
    from tools import tool_sandbox

    info: dict[str, Any] = {
        "network": tool_sandbox.network_reason(source or ""),
        "apps": [], "paths": [], "writes_files": False, "builtin_tools": [],
    }
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return info
    uses_home = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tools":
            for alias in node.names:
                _add(info["builtin_tools"], _words_of(alias.name))
        if isinstance(node, ast.Attribute) and node.attr in ("home", "expanduser"):
            uses_home = True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _WRITE_CALLS:
                info["writes_files"] = True
            if name == "open" and any(
                isinstance(a, ast.Constant) and isinstance(a.value, str)
                and a.value[:1] in ("w", "a", "x")
                for a in list(node.args[1:2]) + [k.value for k in node.keywords if k.arg == "mode"]
            ):
                info["writes_files"] = True
        if isinstance(node, (ast.List, ast.Tuple)):
            # An argv literal: ["open", "-a", "Safari", ...], ["shortcuts", "run", ...].
            argv = [
                e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
                for e in node.elts
            ]
            head = (argv[0] or "").rsplit("/", 1)[-1] if argv else ""
            if head == "open":
                for flag in ("-a", "-b"):
                    if flag in argv and argv.index(flag) + 1 < len(argv):
                        _add(info["apps"], argv[argv.index(flag) + 1] or "an app it picks")
                if "-a" not in argv and "-b" not in argv:
                    _add(info["apps"], "the default app for a file or link")
            elif head == "shortcuts":
                _add(info["apps"], "Shortcuts")
            elif head == "osascript":
                info.setdefault("_osascript", True)
    strings = _const_strings(tree)
    for text in strings:
        for app in _TELL_APP_RE.findall(text):
            _add(info["apps"], app)
        stripped = text.strip()
        if _PATH_RE.match(stripped) and stripped not in ("/", "~"):
            _add(info["paths"], stripped)
        elif uses_home and stripped in _HOME_FOLDERS:
            _add(info["paths"], f"~/{stripped}")
    if info.pop("_osascript", False) and not info["apps"]:
        _add(info["apps"], "other apps through AppleScript")
    return info


def _join(items: list[str]) -> str:
    items = items[:_MAX_LISTED] + (["more"] if len(items) > _MAX_LISTED else [])
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def touches_sentence(source: str) -> str:
    """One plain sentence from :func:`touches_from_source`."""
    info = touches_from_source(source)
    parts: list[str] = []
    parts.append("uses the internet" if info["network"] else "has no internet access")
    if info["apps"]:
        parts.append(f"controls {_join(info['apps'])}")
    if info["paths"]:
        verb = "can change files in" if info["writes_files"] else "reads"
        parts.append(f"{verb} {_join(info['paths'])}")
    elif info["writes_files"]:
        parts.append("can change files")
    if info["builtin_tools"]:
        parts.append(f"uses the assistant's built-in actions ({_join(info['builtin_tools'])})")
    if len(parts) == 1 and not info["network"]:
        return "Its code has no internet access and names no other apps or folders."
    return "Its code " + "; ".join(parts) + "."


# ---- orchestrator glue -----------------------------------------------------
#
# The idea card and the code-review card are two separate round trips in one
# ``generate_custom_tool`` call. The idea summary is remembered on the
# orchestrator (``_idea_summaries``) so the review card and the provenance
# reuse the same sentences without a second model call.


def _idea_fields(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, str, str]:
    def pick(key: str, index: int) -> str:
        value = kwargs.get(key)
        if value is None and len(args) > index:
            value = args[index]
        return str(value or "")

    return pick("tool_name", 0), pick("description", 1), pick("expected_logic", 2)


def for_idea_card(
    orchestrator: Any, args: list[Any], kwargs: dict[str, Any],
) -> Optional[ToolSummary]:
    """Summary for a ``generate_custom_tool`` approval card (worker thread)."""
    try:
        name, description, logic = _idea_fields(list(args or []), dict(kwargs or {}))
        if not name and not description:
            return None
        engine = getattr(orchestrator, "analysis_engine", None)
        return summarize_idea(engine, name, description, logic)
    except Exception:
        logger.exception("tool summary failed")
        return None


def remember(
    orchestrator: Any, summary: Optional[ToolSummary],
    args: list[Any], kwargs: dict[str, Any],
) -> None:
    """Keep ``summary`` for the code-review card of the approved idea.

    ``args`` / ``kwargs`` are the call as approved (the user may have
    edited the name). :func:`for_code_review` drops a summary whose
    description no longer matches the approved one.
    """
    if summary is None:
        return
    try:
        name, _, _ = _idea_fields(list(args or []), dict(kwargs or {}))
        store = getattr(orchestrator, "_idea_summaries", None)
        if not isinstance(store, dict):
            store = {}
            orchestrator._idea_summaries = store
        store.pop(name, None)
        store[name] = summary
        while len(store) > _REMEMBERED_MAX:
            store.pop(next(iter(store)))
    except Exception:
        logger.exception("remembering tool summary failed")


def for_code_review(
    orchestrator: Any, tool_name: str, description: str, source: str,
) -> str:
    """Summary text for the code-review card and the provenance record.

    What the tool does and when come from the idea summary (or the fallback
    when none is remembered); what it touches comes from ``source``.
    Deterministic, no model call, never raises ("" on failure).
    """
    try:
        store = getattr(orchestrator, "_idea_summaries", None)
        summary = store.get(tool_name) if isinstance(store, dict) else None
        if not isinstance(summary, ToolSummary) or summary.description != str(description or ""):
            summary = fallback_summary(tool_name, description)
        return summary.text(touches=touches_sentence(source))
    except Exception:
        logger.exception("code-review tool summary failed")
        return ""


__all__ = [
    "ASKS_FIRST", "SUMMARY_ROLE", "SUMMARY_SYSTEM_PROMPT", "SUMMARY_TIMEOUT_SECONDS",
    "ToolSummary", "build_prompt", "fallback_summary", "for_code_review",
    "for_idea_card", "parse_engine_output", "remember", "summarize_idea",
    "touches_from_source", "touches_sentence",
]
