"""What a generated tool needs to work, said on its cards up front (#151).

Two gates show it:

* the idea card (``generate_custom_tool``): :func:`idea_needs` guesses from
  the idea's words ("will likely need ...");
* the code card: :func:`code_needs` reads the drafted source
  (:func:`scan_source`) and adds what the sandboxed dry run found no rewrite
  can fix (:func:`record_dry_run`, from ``tools.draft_dry_run``).

A need is ``{"kind", "reason", "granted", "permission_id"}``:

* ``outside_sandbox``: the tool can only work outside the sandbox. ``reason``
  says why in plain words (it quits apps, reads Mail messages, sends Apple
  Events to an app the sandbox closes, runs a program the sandbox blocks,
  writes into ``~/Library``). ``granted`` is ``None``: this module only
  EXPLAINS. Trust is the review card's checkbox, off by default (invariant
  7b, #104); nothing here sets it.
* a macOS permission: ``contacts``, ``calendars``, ``reminders``,
  ``full_disk_access`` or ``automation:<App>``. ``granted`` is ``True`` /
  ``False`` from :mod:`ai.permissions` (``None`` when it can't tell);
  ``permission_id`` is the Permissions row the card's Grant button opens.

CONSERVATIVE: a false alarm on a card is worse than a miss (the tool's first
failure still says the same thing, ``ai.tool_health``), so every check looks
for a specific pattern, never a vague word. Docstrings are not scanned.
"""
from __future__ import annotations

import ast
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from tools import tool_sandbox

logger = logging.getLogger(__name__)

OUTSIDE_SANDBOX = "outside_sandbox"
CONTACTS = "contacts"
CALENDARS = "calendars"
REMINDERS = "reminders"
FULL_DISK_ACCESS = "full_disk_access"
AUTOMATION_PREFIX = "automation:"

REASON_QUIT = "it quits apps, which macOS blocks for sandboxed tools"
REASON_MAIL_DATA = "it reads Mail messages, which macOS blocks for sandboxed tools"
REASON_SHELL = "it runs shell commands, which the sandbox blocks"
REASON_DRY_RUN_APPLE_EVENT = (
    "in the dry run macOS refused one of its Apple Events (-10004), which it "
    "does for every sandboxed tool"
)
REASON_DRY_RUN_EXEC = "in the dry run the sandbox refused to run a program it starts"

# DENIED_APPLE_EVENT_TARGETS by the names a script uses.
_DENIED_APP_NAMES: dict[str, str] = {
    "terminal": "Terminal", "iterm": "iTerm", "iterm2": "iTerm",
    "script editor": "Script Editor", "system events": "System Events",
    "finder": "Finder",
}
_DENIED_BUNDLE_NAMES: dict[str, str] = {
    "com.apple.terminal": "Terminal", "com.googlecode.iterm2": "iTerm",
    "com.apple.scripteditor2": "Script Editor",
    "com.apple.systemevents": "System Events", "com.apple.finder": "Finder",
}

_ALLOWED_PROGRAMS = frozenset(
    {p.rsplit("/", 1)[-1] for p in tool_sandbox.ALLOWED_EXECUTABLES} | {"open"}
)
# Interpreter names are left alone: ``sys.executable`` is allowed and a
# bare ``python3`` may resolve to it; better a miss than a false alarm.
_SKIP_PROGRAMS = frozenset({"python", "python3", "/usr/bin/open"})
_SUBPROCESS_FUNCS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
_SHELL_FUNCS = frozenset({"system", "popen"})
# Writes into these (``~``-relative) need the installed sandbox gone.
_CLOSED_WRITE_DIRS: tuple[str, ...] = ("Library", ".ssh")

# ---- AppleScript / JXA text ---------------------------------------------------

_TELL_BLOCK_RE = re.compile(
    r'tell\s+(?:app|application)\s+(?:id\s+)?"([^"]+)"', re.I)
