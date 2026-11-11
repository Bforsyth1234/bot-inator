"""Installed-tool health: failure log, the repair rule, what can't be repaired (#90).

Generated tools break when macOS or an app changes. This module keeps a
per-tool run log and decides when to OFFER a repair (never to make one:
the repair is a new draft through the same checks, critic, dry run and
code card, ``tools.meta_tool_generator.repair_tool``).

* :class:`ToolHealthLog` (``~/.bot-inator/tool_health.json``, 0600, atomic
  writes): per tool, the recent failures (time, error class, the error's
  first line with argument values removed, whether it is repairable and
  why not), a collapsed count of ok runs, and "repaired" / "restored"
  markers. Argument values are never stored.
* :func:`classify_failure`: not repairable, and said so: sandbox refusals
  (a denied path, app, executable or network is policy, and a "fix" would
  be a way around it), timeouts, a missing macOS permission (point to the
  Permissions checklist, #59), bad arguments from the model. Apple Event
  -10004 is the sandbox, not a permission (#102/#103: macOS refuses quit
  and Mail's message data to any sandboxed process); the reason names the
  built-ins to use, and :func:`record_call` attaches it to the failed
  call's result so the reply says it in one line.
* The rule (:meth:`ToolHealthLog.evaluate`): two repairable failures in a
  row with the same error class, or three repairable failures in 7 days;
  one offer per tool per 7 days (``IdeaState`` section ``"repair"``); never
  for a disabled tool (#23). Only failures since the last repair / restore
  count.
* :func:`repair_idea`: the ``pattern_detected`` metadata
  (``source="repair"``, ``builtin_tool="repair_generated_tool"``). The card
  text (local only) carries the error; the prompt-side fields carry only
  the tool name and error class.
* :func:`record_call`: the hook ``Orchestrator.gated_forward`` calls after
  a generated tool ran (or raised). Never raises.

``path=None`` keeps a log in memory only (tests); ``clock`` is injectable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

DATA_DIR = Path.home() / ".bot-inator"
PATH_ENV = "AGENT_TOOL_HEALTH_PATH"

WEEK_SECONDS = 7 * 24 * 3600.0
REPAIR_COOLDOWN_SECONDS = WEEK_SECONDS
FAILURES_IN_WINDOW = 3
COOLDOWN_SECTION = "repair"
REPAIR_TOOL_NAME = "repair_generated_tool"

MAX_ENTRIES_PER_TOOL = 50
MAX_TOOLS = 500
MAX_MESSAGE = 240
MAX_CLASS = 60
ARG_PLACEHOLDER = "<argument>"
_MIN_REDACT_LEN = 3

# Not-repairable categories, checked in this order.
CATEGORY_TIMEOUT = "timeout"
CATEGORY_PERMISSION = "permission"
CATEGORY_SANDBOX = "sandbox"
CATEGORY_BAD_ARGUMENTS = "bad_arguments"

REASONS = {
    CATEGORY_TIMEOUT: (
        "It ran out of time. A timeout is not a code bug a repair can be "
        "trusted to fix."
    ),
    CATEGORY_PERMISSION: (
        "macOS denied a permission it needs. Grant it in Settings → "
        "Permissions instead of changing the code."
    ),
    CATEGORY_SANDBOX: (
        "The tool sandbox refused it (a denied path, app, executable or "
        "network). That is policy; a code change would be a way around it."
        " If you reviewed this tool and trust it, you can let it run outside the "
        "sandbox: Tool Manager → the tool → Run outside the sandbox (#104)."
    ),
    CATEGORY_BAD_ARGUMENTS: (
        "The assistant called it with bad arguments; the tool's code is not "
        "what failed."
    ),
}

# #102/#103: macOS refuses some Apple Events (quit, Mail's message data) to
# EVERY process under sandbox-exec, Automation granted or not, so -10004 from
# a sandboxed tool is the sandbox, not a missing permission.
REASON_SANDBOX_APPLE_EVENT = (
    "macOS refuses this Apple Event to any sandboxed tool (-10004), even with "
    "Automation allowed, so no permission or code change fixes it. Use the "
    "built-ins instead: quit_app to quit an app, list_recent_emails or "
    "read_email for Mail."
    " If you reviewed this tool and trust it, you can let it run outside the "
    "sandbox: Tool Manager → the tool → Run outside the sandbox (#104)."
)
REASON_SANDBOX_EXECUTABLE = (
    "The tool sandbox does not let generated tools run that program. Use the "
    "built-in take_screenshot for screenshots, quit_app to quit an app."
    " If you reviewed this tool and trust it, you can let it run outside the "
    "sandbox: Tool Manager → the tool → Run outside the sandbox (#104)."
)
# Key added to a failed call's result dict: the one line the reply names.
USER_NOTE_KEY = "tell_user"

_SANDBOX_AE_RE = re.compile(
    r"-10004\b|privilege violation|errAEPrivilegeError", re.I,
)
_SANDBOX_EXEC_RE = re.compile(r"screencapture|process-exec|\[Errno 1\].*(/usr/s?bin/|exec)", re.I)
_TIMEOUT_RE = re.compile(r"timed? ?out|did not finish within|TimeoutExpired", re.I)
_PERMISSION_RE = re.compile(
    r"-1743\b|-25211\b|not authori[sz]ed to send apple events|"
    r"assistive access|errAEEventNotPermitted|"
    r"full disk access|not allowed to send keystrokes",
    re.I,
)
_SANDBOX_RE = re.compile(
    r"refused by the tool sandbox|operation not permitted|\[Errno 1\]|"
    r"sandbox-exec|SandboxError:|deny\(|sandbox.*den(y|ied)",
    re.I,
)
_BAD_ARGS_RE = re.compile(
    r"unexpected keyword argument|required (positional|keyword-only) argument|"
    r"positional arguments? but \d+ (were|was) given|got multiple values for argument|"
    r"not in the tool's input schema",
    re.I,
)
_CLASS_PREFIX_RE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Expired|Interrupt))\b\s*:?")


def default_path() -> Path:
    raw = os.environ.get(PATH_ENV, "").strip()
    return Path(raw).expanduser() if raw else DATA_DIR / "tool_health.json"


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# ---- describing a failure ---------------------------------------------------


def _flat_strings(values: Any) -> list[str]:
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if len(v.strip()) >= _MIN_REDACT_LEN:
                out.append(v.strip())
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, (list, tuple, set)):
            for item in v:
                walk(item)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= 100:
            out.append(str(v))

    walk(values)
    return sorted(set(out), key=len, reverse=True)


def redact_arguments(text: str, arg_values: Any = ()) -> str:
    """``text`` with every argument value (strings of 3+ chars) replaced."""
    for value in _flat_strings(arg_values):
        text = text.replace(value, ARG_PLACEHOLDER)
    return text


def first_line(text: Any) -> str:
    lines = [ln.strip() for ln in str(text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    # A traceback's last line is the error; any other text's first line is.
    line = lines[-1] if lines[0].startswith("Traceback") else lines[0]
    return line[:MAX_MESSAGE]


def error_class_of(message: str, exc: Optional[BaseException] = None) -> str:
    """``AttributeError`` from ``"AttributeError: x"`` (sandbox errors carry
    the child's class); else the exception's type; else ``ToolError`` (the
    tool returned ``status: "error"``)."""
    m = _CLASS_PREFIX_RE.match(message or "")
    if m:
        return m.group(1).rsplit(".", 1)[-1][:MAX_CLASS]
    if exc is not None:
        name = type(exc).__name__
        if name not in ("ToolSandboxError",):
            return name[:MAX_CLASS]
    return "ToolError"


def classify_failure(
    message: str, error_class: str = "", exc: Optional[BaseException] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """``(repairable, category, reason)``. Unrepairable: timeout, permission,
    sandbox refusal, bad arguments (checked in that order)."""
    text = f"{error_class}: {message}"
    if (exc is not None and type(exc).__name__ == "ToolSandboxTimeout") or \
            error_class in ("TimeoutError", "TimeoutExpired") or _TIMEOUT_RE.search(text):
        cat = CATEGORY_TIMEOUT
    elif _SANDBOX_AE_RE.search(text):
        return False, CATEGORY_SANDBOX, REASON_SANDBOX_APPLE_EVENT
    elif _PERMISSION_RE.search(text):
        cat = CATEGORY_PERMISSION
    elif _SANDBOX_RE.search(text) or error_class == "PermissionError":
        if _SANDBOX_EXEC_RE.search(text):
            return False, CATEGORY_SANDBOX, REASON_SANDBOX_EXECUTABLE
        cat = CATEGORY_SANDBOX
    elif _BAD_ARGS_RE.search(text):
        cat = CATEGORY_BAD_ARGUMENTS
    else:
        return True, None, None
    return False, cat, REASONS[cat]


def describe_failure(
    *, result: Any = None, exc: Optional[BaseException] = None, arg_values: Any = (),
) -> Optional[dict[str, Any]]:
    """The failure fields for one call, or ``None`` when it succeeded.

    A raised exception is a failure; so is a result dict with
    ``status == "error"`` (tools catch their own errors and report them).
    """
    if exc is not None:
        raw = getattr(exc, "child_error", None) or str(exc) or type(exc).__name__
    elif isinstance(result, dict) and str(result.get("status", "")).lower() == "error":
        raw = result.get("error") or result.get("message") or "returned status error"
        if not isinstance(raw, str):
            raw = json.dumps(raw, default=str)
    else:
        return None
    message = redact_arguments(first_line(raw), arg_values)
    cls = error_class_of(message, exc)
    repairable, category, reason = classify_failure(message, cls, exc)
    return {
        "error_class": cls, "message": message, "repairable": repairable,
        "category": category, "reason": reason,
    }


# ---- the log ----------------------------------------------------------------


class ToolHealthLog:
    """Per-tool run log and the repair-offer rule (thread-safe)."""

    VERSION = 1

    def __init__(
        self, path: Optional[Path] = None, *, state: Any = None, clock: Clock = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        if state is None:
            from ai.idea_sources import IdeaState

            state = IdeaState(None, clock=clock)
        self.state = state
        self._tools: dict[str, dict[str, Any]] = {}
        self._load()

    # -- storage
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("tool health at %s unreadable; starting empty", self.path)
            return
        tools = data.get("tools") if isinstance(data, dict) else None
        if isinstance(tools, dict):
            self._tools = {
                str(k): v for k, v in tools.items()
                if isinstance(v, dict) and isinstance(v.get("entries"), list)
            }

    def _save(self) -> None:
        if len(self._tools) > MAX_TOOLS:
            oldest = sorted(self._tools, key=lambda n: self._last_time(n))
            for name in oldest[: len(self._tools) - MAX_TOOLS]:
                self._tools.pop(name, None)
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}-")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"version": self.VERSION, "tools": self._tools}, fh, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            logger.exception("failed to save %s", self.path)

    def _last_time(self, name: str) -> float:
        entries = self._tools.get(name, {}).get("entries") or [{}]
        return float(entries[-1].get("t", 0.0))

    def _record(self, tool_name: str) -> dict[str, Any]:
        return self._tools.setdefault(tool_name, {"entries": [], "ok_runs": 0, "last_ok": None})

    def _append(self, tool_name: str, entry: dict[str, Any]) -> None:
        rec = self._record(tool_name)
        rec["entries"].append(entry)
        del rec["entries"][:-MAX_ENTRIES_PER_TOOL]

    # -- recording
    def record_success(self, tool_name: str) -> None:
        with self._lock:
            now = self._clock()
            rec = self._record(tool_name)
            rec["ok_runs"] = int(rec.get("ok_runs") or 0) + 1
            rec["last_ok"] = now
            entries = rec["entries"]
            if entries and entries[-1].get("kind") == "ok":
                entries[-1]["t"] = now
                entries[-1]["count"] = int(entries[-1].get("count") or 1) + 1
            else:
                self._append(tool_name, {"t": now, "kind": "ok", "count": 1})
            self._save()

    def record_failure(self, tool_name: str, failure: dict[str, Any]) -> dict[str, Any]:
        """Store one failure (fields from :func:`describe_failure`)."""
        entry = {
            "t": self._clock(), "kind": "fail",
            "error_class": str(failure.get("error_class") or "ToolError")[:MAX_CLASS],
            "message": str(failure.get("message") or "")[:MAX_MESSAGE],
            "repairable": bool(failure.get("repairable", True)),
            "category": failure.get("category"),
            "reason": failure.get("reason"),
        }
        with self._lock:
            self._append(tool_name, entry)
            self._save()
        return dict(entry)

    def note_change(self, tool_name: str, kind: str) -> None:
        """A repair was installed or a previous version restored: failures
        before it no longer count toward the rule."""
        with self._lock:
            self._append(tool_name, {"t": self._clock(), "kind": kind})
            self._save()

    def forget(self, tool_name: str) -> None:
        with self._lock:
            if self._tools.pop(tool_name, None) is not None:
                self._save()

    # -- the rule
    def _since_change(self, tool_name: str) -> list[dict[str, Any]]:
        entries = list(self._tools.get(tool_name, {}).get("entries") or [])
        for idx in range(len(entries) - 1, -1, -1):
            if entries[idx].get("kind") in ("repaired", "restored"):
                return entries[idx + 1:]
        return entries

    def cooldown_active(self, tool_name: str) -> bool:
        return bool(self.state.active(COOLDOWN_SECTION, tool_name))

    def evaluate(self, tool_name: str, *, disabled: bool = False) -> dict[str, Any]:
        """``{"offer": bool, "reason": str, "failure": entry | None, "count": n}``."""
        with self._lock:
            entries = self._since_change(tool_name)
            last = entries[-1] if entries else None
            out: dict[str, Any] = {"offer": False, "reason": "", "failure": last, "count": 0}
            if disabled:
                out["reason"] = "disabled"
                return out
            if last is None or last.get("kind") != "fail":
                out["reason"] = "last run ok" if last else "no runs"
                out["failure"] = None
                return out
            if not last.get("repairable", True):
                out["reason"] = f"not repairable ({last.get('category')})"
                return out
            now = self._clock()
            fails = [
                e for e in entries
                if e.get("kind") == "fail" and e.get("repairable", True)
                and now - float(e.get("t", 0)) <= WEEK_SECONDS
            ]
            prev = entries[-2] if len(entries) >= 2 else None
            in_a_row = bool(
                prev and prev.get("kind") == "fail" and prev.get("repairable", True)
                and prev.get("error_class") == last.get("error_class")
            )
            out["count"] = len(fails)
            if not (in_a_row or len(fails) >= FAILURES_IN_WINDOW):
                out["reason"] = "not failing often enough yet"
                return out
            if self.cooldown_active(tool_name):
                out["reason"] = "a repair was offered within 7 days"
                return out
            out["offer"] = True
            out["reason"] = (
                "two failures in a row" if in_a_row else f"{len(fails)} failures in 7 days"
            )
            return out

    def mark_offered(self, tool_name: str) -> None:
        self.state.start(COOLDOWN_SECTION, tool_name, REPAIR_COOLDOWN_SECONDS)

    def observe(
        self, tool_name: str, *, result: Any = None, exc: Optional[BaseException] = None,
        arg_values: Any = (), disabled: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Record one call; return the repair idea metadata when the rule
        fires (and start the cooldown), else ``None``."""
        failure = describe_failure(result=result, exc=exc, arg_values=arg_values)
        if failure is None:
            self.record_success(tool_name)
            return None
        with self._lock:
            entry = self.record_failure(tool_name, failure)
            decision = self.evaluate(tool_name, disabled=disabled)
            if not decision["offer"]:
                logger.info("tool %s failed (%s): no repair offer: %s",
                            tool_name, entry["error_class"], decision["reason"])
                return None
            self.mark_offered(tool_name)
        return repair_idea(tool_name, entry, count=max(2, decision["count"]))

    # -- reading
    def latest_failure(self, tool_name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for entry in reversed(self._since_change(tool_name)):
                if entry.get("kind") == "fail":
                    return dict(entry)
        return None

    def health(self, tool_name: str) -> dict[str, Any]:
        with self._lock:
            rec = self._tools.get(tool_name) or {"entries": [], "ok_runs": 0, "last_ok": None}
            entries = rec.get("entries") or []
            since = self._since_change(tool_name)
            now = self._clock()
            fails = [e for e in entries if e.get("kind") == "fail"]
            changes = [e for e in entries if e.get("kind") in ("repaired", "restored")]
            last = since[-1] if since else None
            status = "unknown"
            if last is not None:
                status = "failing" if last.get("kind") == "fail" else "ok"
            elif changes:
                status = changes[-1]["kind"]
            until = (getattr(self.state, "sections", {}) or {}).get(COOLDOWN_SECTION, {}).get(tool_name)
            return {
                "tool_name": tool_name,
                "status": status,
                "ok_runs": int(rec.get("ok_runs") or 0),
                "last_ok_at": _iso(rec.get("last_ok")),
                "failures_7d": sum(1 for e in fails if now - float(e.get("t", 0)) <= WEEK_SECONDS),
                "recent_failures": [_public(e) for e in reversed(fails[-10:])],
                "last_change": (
                    {"kind": changes[-1]["kind"], "at": _iso(changes[-1].get("t"))}
                    if changes else None
                ),
                "repair_offered_until": _iso(until) if until and until > now else None,
            }

    def all_health(self) -> list[dict[str, Any]]:
        with self._lock:
            names = sorted(self._tools)
        return [self.health(n) for n in names]


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": _iso(entry.get("t")),
        "error_class": entry.get("error_class"),
        "message": entry.get("message"),
        "repairable": bool(entry.get("repairable", True)),
        "category": entry.get("category"),
        "reason": entry.get("reason"),
    }


# ---- the idea ---------------------------------------------------------------


def repair_idea(tool_name: str, failure: dict[str, Any], *, count: int = 2) -> dict[str, Any]:
    """``pattern_detected`` metadata offering a repair of ``tool_name``.

    The agent calls the built-in :data:`REPAIR_TOOL_NAME` (its approval
    card is the idea gate; the code card is the code gate). The error
    message goes only into ``card_text``, which stays local; prompt-side
    fields carry the tool name and the error class.
    """
    cls = str(failure.get("error_class") or "ToolError")
    message = str(failure.get("message") or "")
    details = {"tool_name": tool_name}
    return {
        "source": "repair",
        "trigger": "tool_failing",
        "tool_name": REPAIR_TOOL_NAME,
        "builtin_tool": REPAIR_TOOL_NAME,
        "repair_target": tool_name,
        "error_class": cls,
        "description": (
            f"Draft a fix for the installed tool {tool_name}, which keeps failing "
            f"({cls}). The user reviews the change as a diff before anything is written."
        ),
        "expected_logic": f"Call {REPAIR_TOOL_NAME} with tool_name={tool_name!r}.",
        "event_details": details,
        "event_details_redacted": dict(details),
        "card_text": (
            f"Repair {tool_name}? It failed {count} times recently: {message or cls}. "
            "A fix is drafted and checked, and you review it as a diff before "
            "anything changes."
        ),
    }


# ---- the orchestrator hook --------------------------------------------------


def _is_disabled(orchestrator: Any, tool_name: str) -> bool:
    gen = getattr(orchestrator, "generated_tools_dir", None)
    if not gen:
        return False
    return (Path(gen) / "disabled" / f"{tool_name}.py").exists()


def user_note(tool_name: str, failure: Optional[dict[str, Any]]) -> Optional[str]:
    """One line for the reply when ``failure`` can't be repaired, else ``None``."""
    if not failure or failure.get("repairable", True) or not failure.get("reason"):
        return None
    return f"Tell the user in one line why {tool_name} can't work: {failure['reason']}"


def _attach_note(tool_name: str, result: Any, exc: Optional[BaseException],
                 arg_values: Any) -> None:
    """#102: put the not-repairable reason where the agent reads it (the
    result dict, or the raised error's message) so the reply names it."""
    note = user_note(tool_name, describe_failure(result=result, exc=exc, arg_values=arg_values))
    if note is None:
        return
    if exc is not None:
        if exc.args and isinstance(exc.args[0], str) and note not in exc.args[0]:
            exc.args = (f"{exc.args[0]}\n{note}", *exc.args[1:])
    elif isinstance(result, dict):
        result.setdefault(USER_NOTE_KEY, note)


def record_call(
    orchestrator: Any, tool_name: str, *, result: Any = None,
    exc: Optional[BaseException] = None, args: Iterable[Any] = (),
    kwargs: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Record one generated-tool call on ``orchestrator.tool_health``; when
    the rule fires, push a ``pattern_detected`` repair idea onto the event
    bus (thread-safe). Returns the idea or ``None``. Never raises.

    A failure that can't be repaired (sandbox, permission, timeout, bad
    arguments) gets its reason attached for the reply (:func:`user_note`):
    ``result["tell_user"]``, or appended to the raised error's message."""
    args = list(args)
    try:
        _attach_note(tool_name, result, exc, [args, dict(kwargs or {})])
    except Exception:
        logger.exception("tool health: explaining %s failed", tool_name)
    log = getattr(orchestrator, "tool_health", None)
    if log is None:
        return None
    try:
        idea = log.observe(
            tool_name, result=result, exc=exc,
            arg_values=[list(args), dict(kwargs or {})],
            disabled=_is_disabled(orchestrator, tool_name),
        )
        if idea is None:
            return None
        from events.event_bus import ContextEvent

        bus = getattr(orchestrator, "event_bus", None)
        if bus is not None and getattr(bus, "loop", None) is not None:
            bus.push_threadsafe(ContextEvent(event_type="pattern_detected", metadata=idea))
        return idea
    except Exception:
        logger.exception("tool health: recording %s failed", tool_name)
        return None


__all__ = [
    "CATEGORY_BAD_ARGUMENTS", "CATEGORY_PERMISSION", "CATEGORY_SANDBOX", "CATEGORY_TIMEOUT",
    "REPAIR_TOOL_NAME", "ToolHealthLog", "classify_failure", "default_path",
    "describe_failure", "error_class_of", "record_call", "redact_arguments", "repair_idea",
    "user_note",
]
