"""Idea source: friction between two apps (todo #34, Phase 5.2 row 4).

Two signals, both built from ``app_activated`` and ``clipboard_changed``
events (the clipboard watcher reports which app and the content *kind*,
never the content):

* **copy -> switch** (strong): a copy in app A followed within
  ``copy_to_switch_seconds`` by a switch to app B counts one A -> B
  transfer. When one A -> B pair repeats ``min_transfers`` times inside
  ``window_seconds`` the user is moving data by hand, and a tool could
  move it directly (Mail -> Numbers x5 -> "pull invoice totals into my
  sheet").
* **ping-pong** (weak): ``pingpong_min_switches`` alternating switches
  between the same two apps inside ``pingpong_window_seconds``, without
  the copies. It needs a higher judge score (``pingpong_min_score``).

A copy is attributed to the app of the most recent ``app_activated`` the
detector saw (the watcher's own ``app`` field is used only before any
activation arrived), so the copy -> switch order comes from one stream.

A detected pair is filtered like open-help: :func:`passes_checks` (which
drops a tool that only switches or toggles between the two apps, todo
#66), a grounding check (the idea must name one of the two apps), the
:class:`IdeaFeedback` rejected-idea check, then a judge score. Ideas that
pass publish ``pattern_detected`` with ``source="friction"``. Each app
pair (unordered) has its own cooldown, started when the pair is detected
so a judge rejection doesn't re-ask on the next copy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Callable, Optional

from ai.idea_sources import _PROPOSAL_FORMAT, Evaluate, Publish, parse_suggestion
from ai.open_help import IdeaFeedback, is_app_switcher, parse_judge, passes_checks
from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

FRICTION_SYSTEM_PROMPT = (
    "You are a proactive macOS assistant. The user keeps moving data "
    "between two apps by hand: they copy something in one app, switch to "
    "the other, and (presumably) paste. You are told the two apps and how "
    "often it happened, never what was copied.\n\n"
    "Propose the single small tool that would move that data directly "
    "from the first app to the second, so the user stops copying and "
    "pasting (for example: Mail -> Numbers becomes 'pull invoice totals "
    "from selected Mail messages into the open Numbers sheet'). Use "
    "AppleScript via osascript, the shortcuts command or the apps' files "
    "where that works. The tool must take its inputs as parameters, and "
    "must never delete or send anything without a dry_run option that "
    "defaults to True. Name both apps in the description.\n\n"
    "The tool must move data. Do not propose a tool that only switches, "
    "toggles or focuses the two apps, or that lists or counts app "
    "switches: macOS already does that (Cmd-Tab, the Dock, Screen Time).\n\n"
    "If you cannot think of a tool that moves data between these two "
    "specific apps, do not propose one.\n\n"
    + _PROPOSAL_FORMAT
)

FRICTION_JUDGE_SYSTEM_PROMPT = (
    "You review a proposed helper tool for a macOS user before it is "
    "shown to them. The evidence is a count of how often the user copied "
    "something in one app and then switched to another (or switched back "
    "and forth between two apps). What was copied is not known.\n\n"
    "Score the idea from 0 to 10:\n"
    "- 9-10: the tool moves data from the first app to the second "
    "directly, both apps can be scripted for it (AppleScript, Shortcuts, "
    "files), and it removes the copy-and-paste step;\n"
    "- 7-8: plausible and specific to these two apps, but it guesses what "
    "the data is;\n"
    "- 4-6: generic (a clipboard manager, a notification), or only one of "
    "the apps is involved;\n"
    "- 0-3: unrelated to the two apps, already one click in macOS, or not "
    "doable with the Python standard library and macOS commands. A tool "
    "that only switches, toggles or focuses the two apps, or lists or "
    "counts app switches, always scores 0-3 (Cmd-Tab already does it).\n"
    "Switching back and forth without copying is weak evidence: score "
    "such ideas lower. Ideas similar to ones the user rejected score low. "
    "When in doubt, score lower.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"score": <integer 0-10>, "reason": "<one sentence>"}'
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _app_words(app: str) -> set[str]:
    return {w for w in _WORD_RE.findall(app.lower()) if len(w) >= 3}


def names_an_app(idea: dict[str, Any], apps: tuple[str, str]) -> bool:
    """True when the idea's text mentions a word of either app name."""
    text = " ".join(
        str(idea.get(k) or "") for k in ("tool_name", "description", "expected_logic")
    ).lower().replace("_", " ")
    words = set(_WORD_RE.findall(text))
    return any(_app_words(app) & words for app in apps)


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


