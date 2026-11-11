""""Watch for…": proactive triggers the user writes, with a backtest (#88).

The user asks for a proactive behaviour in one sentence ("when a PDF from my
accountant arrives, offer to file it under Taxes"). One model call
(:class:`Compiler`) turns the sentence into a restricted JSON spec; the user
sees the spec in plain words (:func:`describe`) with a read-only backtest
(:func:`backtest`) and saves it through ``routes/watches.py``. Nothing else
creates or edits a watch: there is no tool for it, and the route is a user
action.

Rules (todo #88 DECISIONS, LIVING_SPEC §1):

* A WATCH RAISES A CARD, NEVER AN ACTION. A match publishes a
  ``pattern_detected`` idea (``source="watch"``, ``trigger="watch:<id>"``).
  When the action is one ``ai.prepared_offers`` can run after approval
  (``BUNDLE_TOOLS``) the idea carries a filled one-action prepared offer, so
  the card shows the exact arguments and no model is involved; otherwise it
  is a built-in follow-up (``builtin_tool`` + ``event_details``) and the
  built-in tool raises its own approval card. It is not a standing rule
  (#78) and adds no bypass (7a).
* THE MATCHER IS DATA. :func:`matches` is deterministic and reads only the
  event's structured fields (path, sender, subject, title, time), so text in
  a file or an email cannot create, change or trigger a watch.
* SPEC allow-lists (:func:`validate_spec`): trigger kinds ``file``,
  ``mail_received``, ``event_starting_soon``, ``time``; action tools
  :data:`ACTION_TOOLS`; argument placeholders only from the trigger's own
  fields (:data:`PLACEHOLDERS`); a URL never holds a placeholder; no
  ``generate_custom_tool``, generated or MCP tools.
* LIMITS: :data:`MAX_WATCHES`; each watch has on/off, a per-day card limit
  (default :data:`DEFAULT_DAILY_LIMIT`), a cooldown, and switches itself off
  after :data:`DENIALS_TO_PAUSE` denials in a row of its cards (DecisionLog
  rows for ``trigger_kind="watch:<id>"``).
* Stored in ``~/.bot-inator/watches.json`` (0600, atomic writes).
* Privacy (#31/#52): the sentence holds addresses and keywords, so the
  compile call is refused (and says why) when its engine role is a cloud
  one and personal text is off (``privacy.should_redact``). The built-in
  path's redacted event details hide the sender and subject.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path, PurePath
from typing import Any, Awaitable, Callable, Mapping, Optional

from ai import privacy
from events.event_bus import ContextEvent

logger = logging.getLogger(__name__)

Evaluate = Callable[[str], Awaitable[str]]

SOURCE_NAME = "watch"
TRIGGER_PREFIX = "watch:"
DEFAULT_PATH = Path.home() / ".bot-inator" / "watches.json"
PATH_ENV = "AGENT_WATCHES_PATH"

MAX_WATCHES = 20
DEFAULT_DAILY_LIMIT = 5
MAX_DAILY_LIMIT = 20
DEFAULT_COOLDOWN_MINUTES = 30
MAX_COOLDOWN_MINUTES = 24 * 60
DENIALS_TO_PAUSE = 3
BACKTEST_DAYS = 30
MAX_EXAMPLES = 5
MAX_TEXT = 200
MAX_ARG = 500
MAX_SENTENCE = 500
# A ``time`` watch missed by more than this (daemon asleep) is skipped for the day.
TIME_GRACE = timedelta(minutes=60)
POLL_SECONDS = 30.0
_RECENT_KEYS = 50

TRIGGER_KINDS = ("file", "mail_received", "event_starting_soon", "time")
TRIGGER_FIELDS: dict[str, frozenset[str]] = {
    "file": frozenset({"kind", "folder", "extension", "name_contains"}),
    "mail_received": frozenset({"kind", "sender", "subject_contains", "not_bulk"}),
    "event_starting_soon": frozenset({"kind", "title_contains"}),
    "time": frozenset({"kind", "weekdays", "at"}),
}
# Placeholders an argument template may use, per trigger kind: only the
# trigger's own fields.
PLACEHOLDERS: dict[str, frozenset[str]] = {
    "file": frozenset({"path", "date"}),
    "mail_received": frozenset({"sender", "subject", "date"}),
    "event_starting_soon": frozenset({"title", "date"}),
    "time": frozenset({"date"}),
}
# Fields kept on this Mac when a cloud engine reads the built-in follow-up.
PERSONAL_FIELDS = frozenset({"sender", "subject"})

# tool -> (accepted parameters, required parameters). Gated built-ins only.
ACTION_TOOLS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "move_file": (frozenset({"src", "dst_dir", "new_name"}), frozenset({"src", "dst_dir"})),
    "create_reminder": (frozenset({"title", "due", "list_name", "notes"}), frozenset({"title"})),
    "create_event": (frozenset({"title", "start", "end", "calendar", "location", "notes"}),
                     frozenset({"title", "start"})),
    "draft_email": (frozenset({"to", "subject", "body", "cc"}), frozenset({"to"})),
    "create_note": (frozenset({"title", "body", "folder"}), frozenset({"title"})),
    "open_app": (frozenset({"name"}), frozenset({"name"})),
    "open_url": (frozenset({"url"}), frozenset({"url"})),
    "run_shortcut": (frozenset({"name", "input_text"}), frozenset({"name"})),
}
# Arguments that choose WHAT runs or WHERE a file goes: literal only.
LITERAL_ARGS = frozenset({
    ("move_file", "dst_dir"), ("open_app", "name"), ("open_url", "url"),
    ("run_shortcut", "name"), ("create_note", "folder"), ("create_reminder", "list_name"),
    ("create_event", "calendar"),
})
# Defaults so a tool with required positional params gets a full call.
ARG_DEFAULTS: dict[str, dict[str, str]] = {
    "draft_email": {"subject": "", "body": ""},
    "create_note": {"body": ""},
}
TOOL_VERBS: dict[str, str] = {
    "move_file": "move the file",
    "create_reminder": "add a reminder",
    "create_event": "add a calendar event",
    "draft_email": "open an unsent email draft",
    "create_note": "create a note",
    "open_app": "open an app",
    "open_url": "open a web page",
    "run_shortcut": "run a Shortcut",
}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
_DOMAIN_RE = re.compile(r"^@?[\w-]+(?:\.[\w-]+)+$")
_ADDR_IN_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TEMP_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")
_SKIP_MAILBOXES = re.compile(r"junk|spam|trash|deleted|sent|draft|outbox|send ?later", re.IGNORECASE)


class SpecError(ValueError):
    """A spec outside the allow-lists. ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class WatchLimitError(RuntimeError):
    """Saving would exceed :data:`MAX_WATCHES`."""


