"""One-time setup scan and starter catalog idea sources (todo #33, #35).

Runs once per install. It collects a snapshot of the Mac:

* installed apps (``.app`` names in the Applications folders),
* the most repeated long commands in ``~/.zsh_history`` (skipped when the
  provider is cloud: runner decision #33, invariant 7c),
* Downloads clutter (file counts by extension, oldest file age),
* recurring calendar events, when a calendar reader is injected.

Two kinds of candidate ideas come out of it:

1. ``source="starter_catalog"``: curated entries in ``starter_catalog.json``
   whose ``requires`` block matches the snapshot. No model writes them.
2. ``source="setup_scan"``: a repeated shell command (or a recurring
   calendar event) is handed to the generator, which writes a proposal.

Every candidate passes the open-help filter before it is kept:
:func:`~ai.open_help.passes_checks`, :func:`~ai.open_help.grounded_in_events`
against the signal text, :class:`~ai.open_help.IdeaFeedback` rejection
memory, and the injected judge (score >= ``min_score``). At most
``max_ideas`` survive. They are published as ``pattern_detected`` one at
a time: the first ``initial_delay_seconds`` after the scan, the rest
``stagger_seconds`` apart (default 4 h), so a new install shows one idea
per sitting instead of a burst. The queue lives in the marker file, so a
daemon restart resumes it instead of rescanning.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

from ai.idea_sources import Evaluate, Publish, _PROPOSAL_FORMAT, parse_suggestion
from ai.open_help import IdeaFeedback, grounded_in_events, parse_judge, passes_checks
from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

CalendarReader = Callable[[], Union[Awaitable[list[Any]], list[Any]]]

DEFAULT_MARKER_PATH = Path.home() / ".bot-inator" / "setup_scan.json"
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "starter_catalog.json"
DEFAULT_APP_DIRS = (
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
)

SETUP_SCAN_SYSTEM_PROMPT = (
    "You are a proactive macOS assistant. A one-time scan of the user's "
    "Mac found one signal of repeated manual work: a shell command the "
    "user typed many times, or a calendar event that recurs. Propose one "
    "small reusable tool that does that work in one click. Take the parts "
    "that vary (paths, names, flags) as inputs with the observed values "
    "as defaults. The tool must never delete or send anything without a "
    "dry_run option that defaults to True.\n\n"
    "If one click would not save the user real effort (the command is "
    "already short, or it is an interactive session), do not propose.\n\n"
    + _PROPOSAL_FORMAT
)

SETUP_JUDGE_SYSTEM_PROMPT = (
    "You review a proposed helper tool for a macOS user before it is "
    "shown to them. The evidence is a one-time snapshot of their Mac, not "
    "live activity: installed apps, shell commands with how often they "
    "were typed, Downloads folder counts, recurring calendar events. Most "
    "proposals should be rejected.\n\n"
    "First name the manual step the tool removes. Then check the snapshot "
    "for a count that shows the user does that step repeatedly.\n\n"
    "Score the idea from 0 to 10:\n"
    "- 9-10: a count shows the user doing this step by hand many times "
    "(the same command typed 20+ times, 10+ installers or archives left "
    "in Downloads) and the tool removes it;\n"
    "- 7-8: a count shows the step recurs (several files of the kind the "
    "tool handles, a command typed 5+ times, a recurring event) and an "
    "app the tool needs is installed;\n"
    "- 4-6: only installed apps, no count of the step;\n"
    "- 0-3: generic, unrelated to the snapshot, already one click in "
    "macOS, or not doable with the Python standard library and macOS "
    "commands.\n"
    "Ideas similar to ones the user rejected score low. When in doubt, "
    "score lower.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"score": <integer 0-10>, "reason": "<one sentence naming the step '
    'and the count that shows it, or what is missing>"}'
)

# ---- signals ----------------------------------------------------------

_SECRET_RE = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|bearer|authorization|"
    r"aws_access|private[_-]?key",
    re.IGNORECASE,
)
_HIST_PREFIX_RE = re.compile(r"^: \d+:\d+;")


@dataclass
class SetupSignals:
    """What the scan found. Every field is safe to leave empty."""

    apps: set[str] = field(default_factory=set)  # lowercase .app stems
    app_names: dict[str, str] = field(default_factory=dict)  # lower -> display
    shell_commands: list[tuple[str, int]] = field(default_factory=list)
    downloads_by_ext: dict[str, int] = field(default_factory=dict)
    downloads_total: int = 0
    downloads_oldest_days: int = 0
    calendar_recurring: list[tuple[str, int]] = field(default_factory=list)
    history_skipped: bool = False

    def has_app(self, name: str) -> bool:
        return name.lower() in self.apps


def collect_apps(dirs: Iterable[Path] = DEFAULT_APP_DIRS) -> dict[str, str]:
    """``{lowercase stem: display stem}`` of every ``*.app`` in ``dirs`` (one level)."""
    found: dict[str, str] = {}
    for d in dirs:
        try:
            entries = list(Path(d).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix == ".app":
                found.setdefault(entry.stem.lower(), entry.stem)
    return found


def _unmetafy(data: bytes) -> bytes:
    """Undo zsh's history metafication (0x83 marks the next byte XOR 32)."""
    if b"\x83" not in data:
        return data
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == 0x83:
            nxt = next(it, None)
            if nxt is not None:
                out.append(nxt ^ 32)
        else:
            out.append(b)
    return bytes(out)