class FrictionDetector:
    """Detect copy -> switch and ping-pong between two apps; publish filtered ideas."""

    source_name = "friction"
    # #79: ``ai.idea_outcomes.OutcomeStore`` set by main.py. When this
    # source's ideas are rarely kept it raises the judge bar and stretches
    # the cooldown (deterministic; None = the configured values).
    outcomes: Any = None

    def _adjusted(self, base_score: int, base_cooldown: float, kind: Optional[str] = None) -> tuple[int, float]:
        if self.outcomes is None:
            return base_score, base_cooldown
        try:
            return (
                self.outcomes.judge_threshold(self.source_name, base_score, kind),
                base_cooldown * self.outcomes.cooldown_multiplier(self.source_name, kind),
            )
        except Exception:
            logger.exception("%s outcome adjustment failed", self.source_name)
            return base_score, base_cooldown

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        judge: Evaluate,
        feedback: Optional[IdeaFeedback] = None,
        min_transfers: int = 5,
        window_seconds: float = 600.0,
        copy_to_switch_seconds: float = 60.0,
        pingpong_min_switches: int = 10,
        pingpong_window_seconds: float = 120.0,
        min_score: int = 7,
        pingpong_min_score: int = 8,
        cooldown_seconds: float = 24 * 3600.0,
        publish: Optional[Publish] = None,
        clock: Callable[[], float] = time.time,
        records_feedback: bool = True,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self._judge = judge
        self.feedback = feedback or IdeaFeedback()
        self.min_transfers = min_transfers
        self.window_seconds = window_seconds
        self.copy_to_switch_seconds = copy_to_switch_seconds
        self.pingpong_min_switches = pingpong_min_switches
        self.pingpong_window_seconds = pingpong_window_seconds
        self.min_score = min_score
        self.pingpong_min_score = pingpong_min_score
        self.cooldown_seconds = cooldown_seconds
        self._publish = publish or self.event_bus.push
        self._clock = clock
        # False when ``feedback`` is shared with another source that already
        # records each verdict (the orchestrator calls every source).
        self.records_feedback = records_feedback

        self._current_app: Optional[str] = None
        self._pending_copy: Optional[tuple[str, float]] = None  # (app, at)
        self._transfers: dict[tuple[str, str], deque[float]] = {}
        self._switches: deque[tuple[float, str]] = deque(maxlen=max(4, pingpong_min_switches * 2))
        self._cooldowns: dict[str, float] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self.last_verdict: dict[str, Any] = {}

    # ---- detect --------------------------------------------------------

    def observe(self, event: ContextEvent) -> Optional[asyncio.Task[None]]:
        if event.event_type == "clipboard_changed":
            app = self._current_app or event.app_name or (event.metadata or {}).get("app")
            if app:
                self._pending_copy = (str(app), self._clock())
            return None
        if event.event_type != "app_activated" or not event.app_name:
            return None
        app = str(event.app_name)
        if app == self._current_app:
            return None
        now = self._clock()
        self._current_app = app
        self._switches.append((now, app))
        task = self._check_transfer(app, now)
        if task is None:
            task = self._check_pingpong(now)
        return task

    def _check_transfer(self, dest: str, now: float) -> Optional[asyncio.Task[None]]:
        copy = self._pending_copy
        self._pending_copy = None
        if copy is None:
            return None
        src, at = copy
        if src == dest or now - at > self.copy_to_switch_seconds:
            return None
        times = self._transfers.setdefault((src, dest), deque())
        times.append(now)
        while times and now - times[0] > self.window_seconds:
            times.popleft()
        if len(times) < self.min_transfers:
            return None
        return self._detected("copy_paste", (src, dest), len(times), now)

    def _check_pingpong(self, now: float) -> Optional[asyncio.Task[None]]:
        recent = [(t, a) for t, a in self._switches if now - t <= self.pingpong_window_seconds]
        if len(recent) < 2:
            return None
        a, b = recent[-1][1], recent[-2][1]
        run = 0  # alternating activations between a and b, newest first
        for i, (_, app) in enumerate(reversed(recent)):
            if app != (a if i % 2 == 0 else b):
                break
            run += 1
        switches = run - 1
        if switches < self.pingpong_min_switches:
            return None
        return self._detected("ping_pong", (b, a), switches, now)

    def _detected(
        self, trigger: str, apps: tuple[str, str], count: int, now: float,
    ) -> Optional[asyncio.Task[None]]:
        key = _pair_key(*apps)
        if now < self._cooldowns.get(key, 0.0):
            return None
        if self._task is not None and not self._task.done():
            return None
        self._cooldowns[key] = now + self._adjusted(0, self.cooldown_seconds, trigger)[1]
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}
        self._transfers.pop(apps, None)
        self._switches.clear()
        logger.info("friction %s detected: %s -> %s x%d", trigger, apps[0], apps[1], count)
        self._task = asyncio.create_task(
            self._run(trigger, apps, count), name="friction"
        )
        return self._task

    def describe(self, trigger: str, apps: tuple[str, str], count: int) -> str:
        a, b = apps
        if trigger == "copy_paste":
            return (
                f"In the last {self.window_seconds / 60:.0f} minutes the user copied "
                f"something in {a} and then switched to {b}, {count} times. "
                f"What was copied is not known.\n"
                f"From app: {a}\nTo app: {b}\n"
                f"What tool would move this data from {a} to {b} directly?"
            )
        return (
            f"In the last {self.pingpong_window_seconds / 60:.0f} minutes the user "
            f"switched back and forth between {a} and {b} {count} times, without "
            f"copying. They may be reading in one app and typing into the other.\n"
            f"Apps: {a}, {b}\n"
            f"What tool would bring what they need from {a} into {b} directly?"
        )

    # ---- generate + filter ----------------------------------------------

    async def _run(self, trigger: str, apps: tuple[str, str], count: int) -> None:
        prompt = self.describe(trigger, apps, count)
        try:
            raw = await self._evaluate(prompt)
        except Exception:
            logger.exception("friction evaluate failed")
            return
        idea = parse_suggestion(raw)
        if idea is None:
            self.last_verdict = {"tool_name": None, "stage": "no_idea", "score": None, "reason": ""}
            return
        min_score = self.min_score if trigger == "copy_paste" else self.pingpong_min_score
        min_score, _ = self._adjusted(min_score, self.cooldown_seconds, trigger)
        if not await self._accept(idea, prompt, apps, min_score):
            return
        idea.update(
            source=self.source_name, trigger=trigger,
            apps=list(apps), count=count,
        )
        try:
            await self._publish(ContextEvent(event_type="pattern_detected", metadata=idea))
        except Exception:
            logger.exception("friction failed to publish suggestion")

    async def _accept(
        self, idea: dict[str, Any], prompt: str, apps: tuple[str, str], min_score: int,
    ) -> bool:
        name = str(idea.get("tool_name") or "")
        desc = str(idea.get("description") or "")
        self.last_verdict = {"tool_name": name, "stage": "checks", "score": None, "reason": ""}
        if is_app_switcher(idea, apps):
            self.last_verdict["stage"] = "app_switcher"
            logger.info("friction dropped %r: it only switches between %s and %s", name, *apps)
            return False
        if not passes_checks(idea, apps):
            logger.info("friction dropped malformed idea %r", name)
            return False
        if not names_an_app(idea, apps):
            self.last_verdict["stage"] = "grounding"
            logger.info("friction dropped %r: names neither %s nor %s", name, *apps)
            return False
        if self.feedback.resembles_rejected(name, desc):
            self.last_verdict["stage"] = "feedback"
            logger.info("friction dropped %r: resembles a rejected idea", name)
            return False
        history = self.feedback.summary()
        judge_prompt = (
            f"{prompt}\n\nProposed tool: {name}\nDescription: {desc}\n"
            f"Expected logic: {str(idea.get('expected_logic'))[:800]}"
            + (f"\n\nThe user's past verdicts:\n{history}" if history else "")
        )
        self.last_verdict["stage"] = "judge"
        try:
            score, reason = parse_judge(await self._judge(judge_prompt))
        except Exception:
            logger.exception("friction judge failed")
            return False
        self.last_verdict.update(score=score, reason=reason)
        if score is None or score < min_score:
            logger.info("friction dropped %r: judge score %s (min %s): %s",
                        name, score, min_score, reason)
            return False
        logger.info("friction passed %r: judge score %s (min %s): %s",
                    name, score, min_score, reason)
        idea["judge_score"] = score
        idea["judge_reason"] = reason
        self.last_verdict["stage"] = "passed"
        return True

    # ---- learn ---------------------------------------------------------

    def record_feedback(self, tool_name: str, description: str, approved: bool) -> None:
        """Called by the orchestrator when the user rules on a tool idea."""
        if self.records_feedback:
            self.feedback.record(tool_name, description, approved)


__all__ = [
    "FRICTION_JUDGE_SYSTEM_PROMPT",
    "FRICTION_SYSTEM_PROMPT",
    "FrictionDetector",
    "names_an_app",
]
