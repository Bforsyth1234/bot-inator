"""Open-ended help source: ask, filter, learn (LIVING_SPEC §1).

The other idea sources only fire on repetition or a fixed list of event
kinds. :class:`OpenHelpSuggester` asks the guiding question on every
window of activity: *"How could I possibly help the user here?"*

A small model asked that question over-proposes, so the answer never
reaches the user directly. Three stages:

1. **Generate**: :data:`OPEN_HELP_SYSTEM_PROMPT` turns recent events into
   at most one candidate tool idea.
2. **Filter**: deterministic checks, then a separate judge call
   (:data:`IDEA_JUDGE_SYSTEM_PROMPT`) scores the idea 0-10. Only ideas at
   or above ``min_score`` are published.
3. **Learn**: :class:`IdeaFeedback` records which ideas the user approved
   or rejected. Ideas close to a rejected one are dropped, and recent
   verdicts are shown to the judge so the bar fits this user.

Approved ideas publish the shared ``pattern_detected`` event with
``source="open_help"``, so the approve-idea -> draft -> review-code flow is
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from ai.idea_sources import (
    NO_SUGGESTION,
    Evaluate,
    IdeaState,
    Publish,
    _PROPOSAL_FORMAT,
    parse_suggestion,
)
from ai.privacy import redact_event
from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

GUIDING_QUESTION = "How could I possibly help the user here?"

OPEN_HELP_SYSTEM_PROMPT = (
    "You are a proactive macOS assistant watching recent activity on the "
    f"user's Mac. Ask yourself: {GUIDING_QUESTION}\n\n"
    "Think of the single most useful small tool that would save the user "
    "time or effort, given what they are doing right now. Good ideas "
    "remove a step the user is clearly about to do, or will do again. "
    "The tool must work for any future case of the same kind (take the "
    "path, text or number as an input), and must never delete or send "
    "anything without a dry_run option that defaults to True.\n\n"
    "Most activity needs no tool. If nothing would clearly help, say so.\n\n"
    + _PROPOSAL_FORMAT
)

IDEA_JUDGE_SYSTEM_PROMPT = (
    "You review a proposed helper tool for a macOS user before it is "
    "shown to them. Most proposals should be rejected: the user only "
    "wants to see an idea when the events prove they need it.\n\n"
    "First name the manual step the tool removes. Then check the events:\n"
    "- Do the events show the user doing that step (renaming, moving, "
    "converting, opening the same kind of thing), or clearly setting up "
    "to do it next?\n"
    "- Is it repeated or clearly intentional, or did files or apps merely "
    "appear?\n"
    "Files being created or downloaded only show that the files exist, not "
    "what the user does with them. A few files with similar names, "
    "especially test, sample, untitled or temporary files, are not "
    "evidence of a need: an idea built only from them scores at most 4. "
    "Switching between apps is not evidence of a need either.\n\n"
    "Score the idea from 0 to 10:\n"
    "- 9-10: the events show the user doing this step by hand, more than "
    "once or clearly on purpose, and the tool removes it;\n"
    "- 7-8: the events show the user about to do this step (for example "
    "they opened the app or folder the step needs right after the files "
    "appeared);\n"
    "- 4-6: plausible, but the events show only files or apps, not the "
    "step;\n"
    "- 0-3: generic, unrelated to the events, already one click in macOS, "
    "or not doable with the Python standard library and macOS commands.\n"
    "Ideas similar to ones the user rejected score low. Ideas similar to "
    "ones they approved may score higher. When in doubt, score lower.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"score": <integer 0-10>, "reason": "<one sentence naming the step '
    'and the event that shows it, or what is missing>"}'
)

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_MIN_LOGIC_CHARS = 60
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and the to of for in on with from by or is it its this that "
    "file files user tool".split()
)


def _tokens(*texts: str) -> set[str]:
    words: set[str] = set()
    for text in texts:
        words.update(_WORD_RE.findall((text or "").lower().replace("_", " ")))
    return words - _STOPWORDS


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class IdeaFeedback:
    """Approve/reject history for tool ideas, persisted as JSON.

    ``path=None`` keeps history in memory only (tests).
    """

    MAX_ENTRIES = 200
    REJECT_SIMILARITY = 0.5

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            logger.warning("idea feedback at %s unreadable; starting empty", self.path)
            return
        if isinstance(data, list):
            self.entries = [e for e in data if isinstance(e, dict)][-self.MAX_ENTRIES:]

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, indent=1))
            tmp.replace(self.path)
        except OSError:
            logger.exception("failed to save idea feedback to %s", self.path)

    def record(self, tool_name: str, description: str, approved: bool) -> None:
        self.entries.append({
            "tool_name": str(tool_name or ""),
            "description": str(description or "")[:300],
            "approved": bool(approved),
            "at": time.time(),
        })
        self.entries = self.entries[-self.MAX_ENTRIES:]
        self._save()

    def resembles_rejected(self, tool_name: str, description: str) -> bool:
        idea = _tokens(tool_name, description)
        for entry in self.entries:
            if entry.get("approved"):
                continue
            if entry.get("tool_name") == tool_name:
                return True
            past = _tokens(entry.get("tool_name", ""), entry.get("description", ""))
            if _similarity(idea, past) >= self.REJECT_SIMILARITY:
                return True
        return False

    def summary(self, limit: int = 8) -> str:
        """Recent verdicts as judge-prompt lines (newest last)."""
        lines = []
        for entry in self.entries[-limit:]:
            verdict = "approved" if entry.get("approved") else "rejected"
            lines.append(
                f"- {verdict}: {entry.get('tool_name')} — {entry.get('description')}"
            )
        return "\n".join(lines)


def parse_judge(raw: str) -> tuple[Optional[int], str]:
    """Pull ``(score 0-10, reason)`` out of a judge reply.

    ``score`` is ``None`` when the reply has no score; ``reason`` is ``""``
    when it has none.
    """
    text = (raw or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            score = data.get("score")
            if isinstance(score, (int, float)):
                return max(0, min(10, int(score))), str(data.get("reason") or "")[:300]
        except (ValueError, AttributeError):
            pass
    match = re.search(r'"?score"?\s*[:=]\s*(\d{1,2})', text, re.IGNORECASE)
    if match:
        return max(0, min(10, int(match.group(1)))), ""
    return None, ""


def parse_score(raw: str) -> Optional[int]:
    """Pull an integer 0-10 score out of a judge reply; ``None`` if absent."""
    return parse_judge(raw)[0]


def passes_checks(idea: dict[str, Any]) -> bool:
    """Deterministic filter: well-formed name and a usable spec."""
    name = str(idea.get("tool_name") or "")
    logic = str(idea.get("expected_logic") or "")
    desc = str(idea.get("description") or "")
    return bool(_SNAKE_RE.match(name)) and len(logic) >= _MIN_LOGIC_CHARS and bool(desc)


# Words in the events text that say nothing about what the user is doing:
# the prompt header, event verbs, path scaffolding and metadata keys.
_EVENT_STOPWORDS = frozenset(
    "recent events oldest first created modified deleted renamed moved "
    "switched imessage received users volumes private var tmp true false "
    "none null bundle pid com apple sender text rowid action path".split()
) | {Path.home().name.lower()}


def _ground_tokens(text: str, stop: frozenset[str] = frozenset()) -> set[str]:
    """Content words of ``text`` not in ``stop``, plural-folded (``invoices`` -> ``invoice``)."""
    out: set[str] = set()
    for word in _tokens(text):
        if len(word) < 3 or word.isdigit() or word in stop:
            continue
        out.add(word[:-1] if len(word) > 4 and word.endswith("s") else word)
    return out


def grounded_in_events(idea: dict[str, Any], events_text: str) -> bool:
    """Deterministic pre-judge check: the idea names something in the window.

    True when the idea's name, description or logic shares at least one
    content word with the events: an app, a file-name word, an extension
    or a folder. An idea that shares none is unrelated to what the user
    is doing, which the judge rubric already scores 0-3, so dropping it
    here only saves a model call.
    """
    seen = _ground_tokens(events_text, _EVENT_STOPWORDS)
    idea_words = _ground_tokens(
        " ".join(str(idea.get(k) or "") for k in ("tool_name", "description", "expected_logic"))
    )
    return bool(seen & idea_words)


def _describe(event: ContextEvent) -> str:
    meta = event.metadata or {}
    if event.event_type == "file":
        if meta.get("from"):
            return f"file {meta.get('action', '?')}: {meta['from']} -> {meta.get('path', '?')}"
        return f"file {meta.get('action', '?')}: {meta.get('path', '?')}"
    if event.event_type == "imessage_received":
        return f"iMessage received: {str(meta.get('text') or '')[:200]!r}"
    if event.event_type == "app_activated":
        title = f" — {event.window_title}" if event.window_title else ""
        return f"switched to {event.app_name or '?'}{title}"
    return f"{event.event_type}: {json.dumps(meta, default=str)[:200]}"


class OpenHelpSuggester:
    """Ask the guiding question over recent events; publish filtered ideas."""

    source_name = "open_help"
    # Events produced by the agent itself, or handled by chat.
    _SKIP_TYPES = frozenset({"pattern_detected", "user_message"})

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        judge: Evaluate,
        feedback: Optional[IdeaFeedback] = None,
        interval_seconds: float = 600.0,
        window_size: int = 20,
        min_events: int = 3,
        min_score: int = 7,
        idea_cooldown_seconds: float = 7 * 24 * 3600.0,
        publish: Optional[Publish] = None,
        state: Optional[IdeaState] = None,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self._judge = judge
        self.feedback = feedback or IdeaFeedback()
        self.interval_seconds = interval_seconds
        self.min_events = min_events
        self.min_score = min_score
        self.idea_cooldown_seconds = idea_cooldown_seconds
        self._publish = publish or self.event_bus.push
        self._window: deque[ContextEvent] = deque(maxlen=window_size)
        self._new_since_ask = 0
        self._next_ask_at = 0.0
        # Per-idea cooldown, persisted across restarts (todo #32).
        self.state = state if state is not None else IdeaState()
        self._task: Optional[asyncio.Task[None]] = None
        # Outcome of the latest filter run (stage, score, reason); read by
        # :func:`evaluate_windows` and handy when debugging.
        self.last_verdict: dict[str, Any] = {}

    # ---- generate ------------------------------------------------------

    def observe(self, event: ContextEvent) -> Optional[asyncio.Task[None]]:
        """Buffer ``event``; ask the guiding question when due."""
        if event.event_type in self._SKIP_TYPES:
            return None
        # The window goes to the detection engine and its judge (#31).
        self._window.append(redact_event(event, "detection"))
        self._new_since_ask += 1
        now = time.time()
        if self._new_since_ask < self.min_events or now < self._next_ask_at:
            return None
        if self._task is not None and not self._task.done():
            return None
        self._next_ask_at = now + self.interval_seconds
        self._new_since_ask = 0
        prompt = "Recent events (oldest first):\n" + "\n".join(
            f"- {_describe(e)}" for e in self._window
        )
        self._task = asyncio.create_task(self._run(prompt), name="open-help")
        return self._task

    async def _run(self, events_text: str) -> None:
        try:
            raw = await self._evaluate(events_text)
        except Exception:
            logger.exception("open_help evaluate failed")
            return
        idea = parse_suggestion(raw)
        if idea is None:
            return
        if not await self._accept(idea, events_text):
            return
        idea["source"] = self.source_name
        idea["trigger"] = "open_help"
        self.state.start(self.source_name, idea["tool_name"], self.idea_cooldown_seconds)
        try:
            await self._publish(ContextEvent(event_type="pattern_detected", metadata=idea))
        except Exception:
            logger.exception("open_help failed to publish suggestion")

    # ---- filter --------------------------------------------------------

    async def _accept(self, idea: dict[str, Any], events_text: str) -> bool:
        name = str(idea.get("tool_name") or "")
        desc = str(idea.get("description") or "")
        self.last_verdict = {"tool_name": name, "stage": "checks", "score": None, "reason": ""}
        if not passes_checks(idea):
            logger.info("open_help dropped malformed idea %r", name)
            return False
        if not grounded_in_events(idea, events_text):
            self.last_verdict["stage"] = "grounding"
            logger.info("open_help dropped %r: names nothing in the event window", name)
            return False
        if self.state.active(self.source_name, name):
            self.last_verdict["stage"] = "cooldown"
            return False
        if self.feedback.resembles_rejected(name, desc):
            self.last_verdict["stage"] = "feedback"
            logger.info("open_help dropped %r: resembles a rejected idea", name)
            return False
        history = self.feedback.summary()
        judge_prompt = (
            f"{events_text}\n\nProposed tool: {name}\n"
            f"Description: {desc}\n"
            f"Expected logic: {str(idea.get('expected_logic'))[:800]}"
            + (f"\n\nThe user's past verdicts:\n{history}" if history else "")
        )
        self.last_verdict["stage"] = "judge"
        try:
            score, reason = parse_judge(await self._judge(judge_prompt))
        except Exception:
            logger.exception("open_help judge failed")
            return False
        self.last_verdict.update(score=score, reason=reason)
        if score is None or score < self.min_score:
            logger.info(
                "open_help dropped %r: judge score %s (min %s): %s",
                name, score, self.min_score, reason,
            )
            return False
        logger.info(
            "open_help passed %r: judge score %s (min %s): %s",
            name, score, self.min_score, reason,
        )
        idea["judge_score"] = score
        idea["judge_reason"] = reason
        self.last_verdict["stage"] = "passed"
        return True

    # ---- learn ---------------------------------------------------------

    def record_feedback(self, tool_name: str, description: str, approved: bool) -> None:
        """Called by the orchestrator when the user rules on a tool idea."""
        self.feedback.record(tool_name, description, approved)


# ---- evaluation (todo #36) -------------------------------------------
#
# A fixed set of event windows, each with the idea a generator produced
# for it and the verdict the filter should reach, lives in
# ``tests/fixtures/open_help_windows.json``. :func:`evaluate_windows` runs
# them through the real filter; the unit tests use a fake judge, and
# ``python -m ai.open_help`` points it at the configured engine to measure
# the pass rate (a live check: it makes real model calls).

DEFAULT_WINDOWS_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "open_help_windows.json"


def load_windows(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Read the evaluation windows (a JSON list of cases)."""
    return json.loads(Path(path or DEFAULT_WINDOWS_PATH).read_text())


