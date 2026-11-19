"""Weekly report: what bot-inator did for you this week (#128).

Three parts:

* :func:`gather_week` builds one week's payload from the local stores, all
  read-only: the action journal (#83), the offer outcomes and the card
  decisions (#79), the watches (#88) and the generated tools' provenance
  records (#61). :func:`format_report` turns a payload into plain text.
  Counts and tool names only. No model call anywhere, so nothing leaves the
  Mac and nothing can be invented. There is no "time saved" number: nothing
  measures one.
* :class:`WeeklyReportScheduler` publishes one
  ``ContextEvent(event_type="weekly_report", metadata=<payload>)`` on the
  chosen weekday at the chosen local time (default Friday 16:00). If the
  time was missed (Mac asleep, daemon down) it fires at the next check the
  same day only. The last run is kept in ``~/.bot-inator/weekly_report.json``
  so a restart doesn't repeat it. A week with nothing in it sends no event.
* :func:`weekly_report` is the read-only tool Chat uses for "what did you do
  for me this week?".

A "week" is the 7 days ending at the moment of the report (``weeks_ago=1``
is the 7 days before that), so a report on any weekday covers every day
exactly once and nothing falls between two reports.

Where each number comes from:

* Actions run, per tool, run without asking, undone: ``ActionJournal.rows``
  filtered by ``created_at``. ``source`` is ``"rule"`` (standing rule, #78)
  or ``"always_allow"`` (#116) for a run without a card; ``undone_at`` says
  it was taken back. The journal answers with its newest
  ``MAX_ROWS`` rows at most; when that cuts into the week the count is
  given as "at least".
* Approval cards answered: ``DecisionLog.decisions`` filtered by ``at``
  (``DecisionLog.summary`` has no time range). The log has answered cards
  only, so there is no "cards shown" number.
* Offers shown / approved / declined: ``OutcomeStore.offers`` filtered by
  ``shown_at`` (an ``expired`` offer was never shown and is counted apart).
* Tools installed / repaired: ``installed_at`` and ``repairs[].at`` in
  ``<generated>/.provenance/<tool>.json``. A tool deleted since has no
  record left and is not counted.
* Tools never run again: approved tool offers with no run whose first week
  (``KEPT_GRACE_SECONDS``, the store's own "not kept" rule) ended in this
  week.
* Watches that fired: offers with source ``watch`` shown in the week, plus
  watches whose ``last_fired_at`` is in the week.
* Advice (one line at most): the idea source with the lowest approval rate
  over the last :data:`ADVICE_WEEKS` weeks, only with ``MIN_SETTLED``
  verdicts or more and only when under half were approved.

A source that is missing or fails is named as unavailable with its reason;
it is never guessed and never dropped.

Read-only (invariant 7a): nothing here asks for approval, posts a
notification or changes a store. The only file written is the scheduler's
own state file. The orchestrator posts the notification and the thought
(:func:`describe`, :func:`notification_text`).

Settings (``user_settings``, read on every check, with the environment as
the fallback): ``AGENT_WEEKLY_REPORT`` ("1"/"0", default on),
``AGENT_WEEKLY_REPORT_DAY`` ("mon".."sun", default "fri") and
``AGENT_WEEKLY_REPORT_TIME`` ("HH:MM", default "16:00").
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .event_bus import ContextEvent, EventBus

try:
    from smolagents import tool  # type: ignore
except Exception:  # pragma: no cover - smolagents is a hard dependency
    def tool(func):  # type: ignore
        return func

logger = logging.getLogger(__name__)

EVENT_TYPE = "weekly_report"
ENABLED_KEY = "AGENT_WEEKLY_REPORT"
DAY_KEY = "AGENT_WEEKLY_REPORT_DAY"
TIME_KEY = "AGENT_WEEKLY_REPORT_TIME"
DEFAULT_DAY = "fri"
DEFAULT_TIME = "16:00"
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_STATE_PATH = Path.home() / ".bot-inator" / "weekly_report.json"
DEFAULT_POLL_INTERVAL = 30.0  # seconds
# Wait before the first check so main.py has handed over the stores
# (:func:`configure`) when a missed report is caught up at daemon start.
DEFAULT_STARTUP_DELAY = 45.0  # seconds

WEEK = timedelta(days=7)
MAX_WEEKS_AGO = 3          # the journal keeps 30 days
VIEW_WEEKS = 4             # Settings shows the last four weeks
ADVICE_WEEKS = 4
ADVICE_MAX_RATE = 0.5      # advice only when under half were approved
MAX_NAMES = 5              # tool names listed on one line
NOTIFICATION_CHARS = 240
JOURNAL_LIMIT = 500        # ai.action_journal.MAX_ROWS
PROVENANCE_DIRNAME = ".provenance"  # tools.meta_tool_generator.PROVENANCE_DIRNAME
AUTO_SOURCES: tuple[tuple[str, str], ...] = (
    ("rule", "standing rules"),
    ("always_allow", "always allow"),
)
WATCH_SOURCE = "watch"            # ai.watches.SOURCE_NAME
WATCH_KIND_PREFIX = "watch:"      # ai.watches.TRIGGER_PREFIX
# Sources the user asked for themselves: never "turn them off?".
NO_ADVICE_SOURCES = frozenset({"chat"})
# The switch for a source, where one exists.
SOURCE_SETTINGS: dict[str, str] = {"open_help": "AGENT_OPEN_HELP", WATCH_SOURCE: "Settings > Watches"}

# (payload key, label): the order of the report's lines.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("actions", "Actions run"),
    ("cards", "Approval cards answered"),
    ("offers", "Offers"),
    ("tools", "Tools installed or repaired"),
    ("unused_tools", "Tools never run again"),
    ("watches", "Watches that fired"),
)
NOT_CONNECTED = "not connected yet"


def _now() -> datetime:
    return datetime.now().astimezone()


# ---- settings ----------------------------------------------------------------

def _setting(key: str, default: str) -> str:
    """Shell export, then the settings file (read live), then .env, then default."""
    import user_settings

    if key not in user_settings.shell_exported:
        saved = user_settings.read().get(key)
        if saved:
            return saved
    return os.environ.get(key) or default


def parse_time(value: Any) -> Optional[dtime]:
    """``"HH:MM"`` (24 h) as a time, or None when it isn't one."""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        return None
    return dtime(hour, minute)