def parse_zsh_history(data: Union[bytes, str]) -> list[str]:
    """Commands from a zsh history file, plain or extended (``: <ts>:0;cmd``).

    Lines ending in a backslash continue the command on the next line.
    """
    if isinstance(data, bytes):
        data = _unmetafy(data).decode("utf-8", errors="replace")
    commands: list[str] = []
    pending: list[str] = []
    for line in data.splitlines():
        if not pending:
            line = _HIST_PREFIX_RE.sub("", line, count=1)
        if line.endswith("\\"):
            pending.append(line[:-1])
            continue
        pending.append(line)
        cmd = "\n".join(pending).strip()
        pending = []
        if cmd:
            commands.append(cmd)
    if pending:
        cmd = "\n".join(pending).strip()
        if cmd:
            commands.append(cmd)
    return commands


def top_repeated_commands(
    commands: Iterable[str], *, min_length: int = 20, min_repeats: int = 5, limit: int = 5,
) -> list[tuple[str, int]]:
    """Most repeated long commands, most frequent first.

    Whitespace is collapsed. Commands that look like they carry a secret
    are dropped: they must not reach a prompt or a generated tool.
    """
    counts: Counter[str] = Counter()
    for cmd in commands:
        norm = " ".join(cmd.split())
        if len(norm) < min_length or _SECRET_RE.search(norm):
            continue
        counts[norm] += 1
    return [(c, n) for c, n in counts.most_common() if n >= min_repeats][:limit]


def scan_downloads(folder: Path, now: Optional[float] = None) -> tuple[dict[str, int], int, int]:
    """``(counts by lowercase extension, total files, oldest age in days)``.

    Top level only; hidden files and folders are ignored. Only names'
    extensions are kept, never the names themselves.
    """
    now = time.time() if now is None else now
    counts: Counter[str] = Counter()
    oldest = now
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return {}, 0, 0
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        counts[entry.suffix.lower()] += 1
        oldest = min(oldest, mtime)
    total = sum(counts.values())
    age_days = int((now - oldest) // 86400) if total else 0
    return dict(counts), total, age_days


def recurring_events(events: Iterable[Any], min_count: int = 3) -> list[tuple[str, int]]:
    """Titles that recur: flagged recurring, or seen ``min_count``+ times."""
    counts: Counter[str] = Counter()
    flagged: set[str] = set()
    for ev in events or []:
        if isinstance(ev, str):
            title = ev
        elif isinstance(ev, dict):
            title = str(ev.get("title") or ev.get("summary") or "")
            if ev.get("recurring") or ev.get("is_recurring"):
                flagged.add(title.strip())
        else:
            continue
        title = title.strip()
        if title:
            counts[title] += 1
    out = [(t, n) for t, n in counts.most_common() if n >= min_count or t in flagged]
    return out[:5]


# ---- catalog ----------------------------------------------------------


def load_catalog(path: Optional[Path] = None) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path or DEFAULT_CATALOG_PATH).read_text())
    except (OSError, ValueError):
        logger.exception("starter catalog unreadable")
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _downloads_count(signals: SetupSignals, exts: Iterable[str]) -> int:
    return sum(signals.downloads_by_ext.get(e.lower(), 0) for e in exts)