def _now() -> datetime:
    return datetime.now().astimezone()


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


# ---- spec validation ---------------------------------------------------------


def _short_folder(raw: str) -> Optional[str]:
    """``~``-relative absolute folder, or None when unsafe / not absolute."""
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        return None
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded) or ".." in PurePath(expanded).parts:
        return None
    folder = os.path.normpath(expanded)
    home = str(Path.home())
    if folder == home:
        return "~"
    if folder.startswith(home + os.sep):
        return "~" + folder[len(home):]
    return folder


def _weekday(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 6 else None
    key = str(value or "").strip().lower()[:3]
    return WEEKDAYS.index(key) if key in WEEKDAYS else None


def _validate_trigger(raw: Any, problems: list[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        problems.append("trigger must be an object")
        return {}
    kind = raw.get("kind")
    if kind not in TRIGGER_FIELDS:
        problems.append(f"unknown trigger kind {kind!r}; use one of {list(TRIGGER_KINDS)}")
        return {}
    extra = sorted(set(raw) - TRIGGER_FIELDS[kind])
    if extra:
        problems.append(f"{kind} trigger does not take {extra}")
    out: dict[str, Any] = {"kind": kind}
    if kind == "file":
        folder = _short_folder(str(raw.get("folder") or ""))
        if folder is None:
            problems.append("file trigger needs an absolute folder such as ~/Downloads")
        else:
            out["folder"] = folder
        ext = str(raw.get("extension") or "").strip().lower()
        if ext:
            ext = ext if ext.startswith(".") else "." + ext
            if not _EXT_RE.match(ext):
                problems.append(f"bad extension {raw.get('extension')!r}")
            out["extension"] = ext
        name = _text(raw.get("name_contains"), 100)
        if name:
            out["name_contains"] = name
    elif kind == "mail_received":
        sender = str(raw.get("sender") or "").strip().lower()
        if sender:
            if _EMAIL_RE.match(sender):
                out["sender"] = sender
            elif _DOMAIN_RE.match(sender):
                out["sender"] = sender.lstrip("@")
            else:
                problems.append(f"sender must be an address or a domain, not {raw.get('sender')!r}")
        subject = _text(raw.get("subject_contains"), 100)
        if subject:
            out["subject_contains"] = subject
        if not (sender or subject):
            problems.append("mail trigger needs a sender or subject words")
        not_bulk = raw.get("not_bulk", True)
        if not isinstance(not_bulk, bool):
            problems.append("not_bulk must be true or false")
        out["not_bulk"] = bool(not_bulk)
    elif kind == "event_starting_soon":
        title = _text(raw.get("title_contains"), 100)
        if not title:
            problems.append("calendar trigger needs title words")
        out["title_contains"] = title
    else:  # time
        days = raw.get("weekdays")
        parsed = [_weekday(d) for d in days] if isinstance(days, list) else []
        if not parsed or any(d is None for d in parsed):
            problems.append("time trigger needs weekdays (0 = Monday … 6 = Sunday)")
        out["weekdays"] = sorted({d for d in parsed if d is not None})
        at = str(raw.get("at") or "").strip()
        m = _AT_RE.match(at)
        if not m:
            problems.append("time trigger needs at as HH:MM")
        else:
            out["at"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    return out


def placeholders_in(value: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(value)


def _validate_action(raw: Any, trigger_kind: str, problems: list[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        problems.append("action must be an object")
        return {}
    extra = sorted(set(raw) - {"tool", "arguments"})
    if extra:
        problems.append(f"action does not take {extra}")
    tool = str(raw.get("tool") or "")
    if tool not in ACTION_TOOLS:
        problems.append(
            f"tool {tool!r} is not allowed; a watch may only offer one of {sorted(ACTION_TOOLS)}"
        )
        return {}
    accepted, required = ACTION_TOOLS[tool]
    args = raw.get("arguments") or {}
    if not isinstance(args, Mapping):
        problems.append("arguments must be an object")
        return {}
    allowed = PLACEHOLDERS.get(trigger_kind, frozenset())
    out: dict[str, str] = {}
    for key, value in args.items():
        if key not in accepted:
            problems.append(f"{tool} has no argument {key!r}")
            continue
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            problems.append(f"argument {key!r} must be text")
            continue
        text = str(value)
        if len(text) > MAX_ARG:
            problems.append(f"argument {key!r} is too long")
            continue
        found = placeholders_in(text)
        bad = [p for p in found if p not in allowed]
        if bad:
            problems.append(
                f"placeholder {{{bad[0]}}} is not a field of a {trigger_kind} trigger "
                f"(use {sorted('{' + p + '}' for p in allowed)})"
            )
        if _PLACEHOLDER_RE.sub("", text).count("{") or _PLACEHOLDER_RE.sub("", text).count("}"):
            problems.append(f"argument {key!r} has a stray brace")
        if found and ("://" in text or (tool, key) in LITERAL_ARGS):
            problems.append(f"argument {key!r} may not contain a placeholder")
        out[key] = text
    for key in sorted(required):
        if not str(out.get(key) or "").strip():
            problems.append(f"{tool} needs {key!r}")
    if tool == "move_file":
        if out.get("src") != "{path}":
            problems.append("move_file src must be exactly {path} (the file that arrived)")
        if "dst_dir" in out and _short_folder(out["dst_dir"]) is None:
            problems.append("move_file dst_dir must be an absolute folder such as ~/Documents/Taxes")
        elif "dst_dir" in out:
            out["dst_dir"] = _short_folder(out["dst_dir"]) or out["dst_dir"]
    if tool == "open_url" and not re.match(r"^https?://\S+$", out.get("url", ""), re.IGNORECASE):
        problems.append("open_url url must be a plain http(s) address")
    return {"tool": tool, "arguments": out}


def validate_spec(raw: Any) -> dict[str, Any]:
    """The normalized spec, or :class:`SpecError` listing every problem."""
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        raise SpecError(["spec must be an object with trigger and action"])
    extra = sorted(set(raw) - {"trigger", "action"})
    if extra:
        problems.append(f"spec does not take {extra}")
    trigger = _validate_trigger(raw.get("trigger"), problems)
    action = _validate_action(raw.get("action"), trigger.get("kind", ""), problems) if trigger else {}
    if problems:
        raise SpecError(problems)
    return {"trigger": trigger, "action": action}


# ---- plain words -------------------------------------------------------------


def describe_trigger(trigger: Mapping[str, Any]) -> str:
    kind = trigger.get("kind")
    if kind == "file":
        what = f"a {trigger['extension']} file" if trigger.get("extension") else "a file"
        if trigger.get("name_contains"):
            what += f" whose name contains “{trigger['name_contains']}”"
        return f"When {what} appears in {trigger.get('folder', '?')}"
    if kind == "mail_received":
        parts = ["When an email"]
        sender = str(trigger.get("sender") or "")
        if sender:
            parts.append(f"from {sender}" if "@" in sender else f"from anyone at {sender}")
        if trigger.get("subject_contains"):
            parts.append(f"with “{trigger['subject_contains']}” in the subject")
        parts.append("arrives")
        text = " ".join(parts)
        return text + (" (mailing lists left out)" if trigger.get("not_bulk", True) else "")
    if kind == "event_starting_soon":
        return f"When a calendar event with “{trigger.get('title_contains', '')}” in its title is about to start"
    if kind == "time":
        days = trigger.get("weekdays") or []
        if len(days) == 7:
            when = "Every day"
        elif days == [0, 1, 2, 3, 4]:
            when = "Weekdays"
        else:
            when = "Every " + ", ".join(WEEKDAYS[d].title() for d in days)
        return f"{when} at {trigger.get('at', '?')}"
    return "When something happens"


def describe_action(action: Mapping[str, Any]) -> str:
    tool = str(action.get("tool") or "")
    args = action.get("arguments") or {}
    shown = ", ".join(f"{k}: {v}" for k, v in args.items() if str(v).strip())
    verb = TOOL_VERBS.get(tool, tool)
    return f"offer to {verb} ({tool}{' with ' + shown if shown else ''})"


def describe(spec: Mapping[str, Any]) -> str:
    """The spec in plain words, for the review step and the saved list."""
    return (
        f"{describe_trigger(spec.get('trigger') or {})}, "
        f"{describe_action(spec.get('action') or {})}. "
        "Each time you get a card to approve; nothing runs on its own."
    )


# ---- compile: sentence -> spec (the one model call) --------------------------

COMPILE_SYSTEM_PROMPT = (
    "You turn one sentence from the user into a watch spec for a macOS "
    "assistant. A watch notices an event and OFFERS one action on a card the "
    "user approves; it never acts on its own. Reply with exactly one JSON "
    "object and nothing else (no markdown):\n"
    '{"trigger": {...}, "action": {"tool": "<tool>", "arguments": {...}}}\n\n'
    "trigger is one of:\n"
    '- {"kind": "file", "folder": "~/Downloads", "extension": ".pdf", "name_contains": ""}\n'
    '- {"kind": "mail_received", "sender": "<address or domain>", "subject_contains": "", "not_bulk": true}\n'
    '- {"kind": "event_starting_soon", "title_contains": "<words>"}\n'
    '- {"kind": "time", "weekdays": [0, 1, 2, 3, 4], "at": "HH:MM"} (0 = Monday)\n'
    "tool is one of: move_file(src, dst_dir, new_name), create_reminder(title, "
    "due, list_name, notes), create_event(title, start, end, calendar, location, "
    "notes), draft_email(to, subject, body, cc), create_note(title, body, folder), "
    "open_app(name), open_url(url), run_shortcut(name, input_text).\n"
    "Argument values are text and may use only these placeholders, from the "
    "trigger's own fields: file: {path} {date}; mail_received: {sender} "
    "{subject} {date}; event_starting_soon: {title} {date}; time: {date}. "
    "move_file src must be {path}. A URL, a folder, an app name or a "
    "Shortcut name must be written out, never a placeholder. Leave out "
    "fields you do not need.\n"
    "If the sentence cannot be expressed with these triggers and tools, reply "
    'with {"error": "<one short sentence saying why>"}.'
)


def compile_prompt(sentence: str) -> str:
    """The user text for the compile call."""
    return f"Sentence: {_text(sentence, MAX_SENTENCE)}"


def _json_object(raw: str) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class Compiler:
    """Sentence -> validated spec with ONE injected model call.

    ``evaluate`` is any ``async (user_text) -> str`` whose system prompt is
    :data:`COMPILE_SYSTEM_PROMPT` (main.py builds it with
    ``make_detection_evaluator(detection_engine, system_prompt=...)``).
    ``role`` is the privacy role that engine plays: under a cloud engine with
    personal text off the call is not made (#31/#52).
    """

    def __init__(self, evaluate: Evaluate, role: str = "detection") -> None:
        self._evaluate = evaluate
        self.role = role

    async def compile(self, sentence: str) -> dict[str, Any]:
        """``{"spec": ..., "words": ...}`` or ``{"error": ..., "problems": [...]}``."""
        text = _text(sentence, MAX_SENTENCE)
        if not text:
            return {"error": "Write what to watch for in one sentence."}
        if privacy.should_redact(self.role):
            return {"error": (
                "Not sent: the model that reads this sentence is a cloud one and "
                "personal text is off (Settings > Privacy). Turn it on, or use an "
                "on-device model."
            ), "reason": "personal_text_off"}
        try:
            raw = await self._evaluate(compile_prompt(text))
        except Exception as exc:
            logger.exception("watch compile failed")
            return {"error": f"The model could not be asked ({exc.__class__.__name__})."}
        obj = _json_object(raw)
        if obj is None:
            return {"error": "The model's answer was not a spec. Try rewording the sentence."}
        if obj.get("error") and "trigger" not in obj:
            return {"error": _text(obj.get("error")) or "That can't be a watch."}
        try:
            spec = validate_spec(obj)
        except SpecError as exc:
            return {"error": "The proposed watch is outside what a watch may do.",
                    "problems": exc.problems}
        return {"spec": spec, "words": describe(spec)}


# ---- matching (deterministic, no model) ---------------------------------------


def _address(text: str) -> str:
    m = _ADDR_IN_RE.search(text or "")
    return m.group(0).lower() if m else ""


def _sender_matches(want: str, address: str) -> bool:
    if not want:
        return True
    if not address:
        return False
    if "@" in want:
        return address == want
    domain = address.rsplit("@", 1)[-1]
    return domain == want or domain.endswith("." + want)


def _contains(haystack: Any, needle: str) -> bool:
    return not needle or needle.lower() in " ".join(str(haystack or "").split()).lower()


def _event_date(value: Any, fallback: date) -> str:
    text = str(value or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return fallback.isoformat()


def file_fields_match(trigger: Mapping[str, Any], folder: str, ext: str, name: Optional[str]) -> bool:
    """Folder (``~`` form), extension and, when known, name against a file trigger."""
    if folder != trigger.get("folder"):
        return False
    if trigger.get("extension") and ext.lower() != trigger["extension"]:
        return False
    if trigger.get("name_contains"):
        return name is not None and _contains(name, trigger["name_contains"])
    return True


def event_fields(trigger: Mapping[str, Any], event: ContextEvent,
                 now: Optional[datetime] = None) -> Optional[dict[str, str]]:
    """The placeholder values when ``event`` matches ``trigger``, else None."""
    now = now or _now()
    meta = event.metadata or {}
    kind = trigger.get("kind")
    if kind == "file":
        if event.event_type != "file" or meta.get("action") not in {"created", "renamed"}:
            return None
        path = str(meta.get("path") or "")
        p = PurePath(path)
        if not path or not p.name or p.name.startswith(".") or p.name.lower().endswith(_TEMP_SUFFIXES):
            return None
        folder = _short_folder(str(p.parent))
        if folder is None or not file_fields_match(trigger, folder, p.suffix, p.name):
            return None
        return {"path": path, "date": now.date().isoformat()}
    if kind == "mail_received":
        if event.event_type != "mail_received":
            return None
        if _SKIP_MAILBOXES.search(str(meta.get("mailbox") or "")):
            return None
        if trigger.get("not_bulk", True) and meta.get("bulk"):
            return None
        sender = str(meta.get("sender") or "")
        address = _address(sender)
        if not _sender_matches(str(trigger.get("sender") or ""), address):
            return None
        subject = str(meta.get("subject") or "")
        if not _contains(subject, str(trigger.get("subject_contains") or "")):
            return None
        return {"sender": address or _text(sender), "subject": _text(subject),
                "date": _event_date(meta.get("date"), now.date())}
    if kind == "event_starting_soon":
        if event.event_type != "event_starting_soon":
            return None
        title = str(meta.get("title") or "")
        if not title or not _contains(title, str(trigger.get("title_contains") or "")):
            return None
        return {"title": _text(title), "date": _event_date(meta.get("start"), now.date())}
    if kind == "time":
        if event.event_type != TIME_EVENT_TYPE:
            return None
        if meta.get("at") != trigger.get("at") or meta.get("weekday") not in (trigger.get("weekdays") or []):
            return None
        return {"date": str(meta.get("date") or now.date().isoformat())}
    return None


def matches(spec: Mapping[str, Any], event: ContextEvent, now: Optional[datetime] = None) -> bool:
    return event_fields(spec.get("trigger") or {}, event, now) is not None


# Internal event a ``time`` watch matches; built by :meth:`WatchSource.tick`,
# never put on the bus.
TIME_EVENT_TYPE = "watch_time"


def fill_arguments(action: Mapping[str, Any], fields: Mapping[str, str]) -> dict[str, str]:
    """The action's arguments with placeholders filled from ``fields``."""
    tool = str(action.get("tool") or "")
    out = dict(ARG_DEFAULTS.get(tool, {}))
    for key, template in (action.get("arguments") or {}).items():
        value = _PLACEHOLDER_RE.sub(lambda m: str(fields.get(m.group(1), "")), str(template))
        if tool == "move_file" and key == "new_name":
            value = value.replace("/", "-").replace(":", "-").strip()
        if tool == "move_file" and key == "dst_dir":
            value = os.path.expanduser(value)
        out[key] = value
    return out


def to_idea(watch: Mapping[str, Any], event: ContextEvent, fields: Mapping[str, str]) -> dict[str, Any]:
    """The ``pattern_detected`` metadata for one watch match.

    A built-in follow-up (``builtin_tool`` + ``event_details``), plus a
    filled one-action prepared offer when ``ai.prepared_offers`` can run the
    tool after approval: the card then shows these exact arguments and no
    model call is made.
    """
    from ai import prepared_offers

    spec = watch["spec"]
    action = spec["action"]
    tool = action["tool"]
    args = fill_arguments(action, fields)
    redacted_fields = {k: (privacy.PLACEHOLDER if k in PERSONAL_FIELDS else v) for k, v in fields.items()}
    redacted_args = fill_arguments(action, redacted_fields)
    words = describe(spec)

    def details(values: Mapping[str, str], call_args: Mapping[str, str]) -> str:
        facts = "\n".join(f"{k}: {v}" for k, v in values.items())
        return (
            f"A watch the user set matched.\nWatch: {words}\n{facts}\n"
            f"Call `{tool}` with exactly these arguments: {json.dumps(call_args)}"
        )

    idea: dict[str, Any] = {
        "tool_name": f"watch_{watch['id']}",
        "description": f"Your watch: {words}",
        "expected_logic": details(fields, args),
        "builtin_tool": tool,
        "event_details": details(fields, args),
        "event_details_redacted": details(redacted_fields, redacted_args),
        "source": SOURCE_NAME,
        "trigger": TRIGGER_PREFIX + str(watch["id"]),
        "watch_id": watch["id"],
        "arguments": args,
    }
    if tool in prepared_offers.BUNDLE_TOOLS:
        offer = prepared_offers.PreparedOffer(
            kind=idea["trigger"],
            headline=f"Your watch matched. {describe_trigger(spec['trigger'])}: {TOOL_VERBS[tool]}?",
            actions=[prepared_offers.OfferAction(tool, args, True, TOOL_VERBS[tool])],
            facts={"watch_id": watch["id"]},
        )
        idea[prepared_offers.PREPARED_KEY] = offer.to_dict()
    return idea


# ---- backtest (read-only) ----------------------------------------------------


def _default_activity_rows(since: datetime) -> list[dict[str, Any]]:
    from ai import activity_store

    return activity_store.default_store().rows(since)


def _default_find_files(query: str, folder: str = "", kind: str = "", limit: int = 50) -> dict[str, Any]:
    from tools.find_files import find_files

    return find_files(query, folder=folder, kind=kind, limit=limit)


def _default_recent_mail(since: datetime) -> dict[str, Any]:
    from events.daily_brief import recent_mail

    return recent_mail(since)


def _default_list_events(start: str, end: str) -> dict[str, Any]:
    from tools.list_events import list_events

    return list_events(start, end)


@dataclass
class BacktestSources:
    """Read-only sources for :func:`backtest`; each is injectable."""

    activity_rows: Callable[[datetime], list[dict[str, Any]]] = _default_activity_rows
    find_files: Callable[..., dict[str, Any]] = _default_find_files
    recent_mail: Callable[[datetime], dict[str, Any]] = _default_recent_mail
    list_events: Callable[[str, str], dict[str, Any]] = _default_list_events


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str]:
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        return None, _text(str(exc) or exc.__class__.__name__)
    if isinstance(result, dict) and result.get("status") not in (None, "ok"):
        return None, _text(result.get("error") or "unavailable")
    return result, ""


def _source(name: str, status: str, count: Optional[int] = None, reason: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "count": count, "reason": reason}


def _backtest_file(trigger: Mapping[str, Any], src: BacktestSources, now: datetime, since: datetime,
                   sources: list[dict[str, Any]], examples: list[dict[str, str]]) -> Optional[int]:
    count: Optional[int] = None
    name_filter = bool(trigger.get("name_contains"))
    if name_filter:
        sources.append(_source("Activity history", "skipped",
                               reason="the history keeps folders and extensions, not file names"))
    else:
        rows, why = _call(src.activity_rows, since)
        if rows is None:
            sources.append(_source("Activity history", "unavailable", reason=why))
        else:
            hits = [r for r in rows if r.get("event_type") == "file"
                    and r.get("action") in {"created", "renamed"}
                    and file_fields_match(trigger, str(r.get("folder") or ""), str(r.get("ext") or ""), None)]
            count = len(hits)
            sources.append(_source("Activity history", "ok", count))
    query = str(trigger.get("name_contains") or str(trigger.get("extension") or "").lstrip("."))
    if not query:
        sources.append(_source("Spotlight", "skipped", reason="no name or extension to search for"))
        return count
    found, why = _call(src.find_files, query, folder=str(trigger.get("folder") or ""), kind="", limit=50)
    if found is None:
        sources.append(_source("Spotlight", "unavailable", reason=why))
        return count
    folder_abs = os.path.expanduser(str(trigger.get("folder") or ""))
    hits = []
    for item in found.get("results") or []:
        p = PurePath(str(item.get("path") or ""))
        modified = item.get("modified")
        if str(p.parent) != folder_abs or not isinstance(modified, (int, float)) or modified < since.timestamp():
            continue
        if not file_fields_match(trigger, str(trigger.get("folder")), p.suffix, p.name):
            continue
        hits.append((modified, p.name))
    hits.sort(reverse=True)
    sources.append(_source("Spotlight", "ok", len(hits)))
    for modified, name in hits[:MAX_EXAMPLES]:
        examples.append({"when": datetime.fromtimestamp(modified).astimezone().date().isoformat(), "text": name})
    return count if count is not None else len(hits)


def _backtest_mail(spec: Mapping[str, Any], src: BacktestSources, now: datetime, since: datetime,
                   sources: list[dict[str, Any]], examples: list[dict[str, str]]) -> Optional[int]:
    found, why = _call(src.recent_mail, since)
    if found is None:
        sources.append(_source("Mail", "unavailable", reason=why))
        return None
    hits = []
    for msg in found.get("messages") or []:
        event = ContextEvent(event_type="mail_received", metadata={**msg, "bulk": msg.get("bulk", False)})
        fields = event_fields(spec["trigger"], event, now)
        if fields is not None:
            hits.append(fields)
    sources.append(_source("Mail", "ok", len(hits)))
    for f in hits[:MAX_EXAMPLES]:
        examples.append({"when": f["date"], "text": f"{f['sender']}: {f['subject']}"})
    return len(hits)


def _backtest_calendar(spec: Mapping[str, Any], src: BacktestSources, now: datetime, since: datetime,
                       sources: list[dict[str, Any]], examples: list[dict[str, str]]) -> Optional[int]:
    found, why = _call(src.list_events, since.date().isoformat(), now.isoformat(timespec="minutes"))
    if found is None:
        sources.append(_source("Calendar", "unavailable", reason=why))
        return None
    hits = []
    for item in found.get("events") or []:
        event = ContextEvent(event_type="event_starting_soon", metadata=dict(item))
        fields = event_fields(spec["trigger"], event, now)
        if fields is not None:
            hits.append(fields)
    sources.append(_source("Calendar", "ok", len(hits)))
    for f in sorted(hits, key=lambda f: f["date"], reverse=True)[:MAX_EXAMPLES]:
        examples.append({"when": f["date"], "text": f["title"]})
    return len(hits)


def _backtest_time(trigger: Mapping[str, Any], now: datetime, since: datetime,
                   sources: list[dict[str, Any]], examples: list[dict[str, str]]) -> int:
    hh, mm = (int(x) for x in str(trigger.get("at") or "00:00").split(":"))
    days = []
    day = since.date()
    while day <= now.date():
        moment = datetime.combine(day, dtime(hh, mm), tzinfo=now.tzinfo)
        if day.weekday() in (trigger.get("weekdays") or []) and since <= moment <= now:
            days.append(day)
        day += timedelta(days=1)
    sources.append(_source("Schedule", "ok", len(days)))
    for d in reversed(days[-MAX_EXAMPLES:]):
        examples.append({"when": d.isoformat(), "text": f"{WEEKDAYS[d.weekday()].title()} {trigger.get('at')}"})
    return len(days)


def backtest(spec: Mapping[str, Any], sources: Optional[BacktestSources] = None,
             now: Optional[datetime] = None, days: int = BACKTEST_DAYS) -> dict[str, Any]:
    """How often ``spec`` would have matched in the last ``days`` days.

    Read-only. Returns ``{days, matches, examples, sources, unavailable,
    summary}``; ``matches`` is None when no source could answer, and every
    source that could not is named with its reason (never guessed).
    """
    spec = validate_spec(spec)
    src = sources or BacktestSources()
    now = now or _now()
    since = now - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    examples: list[dict[str, str]] = []
    kind = spec["trigger"]["kind"]
    if kind == "file":
        count = _backtest_file(spec["trigger"], src, now, since, rows, examples)
    elif kind == "mail_received":
        count = _backtest_mail(spec, src, now, since, rows, examples)
    elif kind == "event_starting_soon":
        count = _backtest_calendar(spec, src, now, since, rows, examples)
    else:
        count = _backtest_time(spec["trigger"], now, since, rows, examples)
    unavailable = [f"{r['name']}: {r['reason']}" for r in rows if r["status"] == "unavailable"]
    if count is None:
        summary = "Couldn't check the last %d days (%s)." % (days, "; ".join(unavailable) or "no source")
    else:
        summary = f"Would have matched {count} time{'s' if count != 1 else ''} in the last {days} days."
        if unavailable:
            summary += " Not checked: " + "; ".join(unavailable) + "."
    return {"days": days, "matches": count, "examples": examples[:MAX_EXAMPLES],
            "sources": rows, "unavailable": unavailable, "summary": summary}


# ---- store -------------------------------------------------------------------


def default_path() -> Path:
    override = os.environ.get(PATH_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_PATH


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


class WatchStore:
    """``watches.json`` (0600, atomic). Thread-safe; every change is saved."""

    VERSION = 1

    def __init__(self, path: Optional[Path] = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._clock = clock
        self._lock = threading.RLock()
        self._watches: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning("%s unreadable; starting empty", self.path)
            return []
        out = []
        for raw in (data.get("watches") if isinstance(data, dict) else None) or []:
            try:
                raw["spec"] = validate_spec(raw.get("spec"))
            except (SpecError, TypeError, AttributeError):
                logger.warning("dropped an invalid saved watch")
                continue
            out.append(raw)
        return out[:MAX_WATCHES]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".watches-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": self.VERSION, "watches": self._watches}, fh, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def public(watch: Mapping[str, Any]) -> dict[str, Any]:
        keys = ("id", "sentence", "spec", "enabled", "daily_limit", "cooldown_minutes",
                "created_at", "paused_reason", "last_fired_at")
        out = {k: watch.get(k) for k in keys}
        out["words"] = describe(watch["spec"])
        out["trigger_kind"] = TRIGGER_PREFIX + str(watch["id"])
        fired = watch.get("fired") or {}
        today = _now().date().isoformat()
        out["fired_today"] = int(fired.get("count") or 0) if fired.get("date") == today else 0
        return out

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(w) for w in self._watches]

    def get(self, watch_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for w in self._watches:
                if w["id"] == watch_id:
                    return dict(w)
        return None

    def add(self, spec: Mapping[str, Any], sentence: str = "", *,
            daily_limit: Any = DEFAULT_DAILY_LIMIT, cooldown_minutes: Any = DEFAULT_COOLDOWN_MINUTES) -> dict[str, Any]:
        clean = validate_spec(spec)
        with self._lock:
            if len(self._watches) >= MAX_WATCHES:
                raise WatchLimitError(f"at most {MAX_WATCHES} watches; delete one first")
            watch = {
                "id": uuid.uuid4().hex[:10],
                "sentence": _text(sentence, MAX_SENTENCE),
                "spec": clean,
                "enabled": True,
                "daily_limit": _clamp(daily_limit, 1, MAX_DAILY_LIMIT, DEFAULT_DAILY_LIMIT),
                "cooldown_minutes": _clamp(cooldown_minutes, 0, MAX_COOLDOWN_MINUTES, DEFAULT_COOLDOWN_MINUTES),
                "created_at": float(self._clock()),
                "paused_reason": "",
                "denials_reset_at": float(self._clock()),
                "last_fired_at": None,
                "fired": {"date": "", "count": 0},
                "recent_keys": [],
                "time_fired_on": "",
            }
            self._watches.append(watch)
            self._save()
            return dict(watch)

    def update(self, watch_id: str, *, enabled: Optional[bool] = None, daily_limit: Any = None,
               cooldown_minutes: Any = None) -> Optional[dict[str, Any]]:
        with self._lock:
            for w in self._watches:
                if w["id"] != watch_id:
                    continue
                if enabled is not None:
                    if enabled and not w.get("enabled"):
                        # Switching back on starts a fresh denial count.
                        w["denials_reset_at"] = float(self._clock())
                        w["paused_reason"] = ""
                    w["enabled"] = bool(enabled)
                if daily_limit is not None:
                    w["daily_limit"] = _clamp(daily_limit, 1, MAX_DAILY_LIMIT, w.get("daily_limit", DEFAULT_DAILY_LIMIT))
                if cooldown_minutes is not None:
                    w["cooldown_minutes"] = _clamp(cooldown_minutes, 0, MAX_COOLDOWN_MINUTES,
                                                   w.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
                self._save()
                return dict(w)
        return None

    def delete(self, watch_id: str) -> bool:
        with self._lock:
            before = len(self._watches)
            self._watches = [w for w in self._watches if w["id"] != watch_id]
            if len(self._watches) == before:
                return False
            self._save()
            return True

    def pause(self, watch_id: str, reason: str) -> None:
        with self._lock:
            for w in self._watches:
                if w["id"] == watch_id and w.get("enabled"):
                    w["enabled"] = False
                    w["paused_reason"] = reason
                    self._save()

    # -- firing limits ---------------------------------------------------

    def claim(self, watch_id: str, key: str, now: datetime) -> str:
        """Record one card for ``watch_id``; "" when allowed, else why not.

        Checks (in order): on, the same event not offered before, cooldown,
        per-day limit.
        """
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            w = next((w for w in self._watches if w["id"] == watch_id), None)
            if w is None or not w.get("enabled"):
                return "off"
            if digest in (w.get("recent_keys") or []):
                return "already offered"
            last = w.get("last_fired_at")
            if last is not None and now.timestamp() - float(last) < 60 * int(w.get("cooldown_minutes") or 0):
                return "cooling down"
            today = now.date().isoformat()
            fired = w.get("fired") or {}
            count = int(fired.get("count") or 0) if fired.get("date") == today else 0
            if count >= int(w.get("daily_limit") or DEFAULT_DAILY_LIMIT):
                return "daily limit reached"
            w["fired"] = {"date": today, "count": count + 1}
            w["last_fired_at"] = now.timestamp()
            w["recent_keys"] = ((w.get("recent_keys") or []) + [digest])[-_RECENT_KEYS:]
            self._save()
            return ""

    def mark_time_fired(self, watch_id: str, day: str) -> None:
        with self._lock:
            for w in self._watches:
                if w["id"] == watch_id:
                    w["time_fired_on"] = day
                    self._save()

    # -- denials (#72-style off switch) ------------------------------------

    def apply_denials(self, decisions: Any) -> list[str]:
        """Switch off every watch whose last :data:`DENIALS_TO_PAUSE` card
        decisions (since it was created or switched back on) were denials.
        ``decisions`` is an ``ai.idea_outcomes.DecisionLog``. Returns the
        ids switched off."""
        if decisions is None:
            return []
        rows = list(getattr(decisions, "decisions", None) or [])
        paused = []
        for w in self.list():
            if not w.get("enabled"):
                continue
            if consecutive_denials(rows, w) >= DENIALS_TO_PAUSE:
                self.pause(w["id"], f"Switched off after {DENIALS_TO_PAUSE} denials in a row.")
                paused.append(w["id"])
        return paused


def consecutive_denials(rows: list[Mapping[str, Any]], watch: Mapping[str, Any]) -> int:
    """Denials in a row of this watch's cards, newest last."""
    trigger = TRIGGER_PREFIX + str(watch["id"])
    since = float(watch.get("denials_reset_at") or 0.0)
    count = 0
    for row in rows:
        if row.get("trigger_kind") != trigger or float(row.get("at") or 0.0) < since:
            continue
        count = count + 1 if row.get("decision") == "denied" else 0
    return count


# ---- idea source -------------------------------------------------------------


class WatchSource:
    """Watches as an idea source.

    :meth:`observe` is the hook the orchestrator calls on every event; a
    match publishes one ``pattern_detected`` idea (never runs anything).
    ``time`` watches fire from :meth:`tick`, which a daemon thread calls
    every :data:`POLL_SECONDS` between :meth:`start` and :meth:`stop`
    (main.py keeps it in ``listeners``).
    """

    source_name = SOURCE_NAME

    def __init__(self, bus: Any, store: WatchStore, *, decisions: Any = None,
                 clock: Callable[[], datetime] = _now,
                 publish: Optional[Callable[[ContextEvent], Any]] = None,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self._bus = bus
        self.store = store
        # ai.idea_outcomes.DecisionLog; main.py sets it (may be None in tests).
        self.decisions = decisions
        self._clock = clock
        self._publish = publish or (lambda e: self._bus.push_threadsafe(e))
        self._poll = poll_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # listener interface
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="WatchSource", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.tick()
            except Exception:
                logger.exception("watch tick failed")

    @staticmethod
    def _key(event: ContextEvent, fields: Mapping[str, str]) -> str:
        meta = event.metadata or {}
        ident = meta.get("message_id") or meta.get("path") or f"{meta.get('title', '')}|{meta.get('start', '')}"
        return f"{event.event_type}|{ident}|{fields.get('date', '')}"

    def _offer(self, watch: Mapping[str, Any], event: ContextEvent, fields: Mapping[str, str],
               now: datetime) -> Optional[dict[str, Any]]:
        why = self.store.claim(watch["id"], self._key(event, fields), now)
        if why:
            logger.info("watch %s matched but no card: %s", watch["id"], why)
            return None
        idea = to_idea(watch, event, fields)
        try:
            self._publish(ContextEvent(event_type="pattern_detected", metadata=idea))
        except Exception:
            logger.exception("watch %s publish failed", watch["id"])
            return None
        return idea

    def _active(self) -> list[dict[str, Any]]:
        try:
            self.store.apply_denials(self.decisions)
        except Exception:
            logger.exception("watch denial check failed")
        return [w for w in self.store.list() if w.get("enabled")]

    def observe(self, event: ContextEvent) -> list[dict[str, Any]]:
        """Publish an idea for each enabled watch ``event`` matches."""
        if event.event_type in {"pattern_detected", TIME_EVENT_TYPE}:
            return []
        now = self._clock()
        offered = []
        for watch in self._active():
            if watch["spec"]["trigger"]["kind"] == "time":
                continue
            fields = event_fields(watch["spec"]["trigger"], event, now)
            if fields is None:
                continue
            idea = self._offer(watch, event, fields, now)
            if idea is not None:
                offered.append(idea)
        return offered

    def tick(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Fire ``time`` watches due today (at most once a day each)."""
        now = now or self._clock()
        today = now.date().isoformat()
        offered = []
        for watch in self._active():
            trigger = watch["spec"]["trigger"]
            if trigger["kind"] != "time" or watch.get("time_fired_on") == today:
                continue
            if now.weekday() not in trigger["weekdays"]:
                continue
            hh, mm = (int(x) for x in trigger["at"].split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now < due:
                continue
            self.store.mark_time_fired(watch["id"], today)
            if now - due > TIME_GRACE:
                continue
            event = ContextEvent(event_type=TIME_EVENT_TYPE, metadata={
                "at": trigger["at"], "weekday": now.weekday(), "date": today})
            fields = event_fields(trigger, event, now)
            if fields is None:
                continue
            idea = self._offer(watch, event, fields, now)
            if idea is not None:
                offered.append(idea)
        return offered


# How ``Orchestrator._build_pattern_prompt`` introduces a watch idea (runner:
# ``_PATTERN_SOURCE_INTROS["watch"]``). Watch ideas always carry
# ``builtin_tool``, so this text is a fallback only.
PATTERN_SOURCE_INTRO = (
    "A watch the user wrote and saved matched an event, and it offers one "
    "built-in action the user approves on a card."
)

__all__ = [
    "ACTION_TOOLS", "BacktestSources", "COMPILE_SYSTEM_PROMPT", "Compiler", "DENIALS_TO_PAUSE",
    "MAX_WATCHES", "PATTERN_SOURCE_INTRO", "PLACEHOLDERS", "SOURCE_NAME", "SpecError",
    "TRIGGER_PREFIX", "WatchLimitError", "WatchSource", "WatchStore", "backtest",
    "compile_prompt", "consecutive_denials", "describe", "event_fields", "fill_arguments",
    "matches", "to_idea", "validate_spec",
]