_JXA_APP_RE = re.compile(r'Application\(\s*[\'"]([^\'"]+)[\'"]\s*\)', re.I)
# Inside a ``tell`` block or one-liner: ``to quit`` / a ``quit`` line.
_QUIT_RE = re.compile(r"\bto\s+quit\b|^\s*quit\b(?!\s*[:=(])", re.I | re.M)
_QUIT_APP_RE = re.compile(r'\bquit\s+(?:app|application)\s+(?:id\s+)?"', re.I)
_JXA_QUIT_RE = re.compile(r'Application\(\s*[\'"][^\'"]+[\'"]\s*\)\.quit\s*\(', re.I)
_LAUNCH_ONLY_RE = re.compile(r'"\s+to\s+(?:quit|activate|launch|run)\b', re.I)
_MAIL_DATA_RE = re.compile(
    r"\b(?:messages?\s+of|every\s+message|(?:content|subject|sender|"
    r"date\s+received|read\s+status)\s+of|inbox|mailbox(?:es)?)\b",
    re.I,
)
_MAIL_COMPOSE_RE = re.compile(r"\b(?:outgoing|new)\s+message\b", re.I)

# ---- data the sandboxed child reads with Full Disk Access --------------------

_FDA_PATH_RE = re.compile(
    r"Library/(?:Mail|Messages|Safari)\b|\bchat\.db\b|Envelope Index", re.I)
_FDA_MODULES = frozenset({"tools.read_email", "tools.list_recent_emails"})
_CONTACTS_MODULES = frozenset({"Contacts", "AddressBook", "tools.contacts"})
_CALENDAR_MODULES = frozenset({"tools.calendar_access", "tools.list_events", "tools.create_event"})


@dataclass(frozen=True)
class Need:
    kind: str
    reason: str
    granted: Optional[bool] = None
    permission_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "granted": self.granted,
                "permission_id": self.permission_id}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _string_constants(tree: ast.AST) -> list[str]:
    """Every string literal in source order, docstrings left out. An
    f-string is one literal with ``{}`` for each placeholder."""
    skip = _docstring_nodes(tree)
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                for v in node.values
            )
            found.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), text))
            skip.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            found.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), node.value))
    found.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in found]


def _path_texts(tree: ast.AST) -> list[str]:
    """``a / "b" / "c"`` and ``os.path.join(a, "b", "c")`` as ``"b/c"``."""
    out: list[str] = []
    for node in ast.walk(tree):
        is_join = isinstance(node, ast.Call) and _call_name(node.func)[1] == "join" \
            and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute)
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)) or is_join:
            parts = [n.value for n in sorted(
                (n for n in ast.walk(node)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)),
                key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))]
            if len(parts) > 1:
                out.append("/".join(p.strip("/") for p in parts))
    return out


def _placeholder(name: str) -> bool:
    return "{" in name or "%" in name or not name.strip()


def _script_segments(text: str) -> list[tuple[str, str]]:
    """``(app, body)`` per ``tell application "X"`` block or one-liner; a
    block runs to the next ``end tell`` (good enough for one level)."""
    out: list[tuple[str, str]] = []
    for m in _TELL_BLOCK_RE.finditer(text):
        rest = text[m.end():]
        line_end = rest.find("\n")
        same_line = rest if line_end < 0 else rest[:line_end]
        if re.match(r"\s+to\b", same_line, re.I):
            out.append((m.group(1), m.group(0) + same_line))
            continue
        end = re.search(r"\bend\s+tell\b", rest, re.I)
        out.append((m.group(1), m.group(0) + (rest[: end.start()] if end else same_line)))
    return out


def _app_label(name: str) -> str:
    low = name.strip().lower()
    return _DENIED_APP_NAMES.get(low) or _DENIED_BUNDLE_NAMES.get(low) or name.strip()


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _call_name(func: ast.AST) -> tuple[str, str]:
    """``("subprocess", "run")`` for ``subprocess.run``; ``("", "run")`` for ``run``."""
    if isinstance(func, ast.Attribute):
        base = func.value
        return (base.id if isinstance(base, ast.Name) else "", func.attr)
    if isinstance(func, ast.Name):
        return ("", func.id)
    return ("", "")


def _from_imported(tree: ast.AST, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(a.asname or a.name for a in node.names)
    return names


def _program_of(call: ast.Call) -> tuple[Optional[str], bool]:
    """``(program, shell)`` of a subprocess call when its program is a literal."""
    shell = any(
        k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in call.keywords
    )
    arg: Optional[ast.AST] = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == "args"), None)
    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = arg.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value.strip(), shell
        return None, shell
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        text = arg.value.strip()
        return (text.split()[0] if text else None), shell
    return None, shell