def _shell_count(signals: SetupSignals, prefixes: Iterable[str]) -> int:
    prefixes = tuple(prefixes)
    return sum(n for cmd, n in signals.shell_commands if cmd.startswith(prefixes))


def match_catalog(entry: dict[str, Any], signals: SetupSignals) -> Optional[str]:
    """Signal text for ``entry`` if every requirement holds, else ``None``."""
    req = entry.get("requires") or {}
    if not req:
        return None
    lines: list[str] = []
    apps_all = req.get("apps_all") or []
    if apps_all:
        if not all(signals.has_app(a) for a in apps_all):
            return None
        lines.append("installed apps: " + ", ".join(apps_all))
    apps_any = req.get("apps_any") or []
    if apps_any:
        have = [a for a in apps_any if signals.has_app(a)]
        if not have:
            return None
        lines.append("installed apps: " + ", ".join(have))
    dl = req.get("downloads")
    if dl:
        exts = dl.get("exts") or []
        n = _downloads_count(signals, exts)
        if n < int(dl.get("min", 1)):
            return None
        detail = ", ".join(
            f"{signals.downloads_by_ext[e]} {e}" for e in exts if signals.downloads_by_ext.get(e)
        )
        lines.append(f"Downloads folder holds {n} {'/'.join(exts)} files ({detail})")
    if "downloads_total" in req:
        if signals.downloads_total < int(req["downloads_total"]):
            return None
        lines.append(f"Downloads folder holds {signals.downloads_total} files in total")
    if "downloads_oldest_days" in req:
        if signals.downloads_oldest_days < int(req["downloads_oldest_days"]):
            return None
        lines.append(f"oldest file in Downloads is {signals.downloads_oldest_days} days old")
    shell_any = req.get("shell_any") or []
    if shell_any:
        n = _shell_count(signals, shell_any)
        if n < int(req.get("shell_min", 5)):
            return None
        for cmd, count in signals.shell_commands:
            if cmd.startswith(tuple(shell_any)):
                lines.append(f"shell command typed {count} times: {cmd[:200]}")
    if "calendar_recurring" in req:
        if len(signals.calendar_recurring) < int(req["calendar_recurring"]):
            return None
        for title, count in signals.calendar_recurring:
            lines.append(f"recurring calendar event ({count}x): {title[:100]}")
    return _signal_text(lines)


def _signal_text(lines: list[str]) -> str:
    return "Setup scan of the user's Mac (one-time snapshot):\n" + "\n".join(
        f"- {line}" for line in lines
    )


# ---- the source -------------------------------------------------------


