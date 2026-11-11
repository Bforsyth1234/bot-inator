"""What happens to each suggestion, and what the user decides on cards (#79).

Two local stores, both JSON in ``~/.bot-inator/`` (0600, atomic writes):

* :class:`OutcomeStore` (``idea_outcomes.json``): one row per idea offer.
  Source (open_help, friction, pattern, next_step, chat_gap, routine),
  idea kind (the trigger, e.g. ``pdf_download``), verdict (shown /
  approved / declined / timed_out) and, afterwards, how the result fared:
  run count and last run of the installed tool, disabled / deleted, and
  "undone soon after" (within :data:`UNDO_WINDOW_SECONDS`) where reported.
  From these it derives per-source and per-kind approval and kept rates,
  and two deterministic adjustments the idea sources query:
  :meth:`OutcomeStore.judge_threshold` and
  :meth:`OutcomeStore.cooldown_multiplier`. Tools approved but never run
  again are listed by :meth:`OutcomeStore.removal_candidates` after
  :data:`REMOVAL_AFTER_SECONDS` (30 days). No model call anywhere.

* :class:`DecisionLog` (``approval_decisions.json``): one row per approval
  card decision: tool, trigger kind, approved / denied / edited, edited
  field names with a short fact each ("shorter", "changed"), the note the
  user typed, and an optional fixed argument pattern. #78 reads
  :meth:`DecisionLog.clean_approvals` (">= 5 clean approvals") and #72
  reads :meth:`DecisionLog.denials` / :meth:`DecisionLog.edits_by_field`.

Privacy: only identifiers, field names, counts, times and short bounded
facts are stored; never descriptions, message text or email bodies. Every
string is truncated (:data:`MAX_LABEL`, :data:`MAX_NOTE`).

``path=None`` keeps a store in memory only (tests). ``clock`` is
injectable so tests can pass 30 days in an instant.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

DATA_DIR = Path.home() / ".bot-inator"

UNDO_WINDOW_SECONDS = 3600.0          # "undone soon after" = within an hour
KEPT_GRACE_SECONDS = 7 * 24 * 3600.0  # a tool not run within a week is not kept
REMOVAL_AFTER_SECONDS = 30 * 24 * 3600.0

# Adjustment rules (deterministic). A source or kind needs MIN_SETTLED
# approved offers whose fate is known before its kept rate counts.
MIN_SETTLED = 3
LOW_KEPT_RATE = 0.34   # < 1 in 3 kept -> +1 judge score, cooldown x2
VERY_LOW_KEPT_RATE = 0.15  # with VERY_LOW_MIN_SETTLED -> +2, cooldown x4
VERY_LOW_MIN_SETTLED = 5
MAX_JUDGE_SCORE = 10

MAX_OFFERS = 1000
MAX_DECISIONS = 2000
MAX_LABEL = 80
MAX_NOTE = 200
MAX_PATTERN_KEYS = 8

# "expired": a held offer (#87) that ran out of shelf life unseen; never
# counted as shown or decided.
VERDICTS = ("approved", "declined", "timed_out", "expired")
DECISIONS = ("approved", "denied", "edited")

_SOURCE_ALIASES = {"open-help": "open_help", "next-step": "next_step",
                   "chat-gap": "chat_gap", "": "pattern", "unknown": "pattern"}
_FIELD_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def default_outcomes_path() -> Path:
    return DATA_DIR / "idea_outcomes.json"


def default_decisions_path() -> Path:
    return DATA_DIR / "approval_decisions.json"


def _label(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_LABEL]


def _field(value: Any) -> str:
    return _FIELD_RE.sub("", str(value or ""))[:40]


def normalize_source(source: Any) -> str:
    """``open-help`` -> ``open_help``; a missing source is the pattern detector."""
    raw = _label(source).lower().replace(" ", "_")
    return _SOURCE_ALIASES.get(raw, raw)


def trigger_kind_of(event_type: str, metadata: Optional[Mapping[str, Any]] = None) -> str:
    """The trigger kind a decision is logged under.

    A ``pattern_detected`` idea uses its ``trigger`` (e.g. ``pdf_download``);
    any other event uses its event type (``file_created``, ``user_message``).
    """
    meta = metadata or {}
    if event_type == "pattern_detected" and meta.get("trigger"):
        return _label(meta.get("trigger"))
    return _label(event_type) or "unknown"


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


class _JsonStore:
    """Atomic, 0600 JSON persistence shared by both stores."""

    VERSION = 1

    def __init__(self, path: Optional[Path], clock: Optional[Clock]) -> None:
        self.path = Path(path) if path is not None else None
        self.clock: Clock = clock or time.time
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("%s unreadable; starting empty", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}-")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"version": self.VERSION, **data}, fh, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            logger.exception("failed to save %s", self.path)


# ---------------------------------------------------------------------------
# Offer outcomes
# ---------------------------------------------------------------------------


class OutcomeStore(_JsonStore):
    """Per-offer outcomes and the numbers derived from them."""

    def __init__(self, path: Optional[Path] = None, *, clock: Optional[Clock] = None) -> None:
        super().__init__(path, clock)
        raw = self._read().get("offers", [])
        self.offers: list[dict[str, Any]] = [o for o in raw if isinstance(o, dict)][-MAX_OFFERS:]

    def _save(self) -> None:
        self._write({"offers": self.offers})

    def _find(self, offer_id: str) -> Optional[dict[str, Any]]:
        for offer in reversed(self.offers):
            if offer.get("id") == offer_id:
                return offer
        return None

    def _latest_approved(self, tool: str) -> Optional[dict[str, Any]]:
        tool = _label(tool)
        for offer in reversed(self.offers):
            if offer.get("verdict") == "approved" and offer.get("tool") == tool:
                return offer
        return None

    # ---- recording -------------------------------------------------------

    def record_shown(
        self, source: Any, tool_name: str, *, idea_kind: str = "", one_shot: bool = False,
    ) -> str:
        """An idea card was sent. Returns the offer id for :meth:`record_verdict`.

        ``one_shot`` marks a built-in follow-up (one action, no installed
        tool): it is kept unless undone within the hour.
        """
        offer = {
            "id": f"off_{uuid.uuid4().hex[:12]}",
            "source": normalize_source(source),
            "idea_kind": _label(idea_kind) or "unknown",
            "tool_name": _label(tool_name),
            "tool": _label(tool_name),
            "one_shot": bool(one_shot),
            "shown_at": float(self.clock()),
            "verdict": "shown",
            "decided_at": None,
            "run_count": 0,
            "last_run_at": None,
            "disabled": False,
            "deleted": False,
            "undone_soon": False,
            "removal_dismissed": False,
        }
        with self._lock:
            self.offers.append(offer)
            self.offers = self.offers[-MAX_OFFERS:]
            self._save()
        return offer["id"]

    def record_verdict(self, offer_id: str, verdict: str) -> bool:
        """``approved`` / ``declined`` / ``timed_out``. False if unknown id."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        with self._lock:
            offer = self._find(offer_id)
            if offer is None:
                return False
            offer["verdict"] = verdict
            offer["decided_at"] = float(self.clock())
            self._save()
        return True

    def record_offer(
        self, source: Any, tool_name: str, verdict: str, *, idea_kind: str = "",
        one_shot: bool = False,
    ) -> str:
        """Shown and decided in one call; returns the offer id."""
        offer_id = self.record_shown(source, tool_name, idea_kind=idea_kind, one_shot=one_shot)
        self.record_verdict(offer_id, verdict)
        return offer_id

    def link_tool(self, offer_id: str, installed_name: str) -> bool:
        """The approved idea was installed under ``installed_name``."""
        with self._lock:
            offer = self._find(offer_id)
            if offer is None:
                return False
            offer["tool"] = _label(installed_name)
            self._save()
        return True

    def record_tool_run(self, tool_name: str) -> bool:
        """The installed tool ran (counted on its latest approved offer).

        Call it for runs after the install; the automatic post-install
        rerun is not a sign the user wants the tool. False when no
        approved offer installed a tool by that name.
        """
        with self._lock:
            offer = self._latest_approved(tool_name)
            if offer is None:
                return False
            offer["run_count"] = int(offer.get("run_count") or 0) + 1
            offer["last_run_at"] = float(self.clock())
            self._save()
        return True

    def _flag(self, tool_name: str, key: str) -> bool:
        with self._lock:
            offer = self._latest_approved(tool_name)
            if offer is None:
                return False
            offer[key] = True
            self._save()
        return True

    def record_disabled(self, tool_name: str) -> bool:
        return self._flag(tool_name, "disabled")

    def record_deleted(self, tool_name: str) -> bool:
        return self._flag(tool_name, "deleted")

    def record_undone(self, offer_id: str) -> bool:
        """The approved action was reversed (file moved back, event deleted).

        Counts only when reported within :data:`UNDO_WINDOW_SECONDS` of the
        approval; returns whether it counted.
        """
        with self._lock:
            offer = self._find(offer_id)
            if offer is None or offer.get("verdict") != "approved":
                return False
            if float(self.clock()) - float(offer.get("decided_at") or 0.0) > UNDO_WINDOW_SECONDS:
                return False
            offer["undone_soon"] = True
            self._save()
        return True

    def dismiss_removal(self, tool_name: str) -> bool:
        """The user chose to keep an unused tool: stop listing it."""
        return self._flag(tool_name, "removal_dismissed")

    # ---- derived numbers ------------------------------------------------

    def kept_status(self, offer: Mapping[str, Any]) -> Optional[bool]:
        """True kept, False not kept, None not approved or too early to say."""
        if offer.get("verdict") != "approved":
            return None
        if offer.get("disabled") or offer.get("deleted") or offer.get("undone_soon"):
            return False
        age = float(self.clock()) - float(offer.get("decided_at") or 0.0)
        if offer.get("one_shot"):
            return True if age >= UNDO_WINDOW_SECONDS else None
        if int(offer.get("run_count") or 0) > 0:
            return True
        return False if age >= KEPT_GRACE_SECONDS else None

    def _summarize(self, offers: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        row = {"shown": 0, "approved": 0, "declined": 0, "timed_out": 0,
               "kept": 0, "not_kept": 0, "pending": 0, "expired": 0}
        for offer in offers:
            verdict = offer.get("verdict")
            if verdict == "expired":
                # #87: never shown to the user; not a decision either.
                row["expired"] += 1
                continue
            row["shown"] += 1
            if verdict in VERDICTS:
                row[verdict] += 1
            if verdict == "approved":
                status = self.kept_status(offer)
                row["pending" if status is None else ("kept" if status else "not_kept")] += 1
        decided = row["approved"] + row["declined"] + row["timed_out"]
        row["approval_rate"] = _rate(row["approved"], decided)
        row["kept_rate"] = _rate(row["kept"], row["kept"] + row["not_kept"])
        return row

    def _group(self, key: str) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            for offer in self.offers:
                groups.setdefault(str(offer.get(key) or "unknown"), []).append(offer)
        return groups

    def stats_by_source(self) -> list[dict[str, Any]]:
        return [{"source": name, **self._summarize(rows), **self.adjustment(name)}
                for name, rows in sorted(self._group("source").items())]

    def stats_by_kind(self) -> list[dict[str, Any]]:
        return [{"idea_kind": name, **self._summarize(rows)}
                for name, rows in sorted(self._group("idea_kind").items())]

    def _bump(self, key: str, value: str) -> int:
        rows = self._group(key).get(value, [])
        summary = self._summarize(rows)
        settled = summary["kept"] + summary["not_kept"]
        rate = summary["kept_rate"]
        if rate is None or settled < MIN_SETTLED:
            return 0
        if rate < VERY_LOW_KEPT_RATE and settled >= VERY_LOW_MIN_SETTLED:
            return 2
        return 1 if rate < LOW_KEPT_RATE else 0

    def adjustment(self, source: Any, idea_kind: Optional[str] = None) -> dict[str, Any]:
        """``{threshold_bump, cooldown_multiplier}`` for a source (and kind).

        The larger of the source's and the kind's bump wins.
        """
        bump = self._bump("source", normalize_source(source))
        if idea_kind:
            bump = max(bump, self._bump("idea_kind", _label(idea_kind)))
        return {"threshold_bump": bump, "cooldown_multiplier": float(2 ** bump)}

    def judge_threshold(self, source: Any, base: int, idea_kind: Optional[str] = None) -> int:
        """The judge score an idea from ``source`` must reach (``base`` + bump, <= 10)."""
        return min(MAX_JUDGE_SCORE, int(base) + self.adjustment(source, idea_kind)["threshold_bump"])

    def cooldown_multiplier(self, source: Any, idea_kind: Optional[str] = None) -> float:
        """Multiply the source's cooldown by this (1, 2 or 4)."""
        return self.adjustment(source, idea_kind)["cooldown_multiplier"]

    def removal_candidates(self) -> list[dict[str, Any]]:
        """Installed tools approved >= 30 days ago and never run since."""
        now = float(self.clock())
        latest: dict[str, dict[str, Any]] = {}
        with self._lock:
            for offer in self.offers:
                if offer.get("verdict") == "approved" and not offer.get("one_shot"):
                    latest[str(offer.get("tool"))] = offer
        rows = []
        for tool, offer in latest.items():
            if offer.get("deleted") or offer.get("removal_dismissed"):
                continue
            if int(offer.get("run_count") or 0) > 0:
                continue
            approved_at = float(offer.get("decided_at") or 0.0)
            if now - approved_at < REMOVAL_AFTER_SECONDS:
                continue
            rows.append({"tool_name": tool, "source": offer.get("source"),
                         "idea_kind": offer.get("idea_kind"), "approved_at": approved_at,
                         "days_unused": int((now - approved_at) // 86400)})
        return sorted(rows, key=lambda r: r["approved_at"])


# ---------------------------------------------------------------------------
# Approval-decision log (#72, #78)
# ---------------------------------------------------------------------------


def edit_facts(original: Mapping[str, Any], edited: Mapping[str, Any]) -> dict[str, str]:
    """``{field: fact}`` for each kwarg the user changed on the card.

    Facts are ``shorter`` / ``longer`` / ``changed`` (same-length text,
    non-text values) / ``added`` / ``removed``. Values are never kept.
    """
    facts: dict[str, str] = {}
    for key in sorted(set(original) | set(edited)):
        name = _field(key)
        if not name:
            continue
        if key not in edited:
            facts[name] = "removed"
        elif key not in original:
            facts[name] = "added"
        elif original[key] != edited[key]:
            before, after = original[key], edited[key]
            if isinstance(before, str) and isinstance(after, str) and len(before) != len(after):
                facts[name] = "shorter" if len(after) < len(before) else "longer"
            else:
                facts[name] = "changed"
    return facts


def _pattern(arg_pattern: Optional[Mapping[str, Any]]) -> Optional[dict[str, str]]:
    if not arg_pattern:
        return None
    out: dict[str, str] = {}
    for key in sorted(arg_pattern)[:MAX_PATTERN_KEYS]:
        value = arg_pattern[key]
        if isinstance(value, (str, int, float, bool)) and _field(key):
            out[_field(key)] = _label(value)
    return out or None


class DecisionLog(_JsonStore):
    """Every approve / deny / edit on an approval card, as short facts."""

    def __init__(self, path: Optional[Path] = None, *, clock: Optional[Clock] = None) -> None:
        super().__init__(path, clock)
        raw = self._read().get("decisions", [])
        self.decisions: list[dict[str, Any]] = [d for d in raw if isinstance(d, dict)][-MAX_DECISIONS:]

    def record(
        self,
        tool_name: str,
        trigger_kind: str,
        decision: str,
        edited_field_names: Iterable[str] | Mapping[str, str] = (),
        note: str = "",
        arg_pattern: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Log one card decision; returns the stored row.

        ``edited_field_names`` is a list of names, or ``{name: fact}`` from
        :func:`edit_facts`. An approval with edited fields is stored as
        ``edited``; an ``edited`` with none as ``approved``.
        """
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        if isinstance(edited_field_names, Mapping):
            facts = {_field(k): _label(v) for k, v in edited_field_names.items() if _field(k)}
        else:
            facts = {_field(k): "changed" for k in edited_field_names if _field(k)}
        if decision != "denied":
            decision = "edited" if facts else "approved"
        row = {
            "tool_name": _label(tool_name),
            "trigger_kind": _label(trigger_kind) or "unknown",
            "decision": decision,
            "edited_fields": sorted(facts),
            "edit_facts": facts,
            "note": " ".join(str(note or "").split())[:MAX_NOTE],
            "arg_pattern": _pattern(arg_pattern),
            "at": float(self.clock()),
        }
        with self._lock:
            self.decisions.append(row)
            self.decisions = self.decisions[-MAX_DECISIONS:]
            self._write({"decisions": self.decisions})
        return dict(row)

    def _for(self, tool_name: str, trigger_kind: str) -> list[dict[str, Any]]:
        tool, trigger = _label(tool_name), _label(trigger_kind)
        with self._lock:
            return [d for d in self.decisions
                    if d.get("tool_name") == tool and d.get("trigger_kind") == trigger]

    def clean_approvals(
        self, tool_name: str, trigger_kind: str, arg_pattern: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Approvals with no edit since the last denial or edit (#78).

        Any denial or edit of this tool for this trigger resets the count,
        whatever its arguments. With ``arg_pattern`` only approvals with
        that exact pattern count.
        """
        want = _pattern(arg_pattern)
        count = 0
        for row in self._for(tool_name, trigger_kind):
            if row.get("decision") != "approved":
                count = 0
            elif want is None or row.get("arg_pattern") == want:
                count += 1
        return count

    def denials(self, tool_name: str, trigger_kind: str, *, since_last_approval: bool = False) -> int:
        """Denials of ``tool_name`` for ``trigger_kind`` (#72: 3 -> stop offering)."""
        count = 0
        for row in self._for(tool_name, trigger_kind):
            if row.get("decision") == "denied":
                count += 1
            elif since_last_approval:
                count = 0
        return count

    def edits_by_field(self, tool_name: Optional[str] = None) -> dict[str, dict[str, int]]:
        """``{field: {fact: count, "total": n}}`` over edited decisions."""
        tool = _label(tool_name) if tool_name else None
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            rows = [d for d in self.decisions if tool is None or d.get("tool_name") == tool]
        for row in rows:
            for name, fact in (row.get("edit_facts") or {}).items():
                counts = out.setdefault(name, {"total": 0})
                counts["total"] += 1
                counts[fact] = counts.get(fact, 0) + 1
        return out

    def notes(self, tool_name: Optional[str] = None) -> list[dict[str, Any]]:
        """Typed notes, oldest first: ``{tool_name, trigger_kind, decision, note, at}``."""
        tool = _label(tool_name) if tool_name else None
        with self._lock:
            return [{k: d.get(k) for k in ("tool_name", "trigger_kind", "decision", "note", "at")}
                    for d in self.decisions
                    if d.get("note") and (tool is None or d.get("tool_name") == tool)]

    def summary(self) -> list[dict[str, Any]]:
        """Per (tool, trigger) counts for the Debug tab."""
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        with self._lock:
            rows = list(self.decisions)
        for row in rows:
            key = (str(row.get("tool_name")), str(row.get("trigger_kind")))
            g = groups.setdefault(key, {"tool_name": key[0], "trigger_kind": key[1],
                                        "approved": 0, "denied": 0, "edited": 0})
            g[row.get("decision", "approved")] = g.get(row.get("decision", "approved"), 0) + 1
        for g in groups.values():
            g["clean_streak"] = self.clean_approvals(g["tool_name"], g["trigger_kind"])
        return sorted(groups.values(), key=lambda g: (g["tool_name"], g["trigger_kind"]))
