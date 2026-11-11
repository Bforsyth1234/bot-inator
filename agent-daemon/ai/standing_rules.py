"""Standing rules: "always do this" (#78).

A standing rule is the only consent besides an approval card (invariant 7a
as amended 2026-11-10, LIVING_SPEC §1 "Standing rules"). This module keeps
those limits; nothing here may loosen them:

1. THE USER CREATES IT. A rule exists only because the user said yes to an
   *offer*, through ``routes/rules.py``. An offer is raised only after
   :data:`MIN_CLEAN_APPROVALS` (5) approvals of the same tool for the same
   trigger kind and the same fixed argument pattern with no edit and no
   denial in between (``ai.idea_outcomes.DecisionLog.clean_approvals``), and
   the count is checked again when the user accepts. There is no tool for
   rules; a model, a tool, an event or a forged offer cannot create, change
   or enable one (:meth:`StandingRules.accept` takes only an offer id the
   daemon computed, and re-derives eligibility from the decision log).
2. IT IS NARROW: one trigger kind + one tool + one fixed argument pattern.
   The pattern is a restricted ``ai.watches`` spec (trigger + action
   template), with an ``arg_pattern`` of its hash for the decision log.
3. IT NEVER COVERS (:func:`denied_reason`, checked when the offer is made,
   when it is accepted, when it is switched on, and again at every run):
   sending or drafting messages or email, ``generate_custom_tool``,
   ``repair_generated_tool``, generated tools, MCP tools, ``run_shortcut``,
   anything that deletes, and anything that uses the network (``open_url``
   included). On top of that denylist, only :data:`RULE_TOOLS` can have a
   rule at all: the built-ins whose every run ``ai.action_journal`` can undo.
4. IT RUNS WITHOUT A MODEL. :meth:`StandingRules.run_for_event` matches the
   raw event with ``ai.watches.event_fields`` (structured fields only: the
   file path, the mail sender header) and fills the arguments with
   ``ai.watches.fill_arguments`` from those fields. The only placeholder a
   rule may use is ``{path}`` as ``move_file``'s ``src`` and ``{date}``;
   everything else is a literal fixed when the rule was made. Mail rules
   match on the sender only (never subject words), so text in a file, an
   email or a page cannot trigger or steer a rule.
5. THE USER CAN SEE AND REVOKE IT. Rules are listed (``GET /api/rules``)
   with on/off, delete and the evidence that led to the offer. Every run is
   journaled (``ActionJournal.record(..., source="rule", rule_id=...)``) and
   posts a notification whose Undo goes through the journal. A rule undone
   :data:`UNDOS_TO_DISABLE` (2) times switches itself off
   (:meth:`StandingRules.on_undo`, an ``ActionJournal`` undo listener).

A paused app (#87 ``Delivery.paused``) runs no rule.

Stored in ``~/.bot-inator/standing_rules.json`` (0600, atomic writes;
``AGENT_STANDING_RULES_PATH`` overrides). Only ids, folders, the tool name,
the argument template, counts and times are stored; never message text.
``path=None`` keeps the store in memory (tests).
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
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any, Callable, Iterable, Mapping, Optional

from ai import watches
from events.event_bus import ContextEvent

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

MIN_CLEAN_APPROVALS = 5
UNDOS_TO_DISABLE = 2
MAX_RULES = 20
MAX_RUNS_PER_RULE_PER_DAY = 20
MAX_RECENT_KEYS = 50
PATH_ENV = "AGENT_STANDING_RULES_PATH"

# The only tools a rule may ever run: built-ins whose result the action
# journal (#83) can undo. Nothing outside this set, whatever its name.
RULE_TOOLS: frozenset[str] = frozenset({"move_file", "create_reminder", "create_event", "create_note"})

# The hard denylist (LIVING_SPEC §1 limit 3), by exact name ...
DENIED_TOOLS: dict[str, str] = {
    "send_imessage": "sends a message",
    "draft_email": "drafts an email",
    "generate_custom_tool": "builds new code",
    "repair_generated_tool": "changes generated code",
    "run_shortcut": "a Shortcut can do anything",
    "open_url": "uses the network",
    "append_note": "changes an existing note",
    "write_clipboard": "replaces the clipboard",
}
# ... and by what the name says it does.
_DENIED_WORDS = re.compile(
    r"send|draft|mail|message|imessage|sms|reply|forward|post|tweet|chat|"
    r"delete|remove|trash|erase|wipe|purge|clear|unlink|"
    r"url|http|web|fetch|download|upload|network|request|api|browse|"
    r"shortcut|script|shell|exec|generate|repair",
    re.IGNORECASE,
)
# Event types a rule can run on (watch ``time`` events never reach the bus).
RULE_EVENT_TYPES: frozenset[str] = frozenset({"file", "mail_received"})
# Trigger kinds a rule's matcher may use. Calendar titles come from invites
# (outside text), so ``event_starting_soon`` is left out.
RULE_TRIGGER_KINDS: frozenset[str] = frozenset({"file", "mail_received"})
# Argument names that would reach another person (an invite) are never allowed.
_PEOPLE_ARGS = frozenset({"to", "cc", "bcc", "attendees", "invitees", "recipients"})
# A file rule never moves into or out of these (home-relative parts).
_SENSITIVE_PARTS = frozenset({"Library", ".ssh", ".bot-inator", ".gnupg", ".config", ".aws"})


# ---- the limits ---------------------------------------------------------------


def denied_reason(tool_name: str, *, dynamic_names: Iterable[str] = (),
                  is_mcp: bool = False) -> str:
    """Why ``tool_name`` can never have a standing rule; "" when it may.

    The denylist comes first (exact names, MCP and generated tools, then the
    words in the name), then the :data:`RULE_TOOLS` allowlist.
    """
    name = str(tool_name or "")
    if not name:
        return "no tool"
    if name in DENIED_TOOLS:
        return f"{name} {DENIED_TOOLS[name]}"
    if is_mcp or "__" in name or name.startswith("mcp"):
        return f"{name} is an MCP tool"
    if name in set(dynamic_names):
        return f"{name} is a generated tool"
    if _DENIED_WORDS.search(name):
        return f"{name} may send, delete or use the network"
    if name not in RULE_TOOLS:
        return f"{name} is not one of the tools a rule may run ({', '.join(sorted(RULE_TOOLS))})"
    return ""


def _home_parts(folder: str) -> Optional[tuple[str, ...]]:
    """Parts below the home folder of a ``~`` folder, else None (outside home)."""
    if folder == "~":
        return ()
    if not folder.startswith("~/"):
        return None
    return tuple(PurePath(folder[2:]).parts)


def _safe_folder(folder: str) -> bool:
    parts = _home_parts(folder)
    if parts is None:
        return False
    return not any(p.startswith(".") or p in _SENSITIVE_PARTS for p in parts)


def spec_problems(spec: Any) -> list[str]:
    """Every reason ``spec`` is outside the standing-rule limits ([] = fine).

    Stricter than a watch: the ``ai.watches`` allow-lists first, then the
    tool denylist / allowlist, the trigger kinds, sender-only mail matching,
    and literal arguments except ``move_file`` ``src={path}`` and ``{date}``.
    """
    try:
        clean = watches.validate_spec(spec)
    except watches.SpecError as exc:
        return list(exc.problems)
    problems: list[str] = []
    trigger, action = clean["trigger"], clean["action"]
    tool = action.get("tool", "")
    why = denied_reason(tool)
    if why:
        problems.append(why)
    kind = trigger.get("kind")
    if kind not in RULE_TRIGGER_KINDS:
        problems.append(f"a rule can't start from a {kind} trigger")
    if kind == "mail_received":
        if not trigger.get("sender"):
            problems.append("a mail rule needs a sender")
        if trigger.get("subject_contains"):
            problems.append("a mail rule matches the sender only, never words in the email")
        if not trigger.get("not_bulk", True):
            problems.append("a mail rule leaves mailing lists out")
    if kind == "file" and not _safe_folder(str(trigger.get("folder") or "")):
        problems.append("a file rule only watches ordinary folders in your home folder")
    for key, value in (action.get("arguments") or {}).items():
        if key in _PEOPLE_ARGS:
            problems.append(f"a rule can't fill {key!r}")
        found = watches.placeholders_in(str(value))
        if tool == "move_file" and key == "src":
            if value != "{path}":
                problems.append("move_file src must be exactly {path}")
            continue
        bad = [p for p in found if p != "date"]
        if bad:
            problems.append(f"argument {key!r} may only use {{date}}, not {{{bad[0]}}}")
    if tool == "move_file":
        if "new_name" in (action.get("arguments") or {}):
            problems.append("a move rule keeps the file's name")
        if not _safe_folder(str((action.get("arguments") or {}).get("dst_dir") or "")):
            problems.append("a move rule only files into ordinary folders in your home folder")
        if kind != "file":
            problems.append("a move rule starts from a file trigger")
    return problems


def arg_pattern(spec: Mapping[str, Any]) -> dict[str, str]:
    """The fixed argument pattern the decision log counts approvals under
    (``DecisionLog.record(..., arg_pattern=...)`` / ``clean_approvals``)."""
    canon = json.dumps(watches.validate_spec(spec), sort_keys=True, separators=(",", ":"))
    return {"rule": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]}


def _short(path: str) -> Optional[str]:
    return watches._short_folder(path)  # noqa: SLF001 - same normalisation as watches


def derive_spec(
    tool_name: str,
    kwargs: Mapping[str, Any],
    trigger_kind: str,
    *,
    event_type: str = "",
    event_meta: Optional[Mapping[str, Any]] = None,
    idea_meta: Optional[Mapping[str, Any]] = None,
    watch_lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
) -> Optional[dict[str, Any]]:
    """The rule spec an approved card would become, or None (never a rule).

    Only cards whose arguments came from structured data qualify:

    * a watch's card (``trigger_kind="watch:<id>"``): the watch's own spec,
      when the card's kwargs are exactly what the watch filled in;
    * ``move_file`` whose ``src`` is the triggering file itself: the raw
      ``file`` event's path, or the ``src`` of a one-action prepared offer
      (#76, built from that path by code). A ``src`` the model typed in a
      built-in follow-up turn does not qualify. The rule keeps the name.
    """
    if denied_reason(tool_name):
        return None
    kwargs = dict(kwargs or {})
    meta = dict(event_meta or {})
    idea = dict(idea_meta or {})
    trigger_kind = str(trigger_kind or "")
    spec: Optional[dict[str, Any]] = None
    if trigger_kind.startswith(watches.TRIGGER_PREFIX):
        watch_id = trigger_kind[len(watches.TRIGGER_PREFIX):]
        watch = watch_lookup(watch_id) if watch_lookup is not None else None
        if not watch or (watch.get("spec") or {}).get("action", {}).get("tool") != tool_name:
            return None
        filled = idea.get("arguments")
        if not isinstance(filled, Mapping) or {k: str(v) for k, v in kwargs.items()} != {
                k: str(v) for k, v in filled.items()}:
            return None
        spec = {"trigger": dict(watch["spec"]["trigger"]), "action": dict(watch["spec"]["action"])}
        if tool_name == "move_file":
            spec["action"]["arguments"] = {k: v for k, v in spec["action"]["arguments"].items()
                                           if k != "new_name"}
    elif tool_name == "move_file":
        subject = _subject_path(event_type, meta, idea)
        src, dst = str(kwargs.get("src") or ""), str(kwargs.get("dst_dir") or "")
        if not subject or src != subject or not dst:
            return None
        p = PurePath(subject)
        folder, dst_short = _short(str(p.parent)), _short(dst)
        if folder is None or dst_short is None or dst_short == folder:
            return None
        trigger: dict[str, Any] = {"kind": "file", "folder": folder}
        if p.suffix:
            trigger["extension"] = p.suffix.lower()
        spec = {"trigger": trigger,
                "action": {"tool": "move_file", "arguments": {"src": "{path}", "dst_dir": dst_short}}}
    if spec is None or spec_problems(spec):
        return None
    return watches.validate_spec(spec)


def effective_kwargs(tool_args: Mapping[str, Any], edited_args: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The kwargs the card actually ran with: ``edited_args["kwargs"]``, or
    the single action's args from ``edited_args["actions"]`` (a one-action
    prepared offer), else the proposed kwargs. The runner diffs this against
    the proposal (``edit_facts``) so an edit made in the action list also
    counts as an edit and resets the clean-approval streak."""
    original = dict((tool_args or {}).get("kwargs") or {})
    edited = dict(edited_args or {})
    if isinstance(edited.get("kwargs"), Mapping):
        return {**original, **edited["kwargs"]}
    actions = edited.get("actions")
    if isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], Mapping):
        if not actions[0].get("enabled", True):
            return {}
        if isinstance(actions[0].get("args"), Mapping):
            return {**original, **actions[0]["args"]}
    return original