def parse_weekday(value: Any) -> Optional[int]:
    """``"fri"`` / ``"Friday"`` as 0 (Monday) .. 6 (Sunday), else None."""
    text = str(value or "").strip().lower()
    if len(text) < 3 or text[:3] not in WEEKDAYS:
        return None
    full = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    index = WEEKDAYS.index(text[:3])
    return index if text in (WEEKDAYS[index], full[index]) else None


def report_enabled() -> bool:
    return _setting(ENABLED_KEY, "1").strip() != "0"


def report_weekday() -> int:
    day = parse_weekday(_setting(DAY_KEY, DEFAULT_DAY))
    return WEEKDAYS.index(DEFAULT_DAY) if day is None else day


def report_weekday_text() -> str:
    return WEEKDAYS[report_weekday()]


def report_time() -> dtime:
    return parse_time(_setting(TIME_KEY, DEFAULT_TIME)) or parse_time(DEFAULT_TIME)  # type: ignore[return-value]


def report_time_text() -> str:
    return report_time().strftime("%H:%M")


# ---- the stores main.py hands over ---------------------------------------------

_SOURCES: dict[str, Any] = {
    "journal": None, "outcomes": None, "decisions": None, "watches": None, "generated_dir": None,
}


def configure(
    *,
    journal: Any = None,
    outcomes: Any = None,
    decisions: Any = None,
    watches: Any = None,
    generated_dir: Path | str | None = None,
) -> None:
    """Hand over the daemon's stores (main.py, once, after it built them).

    ``journal`` is the ``ai.action_journal.ActionJournal``, ``outcomes`` the
    ``ai.idea_outcomes.OutcomeStore``, ``decisions`` the ``DecisionLog``,
    ``watches`` the ``ai.watches.WatchStore`` and ``generated_dir`` the
    generated tools folder. Whatever is left out is reported as unavailable.
    """
    _SOURCES.update(
        journal=journal, outcomes=outcomes, decisions=decisions, watches=watches,
        generated_dir=Path(generated_dir).expanduser() if generated_dir else None,
    )