def _exec_reasons(tree: ast.AST) -> list[str]:
    reasons: list[str] = []
    sub_names = _from_imported(tree, "subprocess") & _SUBPROCESS_FUNCS
    os_names = _from_imported(tree, "os") & _SHELL_FUNCS
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        base, name = _call_name(node.func)
        if (base == "os" and name in _SHELL_FUNCS) or (not base and name in os_names):
            reasons.append(REASON_SHELL)
            continue
        if not ((base == "subprocess" and name in _SUBPROCESS_FUNCS)
                or (not base and name in sub_names)):
            continue
        program, shell = _program_of(node)
        if shell:
            reasons.append(REASON_SHELL)
            continue
        if not program or program in _SKIP_PROGRAMS:
            continue
        short = program.rsplit("/", 1)[-1]
        if short in _ALLOWED_PROGRAMS and (
                "/" not in program or program in tool_sandbox.ALLOWED_EXECUTABLES):
            continue
        if short.startswith("python"):
            continue
        reasons.append(f"it runs {short}, a program the sandbox blocks")
    return reasons


# ---- writes into closed folders ---------------------------------------------

_WRITE_ATTRS = frozenset({"write_text", "write_bytes", "mkdir", "touch"})
_WRITE_FUNCS = {
    ("os", "makedirs"), ("os", "mkdir"), ("shutil", "copy"), ("shutil", "copy2"),
    ("shutil", "copyfile"), ("shutil", "copytree"), ("shutil", "move"),
}


def _assigned(tree: ast.AST) -> dict[str, ast.AST]:
    """Names assigned exactly once (one level of indirection for paths)."""
    seen: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            seen.setdefault(node.targets[0].id, []).append(node.value)
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


def _closed_dir_of(expr: Optional[ast.AST], names: dict[str, ast.AST]) -> Optional[str]:
    """``"~/Library"`` when ``expr`` (a path expression) points into a closed folder."""
    if expr is None:
        return None
    if isinstance(expr, ast.Name) and expr.id in names:
        expr = names[expr.id]
    found: list[tuple[int, int, str]] = []
    homeish = False
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), node.value))
        elif isinstance(node, ast.Call):
            _, fn = _call_name(node.func)
            if fn == "home":
                homeish = True
        elif isinstance(node, ast.Name) and node.id in names and node is not expr:
            inner = _closed_dir_of(
                names[node.id], {k: v for k, v in names.items() if k != node.id})
            if inner:
                return inner
    consts = [c for _, _, c in sorted(found)]
    for c in consts:
        for d in _CLOSED_WRITE_DIRS:
            if c == f"~/{d}" or c.startswith(f"~/{d}/"):
                return f"~/{d}"
    if homeish and consts:
        first = consts[0].strip("/")
        for d in _CLOSED_WRITE_DIRS:
            if first == d or first.startswith(d + "/"):
                return f"~/{d}"
    return None


def _write_reasons(tree: ast.AST) -> list[str]:
    names = _assigned(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        base, fn = _call_name(node.func)
        target: Optional[ast.AST] = None
        if not base and fn == "open" and node.args:
            mode = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "mode"), None)
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                    and set(mode.value) & set("wax+"):
                target = node.args[0]
        elif isinstance(node.func, ast.Attribute) and fn in _WRITE_ATTRS:
            target = node.func.value
        elif (base, fn) in _WRITE_FUNCS and node.args:
            target = node.args[-1] if base == "shutil" else node.args[0]
        where = _closed_dir_of(target, names)
        if where and where not in hits:
            hits.append(where)
    return [f"it writes into {w}, which the sandbox closes" for w in hits]


# ---- the scan -------------------------------------------------------------