def _fixture_event(raw: dict[str, Any]) -> ContextEvent:
    return ContextEvent(
        event_type=str(raw["event_type"]),
        app_name=raw.get("app_name"),
        window_title=raw.get("window_title"),
        metadata=dict(raw.get("metadata") or {}),
    )


async def evaluate_windows(
    cases: list[dict[str, Any]],
    *,
    judge: Evaluate,
    evaluate: Optional[Evaluate] = None,
    min_score: int = 7,
) -> list[dict[str, Any]]:
    """Run each case's window through the open-help filter.

    By default the case's recorded ``idea`` stands in for the generator,
    so only the filter (checks, grounding, judge) is measured. Pass
    ``evaluate`` to generate ideas with a real model as well. Feedback is
    in memory only: the user's ``idea_feedback.json`` and
    ``idea_state.json`` are never read.
    """
    results = []
    for case in cases:
        events = [_fixture_event(e) for e in case["events"]]
        published: list[ContextEvent] = []

        async def fixed_idea(_: str, idea=case.get("idea")) -> str:
            return json.dumps(idea) if idea else NO_SUGGESTION

        async def publish(ev: ContextEvent) -> None:
            published.append(ev)

        suggester = OpenHelpSuggester(
            event_bus=EventBus(), evaluate=evaluate or fixed_idea, judge=judge,
            feedback=IdeaFeedback(), state=IdeaState(), interval_seconds=0.0,
            window_size=max(20, len(events)), min_events=len(events),
            min_score=min_score, publish=publish,
        )
        for event in events:
            task = suggester.observe(event)
            if task:
                await task
        verdict = suggester.last_verdict or {"stage": "no_idea", "score": None, "reason": ""}
        got = "pass" if published else "drop"
        results.append({
            "id": case["id"],
            "expected": case["expected"],
            "got": got,
            "ok": got == case["expected"],
            "tool_name": verdict.get("tool_name"),
            "stage": verdict.get("stage"),
            "score": verdict.get("score"),
            "reason": verdict.get("reason"),
        })
    return results


