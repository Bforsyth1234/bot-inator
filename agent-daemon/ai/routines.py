"""Deterministic routine mining over the activity history (#75).

No model call. Two kinds of candidate:

* ``time_routine``: the same app(s) launched in the same weekday-hour slot
  on >= ``min_weeks`` separate weeks (and on at least ``min_ratio`` of the
  observed days of that weekday). Same apps + hour on several weekdays are
  merged into one candidate. Maps to ``open_app`` (or ``run_shortcut``
  when a known Shortcut's name names the app).
* ``trigger_action``: trigger A followed within ``window_minutes`` by
  action B on >= ``min_days`` separate days, and after at least
  ``min_ratio`` of A's occurrences. Triggers: a file landing in a folder,
  mail arriving (person or bulk), a meeting starting soon. Actions: a file
  moved out of a folder to another one (``move_file``), an app launch
  (``open_app``). Anything else maps to ``generate_custom_tool``.

#66: only *launches* (a new process) count, never switches between apps
that are already running, and every candidate ends in an action that
saves a step. So app-switch-only history yields nothing.

Candidates are plain dicts; :func:`idea_summary` turns one into text that
is safe for a cloud prompt (app names, folder, extension, counts only).

CLI: ``python -m ai.routines --report [--db PATH] [--json]``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ai.activity_store import ActivityStore, default_store

MIN_WEEKS = 3
MIN_DAYS = 5
MIN_RATIO = 0.5
WINDOW_MINUTES = 10

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _cid(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _week(date: str) -> tuple[int, int]:
    iso = datetime.strptime(date, "%Y-%m-%d").isocalendar()
    return (iso[0], iso[1])


def _expand(folder: str) -> str:
    return str(Path(folder).expanduser()) if folder.startswith("~") else folder


# -- time routines -----------------------------------------------------------


def _time_routines(rows: list[dict[str, Any]], min_weeks: int,
                   min_ratio: float) -> list[dict[str, Any]]:
    # Days the store was recording, per weekday (the "of N Mondays").
    observed: dict[int, set[str]] = defaultdict(set)
    # (weekday, hour, app) -> {date: last ts}
    seen: dict[tuple[int, int, str], dict[str, float]] = defaultdict(dict)
    for r in rows:
        observed[r["weekday"]].add(r["date"])
        if r["event_type"] == "app_activated" and r["launched"] == 1 and r["app"]:
            seen[(r["weekday"], r["hour"], r["app"])][r["date"]] = r["ts"]

    # Qualifying apps per slot.
    slots: dict[tuple[int, int], dict[str, dict[str, float]]] = defaultdict(dict)
    for (wd, hour, app), dates in seen.items():
        weeks = {_week(d) for d in dates}
        if len(weeks) < min_weeks:
            continue
        if len(dates) < min_ratio * len(observed[wd]):
            continue
        slots[(wd, hour)][app] = dates

    # One entry per slot: all qualifying apps together if they co-occur on
    # enough weeks, else one per app.
    entries: list[tuple[int, int, tuple[str, ...], dict[str, float]]] = []
    for (wd, hour), apps in slots.items():
        names = sorted(apps)
        common = set.intersection(*(set(apps[a]) for a in names))
        if len(names) > 1 and len({_week(d) for d in common}) >= min_weeks:
            dates = {d: max(apps[a][d] for a in names) for d in common}
            entries.append((wd, hour, tuple(names), dates))
        else:
            for a in names:
                entries.append((wd, hour, (a,), apps[a]))

    # Merge the same apps + hour across weekdays.
    merged: dict[tuple[tuple[str, ...], int], list[tuple[int, dict[str, float]]]] = defaultdict(list)
    for wd, hour, names, dates in entries:
        merged[(names, hour)].append((wd, dates))

    out = []
    for (names, hour), per_day in merged.items():
        per_day.sort()
        wds = [wd for wd, _ in per_day]
        days = sum(len(d) for _, d in per_day)
        of = sum(len(observed[wd]) for wd in wds)
        last = max(max(d.values()) for _, d in per_day)
        day_names = _join([WEEKDAYS[w] + "s" for w in wds])
        apps = list(names)
        out.append({
            "id": _cid("time_routine", apps, hour, wds),
            "kind": "time_routine",
            "title": f"Open {_join(apps)} on {day_names} around {hour:02d}:00",
            "trigger": {"type": "time", "weekdays": [WEEKDAYS[w] for w in wds],
                        "hour": hour},
            "action": {"type": "app_launch", "apps": apps},
            "evidence": {
                "count": days,
                "days": days,
                "of": of,
                "weeks": len({_week(d) for _, ds in per_day for d in ds}),
                "last_seen": _iso(last),
            },
            "evidence_text": f"seen on {days} of the last {of} {day_names}",
        })
    return out


# -- trigger -> action pairs -------------------------------------------------


def _trigger_key(r: dict[str, Any]) -> Optional[tuple]:
    et = r["event_type"]
    if et == "file":
        arrival = r["action"] == "created" or (
            r["action"] == "renamed" and r["from_folder"] in (None, r["folder"]))
        if arrival and r["folder"]:
            return ("file_arrival", r["ext"] or "", r["folder"])
    elif et == "mail_received" and r["bulk"] is not None:
        return ("mail", "bulk" if r["bulk"] else "person")
    elif et == "event_starting_soon":
        return ("meeting_soon",)
    return None


def _action_key(r: dict[str, Any]) -> Optional[tuple]:
    et = r["event_type"]
    if et == "file" and r["action"] == "renamed":
        if r["from_folder"] and r["folder"] and r["from_folder"] != r["folder"]:
            return ("file_move", r["ext"] or "", r["from_folder"], r["folder"])
    elif et == "app_activated" and r["launched"] == 1 and r["app"]:
        return ("app_launch", r["app"])
    return None


def _pair_fits(t: tuple, a: tuple) -> bool:
    if a[0] == "file_move":
        # A move only answers the arrival of the same kind of file in the
        # folder it is moved out of.
        return t[0] == "file_arrival" and t[1] == a[1] and t[2] == a[2]
    if a[0] == "app_launch":
        return t[0] in ("mail", "meeting_soon", "file_arrival")
    return False


def _describe_trigger(t: tuple) -> str:
    if t[0] == "file_arrival":
        kind = f"a {t[1]} file" if t[1] else "a file"
        return f"{kind} landed in {t[2]}"
    if t[0] == "mail":
        return "a bulk email arrived" if t[1] == "bulk" else "an email from a person arrived"
    return "a meeting was about to start"


def _describe_action(a: tuple) -> str:
    if a[0] == "file_move":
        return f"moved it to {a[3]}"
    return f"opened {a[1]}"


def _trigger_dict(t: tuple) -> dict[str, Any]:
    if t[0] == "file_arrival":
        return {"type": "file_arrival", "ext": t[1], "folder": t[2]}
    if t[0] == "mail":
        return {"type": "mail", "sender": t[1]}
    return {"type": "meeting_soon"}


def _action_dict(a: tuple) -> dict[str, Any]:
    if a[0] == "file_move":
        return {"type": "file_move", "ext": a[1], "from_folder": a[2], "to_folder": a[3]}
    return {"type": "app_launch", "apps": [a[1]]}


def _trigger_actions(rows: list[dict[str, Any]], min_days: int, min_ratio: float,
                     window_minutes: int) -> list[dict[str, Any]]:
    window = window_minutes * 60
    triggers: list[tuple[float, tuple]] = []
    trig_total: dict[tuple, int] = defaultdict(int)
    trig_days: dict[tuple, set[str]] = defaultdict(set)
    # (t, a) -> trigger occurrence indexes answered, dates, last ts
    answered: dict[tuple, set[int]] = defaultdict(set)
    dates: dict[tuple, set[str]] = defaultdict(set)
    last: dict[tuple, float] = {}

    start = 0
    for r in rows:
        tk = _trigger_key(r)
        ak = _action_key(r)
        if ak is not None:
            while start < len(triggers) and triggers[start][0] < r["ts"] - window:
                start += 1
            for idx in range(start, len(triggers)):
                tts, tkey = triggers[idx]
                if tts > r["ts"]:
                    break
                if not _pair_fits(tkey, ak):
                    continue
                pair = (tkey, ak)
                answered[pair].add(idx)
                dates[pair].add(r["date"])
                last[pair] = r["ts"]
        if tk is not None:
            triggers.append((r["ts"], tk))
            trig_total[tk] += 1
            trig_days[tk].add(r["date"])

    out = []
    for pair, ds in dates.items():
        tkey, akey = pair
        n = len(answered[pair])
        total = trig_total[tkey]
        if len(ds) < min_days or n < min_ratio * total:
            continue
        out.append({
            "id": _cid("trigger_action", list(tkey), list(akey)),
            "kind": "trigger_action",
            "title": f"When {_describe_trigger(tkey)}, you {_describe_action(akey)}",
            "trigger": _trigger_dict(tkey),
            "action": _action_dict(akey),
            "evidence": {
                "count": n,
                "days": len(ds),
                "of": total,
                "trigger_days": len(trig_days[tkey]),
                "last_seen": _iso(last[pair]),
            },
            "evidence_text": (
                f"seen on {len(ds)} separate days "
                f"({n} of {total} times {_describe_trigger(tkey)})"
            ),
        })
    return out


# -- tool mapping ------------------------------------------------------------


def map_to_tool(candidate: dict[str, Any],
                shortcuts: Iterable[str] = ()) -> dict[str, Any]:
    """Pick the existing tool that does the candidate's action.

    Returns ``{"tool": name, "args": {...}}``. ``move_file``'s ``src`` is the
    file that arrives, known only when the trigger fires, so it is absent.
    """
    action = candidate.get("action") or {}
    if action.get("type") == "app_launch":
        apps = list(action.get("apps") or [])
        lowered = {a.lower() for a in apps}
        for name in shortcuts:
            if any(a in name.lower() for a in lowered):
                return {"tool": "run_shortcut", "args": {"name": name}}
        if apps:
            # open_app takes one name; several apps = one call each.
            calls = [{"name": a} for a in apps]
            return {"tool": "open_app", "args": calls[0], "calls": calls}
    if action.get("type") == "file_move" and action.get("to_folder"):
        return {"tool": "move_file", "args": {"dst_dir": _expand(action["to_folder"])}}
    return {
        "tool": "generate_custom_tool",
        "args": {"description": candidate.get("title", "")},
    }


def idea_summary(candidate: dict[str, Any]) -> str:
    """Cloud-safe wording input: app names, folder, extension, counts only."""
    ev = candidate.get("evidence") or {}
    trig = candidate.get("trigger") or {}
    act = candidate.get("action") or {}
    parts: list[str] = []
    if candidate.get("kind") == "time_routine":
        days = [str(d) for d in trig.get("weekdays", []) if d in WEEKDAYS]
        hour = int(trig.get("hour", 0))
        apps = [str(a) for a in act.get("apps", [])]
        parts.append(f"Routine: opens {_join(apps)} on {_join([d + 's' for d in days])} "
                     f"around {hour:02d}:00.")
    else:
        t = trig.get("type")
        if t == "file_arrival":
            ext = str(trig.get("ext") or "")
            parts.append(f"Trigger: a {ext or 'new'} file lands in {trig.get('folder', '')}.")
        elif t == "mail":
            parts.append("Trigger: an email arrives from a "
                         f"{'mailing list' if trig.get('sender') == 'bulk' else 'person'}.")
        elif t == "meeting_soon":
            parts.append("Trigger: a meeting is about to start.")
        if act.get("type") == "file_move":
            parts.append(f"Then: the user moves it to {act.get('to_folder', '')}.")
        elif act.get("type") == "app_launch":
            parts.append(f"Then: the user opens {_join([str(a) for a in act.get('apps', [])])}.")
    parts.append(
        f"Evidence: {int(ev.get('days', 0))} days, {int(ev.get('count', 0))} times "
        f"out of {int(ev.get('of', 0))}."
    )
    tool = candidate.get("tool")
    if tool:
        parts.append(f"Suggested tool: {tool}.")
    return " ".join(parts)


# -- entry point -------------------------------------------------------------


def find_candidates(
    store: Optional[ActivityStore] = None,
    *,
    now: Optional[datetime] = None,
    min_weeks: int = MIN_WEEKS,
    min_days: int = MIN_DAYS,
    min_ratio: float = MIN_RATIO,
    window_minutes: int = WINDOW_MINUTES,
    shortcuts: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Mine the history and return candidates as plain dicts.

    Each dict: ``id``, ``kind``, ``title``, ``trigger``, ``action``,
    ``evidence`` (``count``, ``days``, ``of``, ``last_seen``),
    ``evidence_text``, ``tool``, ``tool_args``, ``summary``.
    Strongest evidence first.
    """
    store = store or default_store()
    ref = now or datetime.now(timezone.utc)
    rows = store.rows(since=ref - timedelta(days=store.retention_days))
    shortcuts = list(shortcuts)
    cands = _time_routines(rows, min_weeks, min_ratio)
    cands += _trigger_actions(rows, min_days, min_ratio, window_minutes)
    for c in cands:
        mapped = map_to_tool(c, shortcuts)
        c["tool"] = mapped["tool"]
        c["tool_args"] = mapped["args"]
        c["tool_calls"] = mapped.get("calls", [mapped["args"]])
        c["summary"] = idea_summary(c)
    cands.sort(key=lambda c: (-c["evidence"]["days"], c["id"]))
    return cands


def report(cands: list[dict[str, Any]], out=None) -> None:
    out = out or sys.stdout
    if not cands:
        print("No routines found.", file=out)
        return
    for c in cands:
        print(f"- {c['title']}", file=out)
        print(f"    {c['evidence_text']}; last seen {c['evidence']['last_seen']}", file=out)
        print(f"    tool: {c['tool']} {json.dumps(c['tool_args'])}", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai.routines",
                                     description="Mine the activity history for routines.")
    parser.add_argument("--report", action="store_true", help="print candidates")
    parser.add_argument("--db", help="activity db path (default ~/.bot-inator/activity.db)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if not args.report:
        parser.print_help()
        return 2
    store = ActivityStore(args.db) if args.db else default_store()
    print(f"{store.count()} events in {store.db_path}", file=sys.stderr)
    cands = find_candidates(store)
    if args.json:
        print(json.dumps(cands, indent=2))
    else:
        report(cands)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