def _subject_path(event_type: str, meta: Mapping[str, Any], idea: Mapping[str, Any]) -> str:
    """The triggering file's path from structured data only, else ""."""
    if event_type == "file":
        return str(meta.get("path") or "")
    offer = idea.get("prepared_offer")
    if isinstance(offer, Mapping):
        actions = [a for a in offer.get("actions") or [] if isinstance(a, Mapping)]
        if len(actions) == 1 and actions[0].get("tool") == "move_file":
            return str((actions[0].get("args") or {}).get("src") or "")
    return ""


def describe_rule(spec: Mapping[str, Any]) -> str:
    """The rule in plain words."""
    action = spec.get("action") or {}
    args = {k: v for k, v in (action.get("arguments") or {}).items() if k != "src"}
    tool = str(action.get("tool") or "")
    if tool == "move_file":
        what = f"move it to {args.get('dst_dir', '?')} (keeping its name)"
    else:
        shown = ", ".join(f"{k}: {v}" for k, v in args.items() if str(v).strip())
        what = f"{watches.TOOL_VERBS.get(tool, tool)}{' (' + shown + ')' if shown else ''}"
    return f"{watches.describe_trigger(spec.get('trigger') or {})}, {what} without asking."


# ---- persistence ------------------------------------------------------------


def default_path() -> Path:
    override = os.environ.get(PATH_ENV)
    return Path(override).expanduser() if override else Path.home() / ".bot-inator" / "standing_rules.json"


