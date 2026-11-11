"""Learn preferences from approval-card decisions (#72).

Input: the #79 :class:`ai.idea_outcomes.DecisionLog` (tool, trigger kind,
approved / denied / edited, edited-field facts, typed note). Output:
memories saved with ``kind="preference", source="approval"`` so recall
feeds them into later prompts, listed and deletable in the app.

Rules (fully deterministic, fixed templates; no model call):

* **Stop offering.** :data:`DENIALS_TO_STOP` denials of one tool for one
  trigger kind, with no approval or edit in between, learn "Don't offer
  <tool> for <trigger> events" and :func:`is_suppressed` turns true for
  that pair. The runner calls it beside ``_previously_declined`` (the
  declined-idea check): a suppressed call is refused with the same
  ``user_declined_before`` error and does not run. Excluded:
  ``generate_custom_tool`` (each build idea is its own offer; #56/#57
  remember those by name) and chat turns (``user_message``): a request the
  user typed is never blocked by a learned rule.
* **Short drafts.** :data:`SHORTER_EDITS` or more approvals where the user
  made a draft field (:data:`DRAFT_FIELDS`) shorter, more shortened than
  lengthened, learn "keeps drafts short".
* **Notes.** A note typed on a card is saved verbatim.

A learned preference can stop an offer or shape a draft; it never lets a
call run without its approval card (invariant 7a): nothing here approves.

Idempotent: each preference has a stable ``key`` and is saved once. When
the user deletes one, :meth:`Memory.delete_preference` leaves a tombstone
and only decisions after it count toward learning it again.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

SOURCE = "approval"
KIND = "preference"
DENIALS_TO_STOP = 3
SHORTER_EDITS = 2
DRAFT_FIELDS = ("body", "message")
STOP_EXCLUDED_TOOLS = frozenset({"generate_custom_tool"})
STOP_EXCLUDED_TRIGGERS = frozenset({"user_message", "chat"})
RULE_STOP = "stop_offer"
RULE_SHORT = "short_drafts"
RULE_NOTE = "note"
MAX_RECALLED = 6


def _human(name: str) -> str:
    return " ".join(str(name or "").replace("_", " ").split())


def _note_key(note: str) -> str:
    norm = " ".join(note.lower().split())
    return "note:" + hashlib.sha256(norm.encode()).hexdigest()[:16]


def _rows(log: Any) -> list[dict[str, Any]]:
    lock = getattr(log, "_lock", None)
    if lock is None:
        return [dict(d) for d in getattr(log, "decisions", [])]
    with lock:
        return [dict(d) for d in log.decisions]


def derive(
    decisions: Iterable[Mapping[str, Any]], tombstones: Optional[Mapping[str, float]] = None,
) -> list[dict[str, Any]]:
    """Preferences the decisions support, oldest evidence first.

    Each: ``{key, text, metadata}``. A decision at or before a key's
    tombstone does not count toward that key.
    """
    tombs = dict(tombstones or {})
    rows = sorted(decisions, key=lambda d: float(d.get("at") or 0.0))

    def after(key: str, row: Mapping[str, Any]) -> bool:
        return float(row.get("at") or 0.0) > tombs.get(key, float("-inf"))

    out: list[dict[str, Any]] = []
    # Denial streaks per (tool, trigger).
    streaks: dict[tuple[str, str], int] = {}
    done: set[str] = set()
    for row in rows:
        tool, trigger = str(row.get("tool_name") or ""), str(row.get("trigger_kind") or "")
        if not tool or tool in STOP_EXCLUDED_TOOLS or trigger in STOP_EXCLUDED_TRIGGERS:
            continue
        key = f"{RULE_STOP}:{tool}:{trigger}"
        if not after(key, row):
            continue
        if row.get("decision") != "denied":
            streaks[(tool, trigger)] = 0
            continue
        n = streaks.get((tool, trigger), 0) + 1
        streaks[(tool, trigger)] = n
        if n >= DENIALS_TO_STOP and key not in done:
            done.add(key)
            out.append({
                "key": key,
                "text": (f"Don't offer {_human(tool)} ({tool}) for {_human(trigger)} "
                         f"events: the user denied it {DENIALS_TO_STOP} times in a row."),
                "metadata": {"rule": RULE_STOP, "tool_name": tool, "trigger_kind": trigger},
            })

    short_key = f"{RULE_SHORT}:draft"
    shorter = longer = 0
    for row in rows:
        if not after(short_key, row):
            continue
        facts = row.get("edit_facts") or {}
        for field in DRAFT_FIELDS:
            if facts.get(field) == "shorter":
                shorter += 1
            elif facts.get(field) == "longer":
                longer += 1
    if shorter >= SHORTER_EDITS and shorter > longer:
        out.append({
            "key": short_key,
            "text": ("The user keeps drafts short: they shortened a drafted message "
                     f"on the approval card {shorter} times. Keep drafts brief."),
            "metadata": {"rule": RULE_SHORT},
        })

    seen_notes: set[str] = set()
    for row in rows:
        note = " ".join(str(row.get("note") or "").split())
        if not note:
            continue
        key = _note_key(note)
        if key in seen_notes or not after(key, row):
            continue
        seen_notes.add(key)
        out.append({
            "key": key,
            "text": note,
            "metadata": {"rule": RULE_NOTE, "tool_name": str(row.get("tool_name") or "")},
        })
    return out


def learned(memory: Any) -> list[dict[str, Any]]:
    """Learned preferences in memory, newest first."""
    return memory.list_preferences(source=SOURCE)


def learn(log: Any, memory: Any) -> list[dict[str, Any]]:
    """Save every preference the log supports that memory lacks.

    Returns the newly saved ``{id, key, text, metadata}`` rows. The runner
    calls it after each ``approval_decisions.record`` (off the event loop:
    saving embeds the text). Errors are logged, never raised.
    """
    if log is None or memory is None:
        return []
    try:
        have = {str(r["metadata"].get("key")) for r in learned(memory)}
        candidates = derive(_rows(log), memory.preference_tombstones())
        saved = []
        for pref in candidates:
            if pref["key"] in have:
                continue
            meta = {"kind": KIND, "source": SOURCE, "key": pref["key"], **pref["metadata"]}
            rowid = memory.save_memory(pref["text"], meta)
            have.add(pref["key"])
            saved.append({"id": rowid, "key": pref["key"], "text": pref["text"], "metadata": meta})
            logger.info("Learned preference %s", pref["key"])
        return saved
    except Exception:
        logger.exception("preference learning failed")
        return []


def suppressed_offers(memory: Any) -> set[tuple[str, str]]:
    """``{(tool_name, trigger_kind)}`` with an active stop-offer preference."""
    if memory is None:
        return set()
    try:
        rows = learned(memory)
    except Exception:
        logger.exception("could not read learned preferences")
        return set()
    return {
        (str(r["metadata"].get("tool_name")), str(r["metadata"].get("trigger_kind")))
        for r in rows if r["metadata"].get("rule") == RULE_STOP
    }


def is_suppressed(memory: Any, tool_name: str, trigger_kind: str) -> bool:
    """True when the user's denials say: don't offer ``tool_name`` for
    ``trigger_kind``. Never true for a chat turn or a build card."""
    if tool_name in STOP_EXCLUDED_TOOLS or trigger_kind in STOP_EXCLUDED_TRIGGERS:
        return False
    return (str(tool_name), str(trigger_kind)) in suppressed_offers(memory)


def refusal(tool_name: str, trigger_kind: str) -> dict[str, Any]:
    """The tool result for a suppressed call (same shape as a declined idea)."""
    return {
        "status": "error",
        "error": "user_declined_before",
        "declined_tool": tool_name,
        "message": (
            f"The user has denied {tool_name} for {trigger_kind} events several "
            "times. Do not offer it; reply 'no action' or answer without it."
        ),
    }


def recall_with_preferences(memory: Any, query: str, top_k: int = 3) -> list[str]:
    """``memory.recall_memory(query, top_k)`` plus learned preferences
    (newest first, at most :data:`MAX_RECALLED`) not already recalled.

    Similarity search alone may miss "keeps drafts short" on a draft
    prompt; learned preferences are few and always relevant.
    """
    recalled = list(memory.recall_memory(query, top_k))
    try:
        extra = [r["text"] for r in learned(memory)][:MAX_RECALLED]
    except Exception:
        logger.exception("could not read learned preferences")
        extra = []
    return recalled + [t for t in extra if t not in recalled]