def scan_source(source: str) -> list[Need]:
    """Needs read from drafted source (no permission state; see :func:`code_needs`)."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    text = "\n".join(_string_constants(tree))
    needs: list[Need] = []

    def add(kind: str, reason: str) -> None:
        if not any(n.kind == kind and n.reason == reason for n in needs):
            needs.append(Need(kind, reason))

    segments = [(a, b) for a, b in _script_segments(text)]
    jxa_apps = [m.group(1) for m in _JXA_APP_RE.finditer(text)]
    if any(_QUIT_RE.search(body) for _, body in segments) or _JXA_QUIT_RE.search(text) \
            or _QUIT_APP_RE.search(text):
        add(OUTSIDE_SANDBOX, REASON_QUIT)
    for app, body in segments:
        if app.strip().lower() == "mail" and _MAIL_DATA_RE.search(_MAIL_COMPOSE_RE.sub("", body)):
            add(OUTSIDE_SANDBOX, REASON_MAIL_DATA)
    targets: list[str] = []   # every app it sends Apple Events to
    told: list[str] = []      # ... beyond a one-line quit / activate / launch
    for app, body in segments:
        if _placeholder(app):
            continue
        label = _app_label(app)
        if label not in targets:
            targets.append(label)
        if label not in told and not (_LAUNCH_ONLY_RE.search(body) and "\n" not in body.strip()):
            told.append(label)
    for app in jxa_apps:
        label = _app_label(app)
        if not _placeholder(app) and label not in told:
            told.append(label)
            targets.append(label)
    for label in targets:
        if label in _DENIED_APP_NAMES.values():
            add(OUTSIDE_SANDBOX,
                f"it sends Apple Events to {label}, which the sandbox blocks")
    for reason in _exec_reasons(tree):
        add(OUTSIDE_SANDBOX, reason)
    for reason in _write_reasons(tree):
        add(OUTSIDE_SANDBOX, reason)

    imports = _imports(tree)
    if (imports & _CONTACTS_MODULES) or re.search(r"\bCNContactStore\b", source) \
            or any(_app_label(a).lower() == "contacts" for a in told):
        add(CONTACTS, "it reads your contacts")
    if "EventKit" in imports or "EKEventStore" in source:
        if "EKEntityTypeReminder" in source:
            add(REMINDERS, "it reads or changes your reminders")
        if "EKEntityTypeEvent" in source or "EKEntityTypeReminder" not in source:
            add(CALENDARS, "it reads or changes your calendars")
    elif imports & _CALENDAR_MODULES:
        add(CALENDARS, "it reads or changes your calendars")
    if (imports & _FDA_MODULES) or _FDA_PATH_RE.search(text) \
            or any(_FDA_PATH_RE.search(p) for p in _path_texts(tree)):
        add(FULL_DISK_ACCESS, "it reads Mail, Messages or Safari data in ~/Library")
    for label in told:
        add(AUTOMATION_PREFIX + label, f"it controls {label} with AppleScript")
    return needs


# ---- idea text (first gate) -----------------------------------------------

_IDEA_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b(?:quit|quits|quitting|force[- ]quit)\b", re.I),
     OUTSIDE_SANDBOX, "quitting apps, which macOS blocks for sandboxed tools"),
    (re.compile(r"\b(?:screenshots?|screen ?capture)\b", re.I),
     OUTSIDE_SANDBOX, "taking screenshots, which the sandbox blocks"),
    (re.compile(r"\b(?:read|reads|reading|list|lists|listing|search|searches|scan|scans|"
                r"summari[sz]e|summari[sz]es)\b[^.]{0,40}\b(?:e-?mails?|inbox|mail messages)\b", re.I),
     FULL_DISK_ACCESS, "reading your Mail"),
    (re.compile(r"\b(?:in|with|using|via)\s+(?:the\s+)?(?:Terminal|iTerm2?|Finder|System Events)\b"),
     OUTSIDE_SANDBOX, "controlling Terminal, Finder or System Events, which the sandbox blocks"),
    (re.compile(r"\bcontacts\b|\baddress book\b", re.I), CONTACTS, "reading your contacts"),
    (re.compile(r"\bcalendars?\b", re.I), CALENDARS, "your calendars"),
    (re.compile(r"\b(?:imessages?|messages app|chat\.db|safari history)\b", re.I),
     FULL_DISK_ACCESS, "reading Messages or Safari data"),
)


def idea_needs(text: str) -> list[Need]:
    """A cheap guess from an idea's words: what it will LIKELY need."""
    needs: list[Need] = []
    for pattern, kind, reason in _IDEA_RULES:
        if pattern.search(text or "") and not any(n.kind == kind and n.reason == reason for n in needs):
            needs.append(Need(kind, reason))
    return needs