def _atomic_write(path: Optional[Path], data: Mapping[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".standing_rules.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        logger.exception("standing rules: saving %s failed", path)


def _callable(obj: Any) -> Callable[..., Any]:
    return getattr(obj, "_ungated_forward", None) or getattr(obj, "forward", None) or obj


def builtin_tools() -> dict[str, Any]:
    """The built-in tool table (never the orchestrator's: no generated or
    MCP tool can be reached from a rule)."""
    import tools as tool_pkg  # deferred: tools imports lead back into ai

    return {getattr(t, "name", getattr(t, "__name__", "")): t for t in tool_pkg.AVAILABLE_TOOLS}


class RuleError(ValueError):
    """An offer or rule outside the limits, or not found. ``code`` is the HTTP status."""

    def __init__(self, message: str, code: int = 409) -> None:
        super().__init__(message)
        self.code = code


class StandingRules:
    """Offers, rules and runs. Thread-safe; call off the event loop."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        clock: Clock = time.time,
        paused: Callable[[], bool] = lambda: False,
        classify: Optional[Callable[[ContextEvent], Optional[str]]] = None,
        watch_lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        tools: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.clock = clock
        # #87: ``lambda: delivery.paused``. A paused app runs no rule.
        self.paused = paused
        # ``NextStepSuggester.classify``: a rule offered from a built-in kind
        # (e.g. ``document_downloaded``) runs only when the event is that kind.
        self.classify = classify
        # ``WatchStore.get``: a rule from a watch runs only while the watch is on.
        self.watch_lookup = watch_lookup
        self._tools = tools
        self._lock = threading.RLock()
        data = self._read()
        self.rules: list[dict[str, Any]] = [r for r in data.get("rules", []) if isinstance(r, dict)]
        self.offers: list[dict[str, Any]] = [o for o in data.get("offers", []) if isinstance(o, dict)]

    # -- storage --------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("%s unreadable; starting with no rules", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        _atomic_write(self.path, {"version": 1, "rules": self.rules, "offers": self.offers})

    @staticmethod
    def public(rule: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: rule.get(k) for k in (
            "id", "trigger_kind", "tool", "spec", "words", "enabled", "created_at",
            "disabled_reason", "evidence", "runs", "last_run_at", "undos", "undos_total")}
        return out

    def list_rules(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.public(r) for r in self.rules]

    def get(self, rule_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            rule = self._find(rule_id)
            return self.public(rule) if rule else None

    def _find(self, rule_id: str) -> Optional[dict[str, Any]]:
        return next((r for r in self.rules if r.get("id") == rule_id), None)

    def pending_offers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(o) for o in self.offers if o.get("status") == "pending"]

    # -- 1. the offer -----------------------------------------------------------

    def maybe_offer(self, decisions: Any, trigger_kind: str, tool_name: str,
                    spec: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        """After a clean approval: a NEW "always do this?" offer, else None.

        None when the spec is outside the limits, fewer than
        :data:`MIN_CLEAN_APPROVALS` clean approvals of this exact pattern, a
        rule for it already exists (on or off), it is already offered, or
        the user said no thanks and it has not been approved 5 more times
        since. Never creates or enables a rule.
        """
        if decisions is None or spec is None or spec_problems(spec):
            return None
        if (spec.get("action") or {}).get("tool") != tool_name:
            return None
        pattern = arg_pattern(spec)
        count = int(decisions.clean_approvals(tool_name, trigger_kind, pattern))
        if count < MIN_CLEAN_APPROVALS:
            return None
        with self._lock:
            if any(r.get("pattern") == pattern for r in self.rules):
                return None
            old = next((o for o in self.offers if o.get("pattern") == pattern), None)
            if old is not None:
                if old.get("status") == "pending":
                    return None
                if count < int(old.get("declined_at_count") or 0) + MIN_CLEAN_APPROVALS:
                    return None
                self.offers.remove(old)
            clean = watches.validate_spec(spec)
            offer = {
                "id": f"ofr_{uuid.uuid4().hex[:10]}",
                "status": "pending",
                "trigger_kind": str(trigger_kind),
                "tool": tool_name,
                "spec": clean,
                "pattern": pattern,
                "words": describe_rule(clean),
                "evidence": _evidence(decisions, tool_name, trigger_kind, pattern, count),
                "created_at": float(self.clock()),
            }
            self.offers.append(offer)
            self.offers = self.offers[-50:]
            self._save()
            return dict(offer)

    def dismiss(self, offer_id: str) -> dict[str, Any]:
        with self._lock:
            offer = next((o for o in self.offers if o.get("id") == offer_id), None)
            if offer is None or offer.get("status") != "pending":
                raise RuleError("no such offer", 404)
            offer["status"] = "declined"
            offer["declined_at_count"] = int((offer.get("evidence") or {}).get("clean_approvals") or 0)
            self._save()
            return dict(offer)

    def accept(self, offer_id: str, decisions: Any) -> dict[str, Any]:
        """The user's yes (``POST /api/rules/offers/{id}/accept`` only).

        Re-checks every limit and the clean-approval count from the
        decision log now, so a stale or forged offer creates nothing.
        """
        with self._lock:
            offer = next((o for o in self.offers if o.get("id") == offer_id), None)
            if offer is None or offer.get("status") != "pending":
                raise RuleError("no such offer", 404)
            spec, tool, trigger = offer.get("spec"), str(offer.get("tool") or ""), str(offer.get("trigger_kind") or "")
            problems = spec_problems(spec)
            if not problems and ((spec.get("action") or {}).get("tool") != tool
                                 or arg_pattern(spec) != offer.get("pattern")):
                problems = ["the offer doesn't match its rule"]
            if problems:
                offer["status"] = "refused"
                self._save()
                raise RuleError("outside the standing-rule limits: " + "; ".join(problems))
            count = int(decisions.clean_approvals(tool, trigger, offer["pattern"])) if decisions is not None else 0
            if count < MIN_CLEAN_APPROVALS:
                offer["status"] = "stale"
                self._save()
                raise RuleError(f"needs {MIN_CLEAN_APPROVALS} approvals without changes; there are {count} now")
            if len(self.rules) >= MAX_RULES:
                raise RuleError(f"at most {MAX_RULES} rules; delete one first")
            now = float(self.clock())
            rule = {
                "id": f"rule_{uuid.uuid4().hex[:10]}",
                "trigger_kind": trigger,
                "tool": tool,
                "spec": watches.validate_spec(spec),
                "pattern": offer["pattern"],
                "words": describe_rule(spec),
                "enabled": True,
                "created_at": now,
                "disabled_reason": "",
                "evidence": {**(offer.get("evidence") or {}), "clean_approvals": count},
                "runs": 0,
                "last_run_at": None,
                "runs_today": {"date": "", "count": 0},
                "undos": 0,
                "undos_total": 0,
                "recent_keys": [],
            }
            self.rules.append(rule)
            offer["status"] = "accepted"
            offer["rule_id"] = rule["id"]
            self._save()
            return self.public(rule)

    # -- user changes (routes only) ------------------------------------------------

    def set_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            rule = self._find(rule_id)
            if rule is None:
                raise RuleError("no such rule", 404)
            if enabled and not rule.get("enabled"):
                problems = spec_problems(rule.get("spec"))
                if problems:
                    raise RuleError("outside the standing-rule limits: " + "; ".join(problems))
                rule["undos"] = 0  # switching back on starts a fresh undo count
                rule["disabled_reason"] = ""
            rule["enabled"] = bool(enabled)
            self._save()
            return self.public(rule)

    def delete(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.get("id") != rule_id]
            if len(self.rules) == before:
                return False
            self._save()
            return True

    # -- 3. undo listener -------------------------------------------------------

    def on_undo(self, entry: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        """``ActionJournal.add_undo_listener(rules.on_undo)``: a rule run that
        was undone counts; at :data:`UNDOS_TO_DISABLE` the rule switches off."""
        if (entry or {}).get("source") != "rule" or (result or {}).get("status") != "undone":
            return
        rule_id = str(entry.get("rule_id") or "")
        with self._lock:
            rule = self._find(rule_id)
            if rule is None:
                return
            rule["undos"] = int(rule.get("undos") or 0) + 1
            rule["undos_total"] = int(rule.get("undos_total") or 0) + 1
            if rule["undos"] >= UNDOS_TO_DISABLE and rule.get("enabled"):
                rule["enabled"] = False
                rule["disabled_reason"] = f"Switched off: you undid it {rule['undos']} times."
                logger.info("standing rule %s switched off after %d undos", rule_id, rule["undos"])
            self._save()

    # -- 2. the run-time hook ---------------------------------------------------

    def run_for_event(self, event: ContextEvent, *, journal: Any = None,
                      event_id: str = "", now: Optional[datetime] = None,
                      dynamic_names: Iterable[str] = (),
                      is_mcp: Callable[[str], bool] = lambda name: False) -> list[dict[str, Any]]:
        """Run the first enabled rule matching ``event``, without a card.

        Deterministic, no model: structured-field matching, arguments from a
        fixed template, the tool from the built-in table only. The denylist
        is checked again here, with the orchestrator's current generated
        tool names (``dynamic_names``) and MCP check (``is_mcp``).

        Returns ``[]`` (nothing ran: the normal path with its card goes on)
        or one ``{rule_id, tool, args, result, entry, words}``. A run whose
        tool failed has ``entry=None`` (not journaled, nothing changed): the
        caller then lets the normal path offer its card.
        """
        dynamic = frozenset(dynamic_names or ())
        if event.event_type not in RULE_EVENT_TYPES:
            return []
        try:
            if self.paused():
                return []
        except Exception:
            logger.exception("standing rules: pause check failed; not running")
            return []
        now = now or datetime.now().astimezone()
        with self._lock:
            candidates = [dict(r) for r in self.rules if r.get("enabled")]
        for rule in candidates:
            try:
                mcp = bool(is_mcp(str(rule.get("tool") or "")))
            except Exception:
                mcp = True  # can't tell -> treat as MCP (denied)
            if denied_reason(str(rule.get("tool") or ""), dynamic_names=dynamic, is_mcp=mcp):
                continue
            args = self._match(rule, event, now)
            if args is None:
                continue
            if not self._claim(rule["id"], event, now):
                continue
            return [self._run(rule, args, event, journal, event_id)]
        return []

    def _match(self, rule: Mapping[str, Any], event: ContextEvent, now: datetime) -> Optional[dict[str, str]]:
        spec = rule.get("spec") or {}
        tool = str(rule.get("tool") or "")
        if spec_problems(spec) or denied_reason(tool) or (spec.get("action") or {}).get("tool") != tool:
            return None
        fields = watches.event_fields(spec["trigger"], event, now)
        if fields is None:
            return None
        trigger_kind = str(rule.get("trigger_kind") or "")
        if trigger_kind.startswith(watches.TRIGGER_PREFIX):
            if self.watch_lookup is not None:
                watch = self.watch_lookup(trigger_kind[len(watches.TRIGGER_PREFIX):])
                if not watch or not watch.get("enabled"):
                    return None
        elif trigger_kind != event.event_type:
            # Offered from a built-in kind: the event must be that kind.
            try:
                kind = self.classify(event) if self.classify is not None else None
            except Exception:
                logger.exception("standing rules: classify failed")
                kind = None
            if kind != trigger_kind:
                return None
        args = watches.fill_arguments(spec["action"], fields)
        if tool == "move_file":
            path = str((event.metadata or {}).get("path") or "")
            if args.get("src") != path or not os.path.isfile(path) or os.path.islink(path):
                return None
        return args

    def _claim(self, rule_id: str, event: ContextEvent, now: datetime) -> bool:
        meta = event.metadata or {}
        key = hashlib.sha256(
            f"{event.event_type}|{meta.get('message_id') or meta.get('path') or ''}".encode()
        ).hexdigest()[:16]
        today = now.date().isoformat()
        with self._lock:
            rule = self._find(rule_id)
            if rule is None or not rule.get("enabled"):
                return False
            if key in (rule.get("recent_keys") or []):
                return False
            day = rule.get("runs_today") or {}
            count = int(day.get("count") or 0) if day.get("date") == today else 0
            if count >= MAX_RUNS_PER_RULE_PER_DAY:
                return False
            rule["runs_today"] = {"date": today, "count": count + 1}
            rule["recent_keys"] = ((rule.get("recent_keys") or []) + [key])[-MAX_RECENT_KEYS:]
            self._save()
            return True

    def _run(self, rule: Mapping[str, Any], args: dict[str, str], event: ContextEvent,
             journal: Any, event_id: str) -> dict[str, Any]:
        tool = str(rule["tool"])
        table = self._tools if self._tools is not None else builtin_tools()
        fn = table.get(tool)
        if fn is None:
            result: Any = {"status": "error", "error": f"no built-in tool {tool!r}"}
        else:
            try:
                result = _callable(fn)(**args)
            except Exception as exc:
                logger.exception("standing rule %s: %s failed", rule["id"], tool)
                result = {"status": "error", "error": str(exc)[:200]}
        entry = None
        if journal is not None:
            try:
                entry = journal.record(
                    tool, {"args": [], "kwargs": args}, result, event_id=event_id or "",
                    why=("Standing rule: " + str(rule.get("words") or ""))[:80],
                    source="rule", rule_id=rule["id"],
                )
            except Exception:
                logger.exception("standing rule %s: journaling failed", rule["id"])
        if entry is not None:
            with self._lock:
                live = self._find(rule["id"])
                if live is not None:
                    live["runs"] = int(live.get("runs") or 0) + 1
                    live["last_run_at"] = float(self.clock())
                    self._save()
        logger.info("standing rule %s ran %s on %s: %s", rule["id"], tool, event.event_type,
                    (result or {}).get("status") if isinstance(result, Mapping) else result)
        return {"rule_id": rule["id"], "tool": tool, "args": args, "result": result,
                "entry": entry, "words": rule.get("words") or ""}


def _evidence(decisions: Any, tool: str, trigger: str, pattern: Mapping[str, str], count: int) -> dict[str, Any]:
    """What led to the offer: the streak of clean approvals and when."""
    rows = [d for d in list(getattr(decisions, "decisions", None) or [])
            if d.get("tool_name") == tool and d.get("trigger_kind") == trigger
            and d.get("decision") == "approved" and d.get("arg_pattern") == dict(pattern)]
    times = [float(d.get("at") or 0.0) for d in rows[-count:]] if rows else []
    out: dict[str, Any] = {"clean_approvals": count, "tool": tool, "trigger_kind": trigger}
    if times:
        out["first_at"], out["last_at"] = min(times), max(times)
    out["text"] = (f"You approved this {count} times in a row without changing anything"
                   + (f" ({_day(out['first_at'])} – {_day(out['last_at'])})." if times else "."))
    return out


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %-d")


# ---- notices (the runner publishes them) -------------------------------------


def offer_notice(offer: Mapping[str, Any]) -> dict[str, Any]:
    """A ``NoticePayload`` dict telling the user an offer is waiting."""
    return {
        "id": f"rule_offer:{offer['id']}",
        "level": "info",
        "title": "Always do this?",
        "body": (f"{offer.get('words', '')} {(offer.get('evidence') or {}).get('text', '')} "
                 "Turn it on in Settings → Standing rules (or ignore this)."),
    }


def run_notice(run: Mapping[str, Any]) -> dict[str, Any]:
    """A ``NoticePayload`` dict for one automatic run, naming where to undo it."""
    entry = run.get("entry") or {}
    return {
        "id": f"rule_run:{entry.get('id') or run.get('rule_id')}",
        "level": "info",
        "title": "Done automatically",
        "body": (f"{entry.get('label') or run.get('tool')} (standing rule: {run.get('words', '')}) "
                 "Undo it in Settings → Standing rules or History."),
    }


def notification_text(run: Mapping[str, Any]) -> tuple[str, str]:
    """Title and body for the macOS notification of one automatic run."""
    entry = run.get("entry") or {}
    return ("Done automatically", f"{entry.get('label') or run.get('tool')}. Undo in Standing rules."[:240])


_default: Optional[StandingRules] = None


def default_store() -> StandingRules:
    global _default
    if _default is None:
        _default = StandingRules(default_path())
    return _default


__all__ = [
    "DENIED_TOOLS", "MIN_CLEAN_APPROVALS", "RULE_EVENT_TYPES", "RULE_TOOLS", "RuleError",
    "StandingRules", "UNDOS_TO_DISABLE", "arg_pattern", "denied_reason", "derive_spec",
    "describe_rule", "effective_kwargs", "notification_text", "offer_notice", "run_notice", "spec_problems",
]