class SetupScanner:
    """Run the one-time scan, filter ideas, publish them staggered."""

    source_name = "setup_scan"
    catalog_source_name = "starter_catalog"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        judge: Evaluate,
        cloud: bool,
        feedback: Optional[IdeaFeedback] = None,
        marker_path: Path = DEFAULT_MARKER_PATH,
        catalog_path: Optional[Path] = None,
        app_dirs: Iterable[Path] = DEFAULT_APP_DIRS,
        history_path: Optional[Path] = None,
        downloads_dir: Optional[Path] = None,
        calendar_reader: Optional[CalendarReader] = None,
        min_score: int = 7,
        max_ideas: int = 3,
        max_generated: int = 2,
        max_judged: int = 8,
        initial_delay_seconds: float = 120.0,
        stagger_seconds: float = 4 * 3600.0,
        publish: Optional[Publish] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self._judge = judge
        self.cloud = cloud
        self.feedback = feedback or IdeaFeedback()
        self.marker_path = Path(marker_path)
        self.catalog_path = catalog_path
        self.app_dirs = list(app_dirs)
        self.history_path = Path(
            history_path or os.environ.get("HISTFILE") or Path.home() / ".zsh_history"
        )
        self.downloads_dir = Path(downloads_dir or Path.home() / "Downloads")
        self.calendar_reader = calendar_reader
        self.min_score = min_score
        self.max_ideas = max_ideas
        self.max_generated = max_generated
        self.max_judged = max_judged
        self.initial_delay_seconds = initial_delay_seconds
        self.stagger_seconds = stagger_seconds
        self._publish = publish or self.event_bus.push
        self._sleep = sleep
        self._clock = clock
        self._task: Optional[asyncio.Task[None]] = None
        self.verdicts: list[dict[str, Any]] = []  # one per judged/dropped candidate

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="setup-scan")
        return self._task

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def record_feedback(self, tool_name: str, description: str, approved: bool) -> None:
        """Orchestrator hook. Only wire it when no other source shares ``feedback``."""
        self.feedback.record(tool_name, description, approved)

    async def run(self) -> None:
        """Scan once (unless the marker says done), then drain the publish queue."""
        try:
            state = self._load_marker()
            if state is None:
                ideas = await self.scan_and_filter()
                state = {
                    "version": 1,
                    "scanned_at": self._clock(),
                    "cloud": self.cloud,
                    "pending": ideas,
                    "published": [],
                    "next_at": self._clock() + self.initial_delay_seconds,
                }
                self._save_marker(state)
                logger.info("setup scan done: %d idea(s) queued", len(ideas))
            await self._drain(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("setup scan failed")

    # ---- scan + filter ------------------------------------------------

    async def collect(self) -> SetupSignals:
        signals = SetupSignals()
        signals.app_names = collect_apps(self.app_dirs)
        signals.apps = set(signals.app_names)
        if self.cloud:
            signals.history_skipped = True
            logger.info("setup scan: shell history skipped (cloud provider)")
        else:
            try:
                raw = self.history_path.read_bytes()
            except OSError:
                raw = b""
            signals.shell_commands = top_repeated_commands(parse_zsh_history(raw))
        signals.downloads_by_ext, signals.downloads_total, signals.downloads_oldest_days = (
            scan_downloads(self.downloads_dir, self._clock())
        )
        # Event titles are personal text like shell history: local only.
        if self.calendar_reader is not None and not self.cloud:
            try:
                result = self.calendar_reader()
                if inspect.isawaitable(result):
                    result = await result
                signals.calendar_recurring = recurring_events(result or [])
            except Exception:
                logger.exception("setup scan: calendar reader failed")
        return signals

    async def _generated_candidates(self, signals: SetupSignals) -> list[dict[str, Any]]:
        prompts: list[tuple[str, str]] = []
        for cmd, n in signals.shell_commands:
            prompts.append((
                "shell_history",
                _signal_text([f"shell command typed {n} times: {cmd[:300]}"]),
            ))
        for title, n in signals.calendar_recurring[:1]:
            prompts.append((
                "calendar",
                _signal_text([f"recurring calendar event ({n}x): {title[:100]}"]),
            ))
        out = []
        for trigger, text in prompts[: self.max_generated]:
            try:
                idea = parse_suggestion(await self._evaluate(text))
            except Exception:
                logger.exception("setup scan evaluate failed")
                continue
            if idea is None:
                continue
            idea.update(source=self.source_name, trigger=trigger, evidence=text)
            out.append(idea)
        return out

    def _catalog_candidates(self, signals: SetupSignals) -> list[dict[str, Any]]:
        out = []
        for entry in load_catalog(self.catalog_path):
            text = match_catalog(entry, signals)
            if text is None:
                continue
            out.append({
                "tool_name": entry.get("tool_name"),
                "description": entry.get("description"),
                "expected_logic": entry.get("expected_logic"),
                "source": self.catalog_source_name,
                "trigger": "setup_scan",
                "catalog_id": entry.get("id"),
                "evidence": text,
            })
        return out

    async def scan_and_filter(self) -> list[dict[str, Any]]:
        """Collect signals, build candidates, keep the best ``max_ideas`` that pass."""
        signals = await self.collect()
        candidates = await self._generated_candidates(signals)
        candidates += self._catalog_candidates(signals)
        kept: list[dict[str, Any]] = []
        seen: set[str] = set()
        judged = 0
        for idea in candidates:
            name = str(idea.get("tool_name") or "")
            if name in seen:
                continue
            seen.add(name)
            if judged >= self.max_judged:
                break
            stage = self._precheck(idea)
            if stage is None:
                judged += 1
                stage = await self._judge_idea(idea)
            self.verdicts.append({
                "tool_name": name, "source": idea.get("source"), "stage": stage,
                "score": idea.get("judge_score"), "reason": idea.get("judge_reason", ""),
            })
            if stage == "passed":
                kept.append(idea)
        kept.sort(key=lambda i: -int(i.get("judge_score") or 0))  # stable
        return kept[: self.max_ideas]

    def _precheck(self, idea: dict[str, Any]) -> Optional[str]:
        name = str(idea.get("tool_name") or "")
        if not passes_checks(idea):
            logger.info("setup scan dropped malformed idea %r", name)
            return "checks"
        if not grounded_in_events(idea, idea.get("evidence", "")):
            logger.info("setup scan dropped %r: names nothing in the snapshot", name)
            return "grounding"
        if self.feedback.resembles_rejected(name, str(idea.get("description") or "")):
            logger.info("setup scan dropped %r: resembles a rejected idea", name)
            return "feedback"
        return None

    async def _judge_idea(self, idea: dict[str, Any]) -> str:
        name = str(idea.get("tool_name") or "")
        history = self.feedback.summary()
        prompt = (
            f"{idea.get('evidence', '')}\n\nProposed tool: {name}\n"
            f"Description: {idea.get('description')}\n"
            f"Expected logic: {str(idea.get('expected_logic'))[:800]}"
            + (f"\n\nThe user's past verdicts:\n{history}" if history else "")
        )
        try:
            score, reason = parse_judge(await self._judge(prompt))
        except Exception:
            logger.exception("setup scan judge failed")
            return "judge"
        idea["judge_score"], idea["judge_reason"] = score, reason
        if score is None or score < self.min_score:
            logger.info("setup scan dropped %r: judge score %s (min %s): %s",
                        name, score, self.min_score, reason)
            return "judge"
        logger.info("setup scan passed %r: judge score %s: %s", name, score, reason)
        return "passed"

    # ---- staggered publishing -----------------------------------------

    async def _drain(self, state: dict[str, Any]) -> None:
        while state.get("pending"):
            wait = float(state.get("next_at") or 0) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            idea = state["pending"].pop(0)
            name = str(idea.get("tool_name") or "")
            # The user may have rejected something similar since the scan.
            if self.feedback.resembles_rejected(name, str(idea.get("description") or "")):
                logger.info("setup scan skipped queued %r: resembles a rejected idea", name)
                self._save_marker(state)
                continue
            try:
                await self._publish(ContextEvent(event_type="pattern_detected", metadata=dict(idea)))
            except Exception:
                logger.exception("setup scan failed to publish %r", name)
            state.setdefault("published", []).append(name)
            state["next_at"] = self._clock() + self.stagger_seconds
            self._save_marker(state)

    def _load_marker(self) -> Optional[dict[str, Any]]:
        if not self.marker_path.exists():
            return None
        try:
            data = json.loads(self.marker_path.read_text())
        except (OSError, ValueError):
            # A marker we can't read still means "scanned": never rescan.
            logger.warning("setup scan marker %s unreadable; not rescanning", self.marker_path)
            return {"pending": []}
        return data if isinstance(data, dict) else {"pending": []}

    def _save_marker(self, state: dict[str, Any]) -> None:
        try:
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.marker_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, default=str))
            tmp.replace(self.marker_path)
        except OSError:
            logger.exception("failed to save setup scan marker %s", self.marker_path)


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_MARKER_PATH",
    "SETUP_JUDGE_SYSTEM_PROMPT",
    "SETUP_SCAN_SYSTEM_PROMPT",
    "SetupScanner",
    "SetupSignals",
    "collect_apps",
    "load_catalog",
    "match_catalog",
    "parse_zsh_history",
    "recurring_events",
    "scan_downloads",
    "top_repeated_commands",
]