def idea_text_from_args(tool_args: Any) -> str:
    """``description`` + ``expected_logic`` of a ``generate_custom_tool`` card's
    ``tool_args`` (``{"args": [...], "kwargs": {...}}``)."""
    if not isinstance(tool_args, dict):
        return ""
    kwargs = tool_args.get("kwargs") if isinstance(tool_args.get("kwargs"), dict) else tool_args
    args = tool_args.get("args") if isinstance(tool_args.get("args"), list) else []
    parts = [str(kwargs.get(k) or "") for k in ("description", "expected_logic")]
    parts += [str(a) for a in args[1:3] if isinstance(a, str)]
    return "\n".join(p for p in parts if p)


# ---- dry-run findings (#151 Approach 2) ----------------------------------------

_DRY_RUN: dict[str, tuple[str, list[Need]]] = {}
_DRY_RUN_LOCK = threading.Lock()


def record_dry_run(tool_name: str, source: str, needs: Iterable[Need]) -> None:
    """The dry run's unfixable refusals for this exact draft (replaces the last)."""
    with _DRY_RUN_LOCK:
        _DRY_RUN[tool_name] = (source, list(needs))


def dry_run_needs(tool_name: str, code: str) -> list[Need]:
    """What :func:`record_dry_run` kept for ``code`` (the card's code may carry
    a review-notes header above the dry-run source)."""
    with _DRY_RUN_LOCK:
        entry = _DRY_RUN.get(tool_name)
    if not entry:
        return []
    source, needs = entry
    if code == source or (source and code.endswith("\n" + source)):
        return list(needs)
    return []


def clear_dry_run() -> None:
    with _DRY_RUN_LOCK:
        _DRY_RUN.clear()


# ---- permission state -------------------------------------------------------


def _permission_id(kind: str) -> Optional[str]:
    if kind in (CONTACTS, CALENDARS, FULL_DISK_ACCESS):
        return kind
    if kind.startswith(AUTOMATION_PREFIX):
        from ai import permissions

        name = f"Automation → {kind[len(AUTOMATION_PREFIX):]}"
        row = next((p for p in permissions.PERMISSIONS if p.name.lower() == name.lower()), None)
        return row.id if row else None
    return None


def with_permission_state(needs: Iterable[Need]) -> list[Need]:
    """``granted`` from :mod:`ai.permissions` (checks never prompt)."""
    from ai import permissions

    out: list[Need] = []
    for need in needs:
        if need.kind == OUTSIDE_SANDBOX:
            out.append(need)
            continue
        perm_id = _permission_id(need.kind)
        granted: Optional[bool] = None
        perm = permissions.get(perm_id) if perm_id else None
        if perm is not None:
            try:
                status = (perm.check() or {}).get("status")
            except Exception:
                logger.debug("permission check %s failed", perm_id, exc_info=True)
                status = None
            granted = True if status == permissions.GRANTED else (
                False if status in (permissions.DENIED, permissions.NOT_ASKED) else None)
        out.append(Need(need.kind, need.reason, granted, perm_id))
    return out


def _merge(*groups: Iterable[Need]) -> list[Need]:
    out: list[Need] = []
    for group in groups:
        for need in group:
            if not any(n.kind == need.kind and n.reason == need.reason for n in out):
                out.append(need)
    return out


def code_needs(tool_name: str, code: str, *, check: bool = True) -> list[dict[str, Any]]:
    """The code card's needs: the source scan plus the dry run's findings."""
    needs = _merge(scan_source(code), dry_run_needs(tool_name, code))
    if check and needs:
        needs = with_permission_state(needs)
    return [n.as_dict() for n in needs]


def idea_card_needs(tool_args: Any, *, check: bool = True) -> list[dict[str, Any]]:
    """The idea card's "will likely need ..." guess."""
    needs = idea_needs(idea_text_from_args(tool_args))
    if check and needs:
        needs = with_permission_state(needs)
    return [n.as_dict() for n in needs]


__all__ = [
    "AUTOMATION_PREFIX", "CALENDARS", "CONTACTS", "FULL_DISK_ACCESS", "Need",
    "OUTSIDE_SANDBOX", "REMINDERS", "clear_dry_run", "code_needs", "dry_run_needs",
    "idea_card_needs", "idea_needs", "idea_text_from_args", "record_dry_run",
    "scan_source", "with_permission_state",
]