def configured_sources() -> dict[str, Any]:
    return dict(_SOURCES)


# ---- gathering -----------------------------------------------------------------

def _unavailable(reason: Any) -> dict[str, Any]:
    return {"status": "unavailable", "reason": str(reason) or "no answer"}


def _guard(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A section, or "unavailable" with the reason. Never raises."""
    try:
        return fn()
    except Exception as exc:  # one store must never sink the whole report
        logger.exception("weekly report: a source failed")
        return _unavailable(str(exc) or exc.__class__.__name__)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _in(value: Any, start: float, end: float) -> bool:
    """``start < value <= end`` (epoch seconds); False when it isn't a time."""
    stamp = _number(value)
    return stamp is not None and start < stamp <= end


def _iso_stamp(value: Any) -> Optional[float]:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return (stamp if stamp.tzinfo is not None else stamp.astimezone()).timestamp()


def _actions(journal: Any, start: float, end: float) -> dict[str, Any]:
    if journal is None:
        return _unavailable(NOT_CONNECTED)
    rows = list(journal.rows(limit=JOURNAL_LIMIT))
    # Newest first, capped: if the cap was hit before the week's first day,
    # older actions of this week are missing from the answer.
    stamps = [s for s in (_number(r.get("created_at")) for r in rows) if s is not None]
    truncated = len(rows) >= JOURNAL_LIMIT and bool(stamps) and min(stamps) > start
    week = [r for r in rows if _in(r.get("created_at"), start, end)]
    by_tool: dict[str, int] = {}
    for row in week:
        name = str(row.get("tool") or "unknown")
        by_tool[name] = by_tool.get(name, 0) + 1
    auto = {key: 0 for key, _ in AUTO_SOURCES}
    auto_undone = 0
    for row in week:
        source = row.get("source")
        if source in auto:
            auto[source] += 1
            auto_undone += row.get("undone_at") is not None
    return {
        "status": "ok",
        "total": len(week),
        "truncated": truncated,
        "by_tool": [{"tool": t, "count": c} for t, c in sorted(by_tool.items(), key=lambda i: (-i[1], i[0]))],
        "undone": sum(1 for r in week if r.get("undone_at") is not None),
        "auto": {**auto, "total": sum(auto.values()), "undone": auto_undone},
    }


def _cards(decisions: Any, start: float, end: float) -> dict[str, Any]:
    if decisions is None:
        return _unavailable(NOT_CONNECTED)
    counts = {"approved": 0, "edited": 0, "denied": 0}
    for row in list(decisions.decisions):
        if _in(row.get("at"), start, end) and row.get("decision") in counts:
            counts[row["decision"]] += 1
    return {"status": "ok", "answered": sum(counts.values()), **counts}


def _offer_rows(outcomes: Any) -> list[dict[str, Any]]:
    return [o for o in list(outcomes.offers) if isinstance(o, Mapping)]


def _offers(outcomes: Any, start: float, end: float) -> dict[str, Any]:
    if outcomes is None:
        return _unavailable(NOT_CONNECTED)
    row = {"shown": 0, "approved": 0, "declined": 0, "timed_out": 0, "open": 0, "expired": 0}
    for offer in _offer_rows(outcomes):
        if not _in(offer.get("shown_at"), start, end):
            continue
        verdict = offer.get("verdict")
        if verdict == "expired":  # #87: held back and never shown
            row["expired"] += 1
            continue
        row["shown"] += 1
        row[verdict if verdict in ("approved", "declined", "timed_out") else "open"] += 1
    return {"status": "ok", **row}


def _unused_tools(outcomes: Any, start: float, end: float) -> dict[str, Any]:
    if outcomes is None:
        return _unavailable(NOT_CONNECTED)
    from ai.idea_outcomes import KEPT_GRACE_SECONDS

    names = sorted({
        str(o.get("tool") or o.get("tool_name") or "unknown")
        for o in _offer_rows(outcomes)
        if o.get("verdict") == "approved" and not o.get("one_shot") and not o.get("deleted")
        and int(o.get("run_count") or 0) == 0
        and _number(o.get("decided_at")) is not None
        and _in(float(o["decided_at"]) + KEPT_GRACE_SECONDS, start, end)
    })
    return {"status": "ok", "count": len(names), "names": names}


def read_provenance(generated_dir: Path | str) -> list[dict[str, Any]]:
    """``[{tool_name, installed_at, repairs: [at, ...]}]`` (epoch seconds) from
    the generated tools' provenance records. Read-only; raises ``OSError``
    when the folder can't be listed."""
    folder = Path(generated_dir) / PROVENANCE_DIRNAME
    if not folder.exists():
        if not Path(generated_dir).is_dir():
            raise FileNotFoundError(f"no generated tools folder at {generated_dir}")
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("weekly report: unreadable provenance %s", path.name)
            continue
        if not isinstance(data, dict):
            continue
        repairs = data.get("repairs") if isinstance(data.get("repairs"), list) else []
        out.append({
            "tool_name": str(data.get("tool_name") or path.stem),
            "installed_at": _iso_stamp(data.get("installed_at")),
            "repairs": [s for s in (_iso_stamp(r.get("at")) for r in repairs if isinstance(r, dict)) if s is not None],
        })
    return out


def _tools(generated_dir: Any, tool_records: Optional[Callable[[], list[dict[str, Any]]]],
           start: float, end: float) -> dict[str, Any]:
    if tool_records is None:
        if generated_dir is None:
            return _unavailable(NOT_CONNECTED)
        records = read_provenance(generated_dir)
    else:
        records = tool_records()
    installed = sorted(r["tool_name"] for r in records if _in(r.get("installed_at"), start, end))
    repaired = sorted(
        r["tool_name"] for r in records if any(_in(s, start, end) for s in r.get("repairs") or [])
    )
    return {"status": "ok", "installed": installed, "repaired": repaired}


def _watches(watches: Any, outcomes: Any, start: float, end: float) -> dict[str, Any]:
    if watches is None and outcomes is None:
        return _unavailable(NOT_CONNECTED)
    fired: set[str] = set()
    cards = 0
    if outcomes is not None:
        for offer in _offer_rows(outcomes):
            if offer.get("source") != WATCH_SOURCE or not _in(offer.get("shown_at"), start, end):
                continue
            kind = str(offer.get("idea_kind") or "")
            fired.add(kind[len(WATCH_KIND_PREFIX):] if kind.startswith(WATCH_KIND_PREFIX) else kind)
            cards += offer.get("verdict") != "expired"
    if watches is not None:
        for watch in watches.list():
            if _in(watch.get("last_fired_at"), start, end):
                fired.add(str(watch.get("id")))
    return {"status": "ok", "fired": len(fired), "cards": cards}


def _advice(outcomes: Any, end: float) -> Optional[dict[str, Any]]:
    """The source with the lowest approval rate, or None (see the module doc)."""
    if outcomes is None:
        return None
    from ai.idea_outcomes import MIN_SETTLED

    start = end - ADVICE_WEEKS * WEEK.total_seconds()
    by_source: dict[str, dict[str, int]] = {}
    for offer in _offer_rows(outcomes):
        verdict = offer.get("verdict")
        if verdict not in ("approved", "declined", "timed_out") or not _in(offer.get("shown_at"), start, end):
            continue
        row = by_source.setdefault(str(offer.get("source") or "unknown"), {"approved": 0, "settled": 0})
        row["settled"] += 1
        row["approved"] += verdict == "approved"
    candidates = [
        (row["approved"] / row["settled"], -row["settled"], source, row)
        for source, row in by_source.items()
        if source not in NO_ADVICE_SOURCES and row["settled"] >= MIN_SETTLED
        and row["approved"] / row["settled"] < ADVICE_MAX_RATE
    ]
    if not candidates:
        return None
    rate, _, source, row = min(candidates)
    return {
        "source": source,
        "approved": row["approved"],
        "settled": row["settled"],
        "approval_rate": round(rate, 3),
        "weeks": ADVICE_WEEKS,
        "setting": SOURCE_SETTINGS.get(source, ""),
    }


def _day(value: datetime) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _names(names: list[str]) -> str:
    if not names:
        return ""
    more = len(names) - MAX_NAMES
    return " (" + ", ".join(names[:MAX_NAMES]) + (f", {more} more" if more > 0 else "") + ")"


def is_empty(payload: Mapping[str, Any]) -> bool:
    """True when no available source counted anything in the week."""
    def ok(key: str) -> Mapping[str, Any]:
        section = payload.get(key) or {}
        return section if section.get("status") == "ok" else {}

    return not any((
        ok("actions").get("total"),
        ok("cards").get("answered"),
        ok("offers").get("shown"), ok("offers").get("expired"),
        ok("tools").get("installed"), ok("tools").get("repaired"),
        ok("unused_tools").get("count"),
        ok("watches").get("fired"),
    ))


def _action_lines(actions: Mapping[str, Any]) -> list[str]:
    per_tool = ", ".join(f"{r['tool']} {r['count']}" for r in actions["by_tool"])
    total = f"{'at least ' if actions.get('truncated') else ''}{actions['total']}"
    lines = [f"Actions run: {total}" + (f" ({per_tool})" if per_tool else "")]
    auto = actions["auto"]
    parts = ", ".join(f"{label} {auto[key]}" for key, label in AUTO_SOURCES if auto[key])
    line = f"Run without asking: {auto['total']}" + (f" ({parts})" if parts else "")
    if auto["total"]:
        line += f", {auto['undone']} undone"
    return lines + [line]


def _section_lines(key: str, section: Mapping[str, Any]) -> list[str]:
    if key == "actions":
        return _action_lines(section)
    if key == "cards":
        approved = section["approved"] + section["edited"]
        edited = f" ({section['edited']} after an edit)" if section["edited"] else ""
        return [f"Approval cards answered: {section['answered']} "
                f"({approved} approved{edited}, {section['denied']} declined)"]
    if key == "offers":
        line = (f"Offers: {section['shown']} shown, {section['approved']} approved, "
                f"{section['declined']} declined")
        waiting = section["timed_out"] + section["open"]
        if waiting:
            line += f", {waiting} not answered"
        if section["expired"]:
            line += f"; {section['expired']} held back and never shown"
        return [line]
    if key == "tools":
        return [f"Tools installed: {len(section['installed'])}{_names(section['installed'])}",
                f"Tools repaired: {len(section['repaired'])}{_names(section['repaired'])}"]
    if key == "unused_tools":
        return [f"Tools never run again: {section['count']}{_names(section['names'])}"]
    cards = f" ({_plural(section['cards'], 'card')})" if section["fired"] else ""
    return [f"Watches that fired: {section['fired']}{cards}"]


def advice_line(advice: Mapping[str, Any]) -> str:
    source = str(advice["source"]).replace("_", " ")
    line = (f"Advice: in the last {advice['weeks']} weeks you approved {advice['approved']} of "
            f"{advice['settled']} {source} offers. Turn them off?")
    return line + (f" ({advice['setting']})" if advice.get("setting") else "")


def format_report(payload: Mapping[str, Any]) -> str:
    """The plain-text report for a :func:`gather_week` payload."""
    missing = [label for key, label in SECTIONS if (payload.get(key) or {}).get("status") != "ok"]
    if payload.get("empty"):
        line = f"Nothing to report for {payload['label']}: no actions ran and no offers were shown."
        return line + (f" Unavailable: {', '.join(missing)}." if missing else "")
    lines = [f"bot-inator, {payload['label']}"]
    for key, label in SECTIONS:
        section = payload.get(key) or {}
        if section.get("status") != "ok":
            lines.append(f"{label}: unavailable: {section.get('reason') or 'no answer'}")
        else:
            lines += _section_lines(key, section)
    if payload.get("advice"):
        lines.append(advice_line(payload["advice"]))
    return "\n".join(lines)


def gather_week(
    now: Optional[datetime] = None,
    journal: Any = None,
    outcomes: Any = None,
    decisions: Any = None,
    watches: Any = None,
    *,
    generated_dir: Path | str | None = None,
    tool_records: Optional[Callable[[], list[dict[str, Any]]]] = None,
    weeks_ago: int = 0,
) -> dict[str, Any]:
    """One week's report payload from the given stores. Never raises.

    The week is the 7 days ending at ``now`` minus ``weeks_ago`` weeks. A
    store left as ``None`` is reported as unavailable. ``tool_records``
    (tests) answers like :func:`read_provenance`. Each section is
    ``{"status": "ok", ...}`` or ``{"status": "unavailable", "reason"}``;
    ``unavailable`` lists the labels of the missing ones, ``empty`` says the
    week had nothing in it and ``text`` is the whole report.
    """
    now = now or _now()
    weeks_ago = max(0, min(int(weeks_ago or 0), MAX_WEEKS_AGO))
    end_at = now - weeks_ago * WEEK
    start_at = end_at - WEEK
    start, end = start_at.timestamp(), end_at.timestamp()
    sections = {
        "actions": _guard(lambda: _actions(journal, start, end)),
        "cards": _guard(lambda: _cards(decisions, start, end)),
        "offers": _guard(lambda: _offers(outcomes, start, end)),
        "tools": _guard(lambda: _tools(generated_dir, tool_records, start, end)),
        "unused_tools": _guard(lambda: _unused_tools(outcomes, start, end)),
        "watches": _guard(lambda: _watches(watches, outcomes, start, end)),
    }
    try:
        advice = _advice(outcomes, end)
    except Exception:
        logger.exception("weekly report: advice failed")
        advice = None
    payload: dict[str, Any] = {
        "weeks_ago": weeks_ago,
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "label": f"{_day(start_at)} – {_day(end_at)}",
        **sections,
        "advice": advice,
        "unavailable": [label for key, label in SECTIONS if sections[key]["status"] != "ok"],
    }
    payload["empty"] = is_empty(payload)
    payload["text"] = format_report(payload)
    return payload


def report_for(weeks_ago: int = 0, now: Optional[datetime] = None) -> dict[str, Any]:
    """The payload for one week from the stores given to :func:`configure`."""
    return gather_week(now or _now(), **configured_sources(), weeks_ago=weeks_ago)


def recent_weeks(count: int = VIEW_WEEKS, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """This week first, then the weeks before it (at most ``MAX_WEEKS_AGO + 1``)."""
    now = now or _now()
    return [report_for(w, now) for w in range(max(1, min(int(count), MAX_WEEKS_AGO + 1)))]


@tool
def weekly_report(weeks_ago: int = 0) -> dict[str, Any]:
    """Get the weekly report: what bot-inator did for the user in a week, as counts.

    Use when the user asks what you did for them this week (or last week).
    Read-only. Covers actions run per tool, actions run without asking and
    how many were undone, approval cards and offers answered, tools
    installed or repaired, and watches that fired. Give the user the
    numbers from ``report`` as they are; keep any "unavailable" line and
    never add a number that is not there.

    Args:
        weeks_ago: 0 for the last 7 days, 1 for the 7 days before, up to 3.

    Returns:
        A dict with ``status`` ("ok"), ``report`` (plain text), ``week`` (the days covered) and ``empty``.
    """
    payload = report_for(weeks_ago)
    return {"status": "ok", "report": payload["text"], "week": payload["label"], "empty": payload["empty"]}


# ---- what the orchestrator says about the event --------------------------------

def describe(event: ContextEvent) -> str:
    """One line for the ``event_received`` thought."""
    meta = event.metadata or {}

    def count(key: str, field: str, noun: str) -> str:
        section = meta.get(key) or {}
        if section.get("status") != "ok":
            return f"{noun}s unavailable"
        return _plural(int(section.get(field) or 0), noun)

    text = f"Weekly report for {meta.get('label', 'this week')}: {count('actions', 'total', 'action')}"
    actions = meta.get("actions") or {}
    if actions.get("status") == "ok":
        text += f" ({(actions.get('auto') or {}).get('total', 0)} without asking)"
    return f"{text}, {count('offers', 'shown', 'offer')} shown, {count('cards', 'answered', 'card')} answered"


def notification_text(event: ContextEvent) -> tuple[str, str]:
    """``(title, body)`` for the macOS notification: the report without its
    first line, cut to :data:`NOTIFICATION_CHARS`."""
    meta = event.metadata or {}
    lines = str(meta.get("text") or "").splitlines()
    body = "\n".join(lines[1:] if len(lines) > 1 else lines)
    return f"Your week with bot-inator ({meta.get('label', 'this week')})", body[:NOTIFICATION_CHARS]


# ---- scheduler -----------------------------------------------------------------

def _read_last_run(path: Path) -> Optional[datetime]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_run")
        stamp = datetime.fromisoformat(str(value))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return stamp if stamp.tzinfo is not None else stamp.astimezone()


def _write_last_run(path: Path, stamp: datetime, sent: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".weekly-report-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_run": stamp.isoformat(), "sent": bool(sent)}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_event(payload: dict[str, Any], trigger: str = "schedule") -> ContextEvent:
    return ContextEvent(event_type=EVENT_TYPE, metadata={**payload, "trigger": trigger})


class WeeklyReportScheduler:
    """Publish one ``weekly_report`` event a week, on the set weekday and time.

    Fires when today is the set weekday and ``last run < today's report time
    <= now``: once that day, not again after a restart, and at the next
    check when the time was missed (same day only; a report missed for a
    whole day waits for next week). A week with nothing in it publishes no
    event but still counts as the run, so it isn't gathered again all day.
    """

    def __init__(
        self,
        bus: EventBus,
        state_path: Path | str | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        clock: Callable[[], datetime] = _now,
        gather: Optional[Callable[[datetime], dict[str, Any]]] = None,
        enabled: Callable[[], bool] = report_enabled,
        weekday: Callable[[], int] = report_weekday,
        time_of_day: Callable[[], dtime] = report_time,
        startup_delay: float = DEFAULT_STARTUP_DELAY,
    ) -> None:
        self._bus = bus
        self._state_path = Path(state_path).expanduser() if state_path else DEFAULT_STATE_PATH
        self._poll_interval = poll_interval
        self._clock = clock
        self._gather = gather or (lambda now: report_for(0, now))
        self._enabled = enabled
        self._weekday = weekday
        self._time_of_day = time_of_day
        self._startup_delay = startup_delay
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._warned = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="WeeklyReportScheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        logger.info("WeeklyReportScheduler started (check every %.0fs)", self._poll_interval)
        if self._stop_event.wait(self._startup_delay):
            return
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:
                if not self._warned:
                    logger.warning("Weekly report check failed (%s); will keep retrying", exc)
                    self._warned = True
            if self._stop_event.wait(self._poll_interval):
                return

    def is_due(self, now: Optional[datetime] = None) -> bool:
        now = now or self._clock()
        if not self._enabled() or now.weekday() != self._weekday():
            return False
        due = datetime.combine(now.date(), self._time_of_day(), tzinfo=now.tzinfo)
        if now < due:
            return False
        last = _read_last_run(self._state_path)
        return last is None or last < due

    def check_once(self) -> Optional[ContextEvent]:
        """Publish this week's report if it is due; the event, or None
        (not due, or the week was empty)."""
        now = self._clock()
        if not self.is_due(now):
            return None
        payload = self._gather(now)
        if payload.get("empty"):
            _write_last_run(self._state_path, now, sent=False)
            return None
        event = make_event(payload, trigger="schedule")
        # Push first: if the bus isn't ready the report is retried, not lost.
        self._bus.push_threadsafe(event)
        _write_last_run(self._state_path, now, sent=True)
        return event

    def run_now(self) -> tuple[dict[str, Any], Optional[ContextEvent]]:
        """"Run now": gather, and publish unless the week is empty. Doesn't
        count as this week's run. Returns ``(payload, event or None)``."""
        payload = self._gather(self._clock())
        if payload.get("empty"):
            return payload, None
        event = make_event(payload, trigger="manual")
        self._bus.push_threadsafe(event)
        return payload, event