def summarize(results: list[dict[str, Any]]) -> str:
    """One line per case plus pass rate and agreement with the expected verdicts."""
    lines = [
        f"{'OK ' if r['ok'] else 'BAD'} {r['id']}: expected {r['expected']}, got "
        f"{r['got']} ({r['stage']}, score {r['score']}) {r['reason'] or ''}".rstrip()
        for r in results
    ]
    passed = sum(r["got"] == "pass" for r in results)
    agree = sum(r["ok"] for r in results)
    n = len(results) or 1
    lines.append(
        f"pass rate {passed}/{len(results)} ({100 * passed // n}%); "
        f"matches expected {agree}/{len(results)}"
    )
    return "\n".join(lines)


def _main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - live check
    import argparse

    from ai.pattern_recognizer import make_detection_evaluator
    from config import settings
    from main import _build_engines, _select_detection_engine

    parser = argparse.ArgumentParser(description="Measure the open-help judge on recorded windows.")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_WINDOWS_PATH)
    parser.add_argument("--generate", action="store_true",
                        help="also generate ideas with the model instead of the recorded ones")
    parser.add_argument("--min-score", type=int, default=settings.open_help_min_score)
    args = parser.parse_args(argv)
    engine = _select_detection_engine(settings.pattern_detection_engine, *_build_engines())
    judge = make_detection_evaluator(engine, max_tokens=120, system_prompt=IDEA_JUDGE_SYSTEM_PROMPT)
    evaluate = (
        make_detection_evaluator(engine, system_prompt=OPEN_HELP_SYSTEM_PROMPT)
        if args.generate else None
    )
    results = asyncio.run(evaluate_windows(
        load_windows(args.path), judge=judge, evaluate=evaluate, min_score=args.min_score,
    ))
    print(f"providers={settings.engine_providers()} model={getattr(engine, 'model_name', '?')}")
    print(summarize(results))
    return 0


__all__ = [
    "GUIDING_QUESTION",
    "IDEA_JUDGE_SYSTEM_PROMPT",
    "NO_SUGGESTION",
    "OPEN_HELP_SYSTEM_PROMPT",
    "IdeaFeedback",
    "OpenHelpSuggester",
    "evaluate_windows",
    "grounded_in_events",
    "load_windows",
    "parse_judge",
    "parse_score",
    "passes_checks",
    "summarize",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
