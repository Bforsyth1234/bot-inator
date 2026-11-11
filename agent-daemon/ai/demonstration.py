""""Watch me" (#84): record one demonstration, get a reviewed tool.

The user starts a recording (menu item or hotkey), does the task once,
stops it, reviews the steps (each line can be removed) and says in one
sentence what the task was. The trace + sentence become a tool SPEC
(``tool_name`` / ``description`` / ``expected_logic``) published as a
``pattern_detected`` idea with ``source="demonstration"``; from there the
normal pipeline runs unchanged: idea card, draft, checks, critic, dry run,
code card (invariant 7b), sandbox (#22). Nothing is replayed: no clicks or
keystrokes are recorded.

Decisions (todo #84, binding):

1. Explicit and short. Nothing is recorded outside a recording; a
   recording stops by itself after :data:`MAX_SECONDS` (5 minutes).
2. The trace may hold content (file names and paths, app names, window
   titles, the active tab's URL, the clipboard *kind*, never its content).
   It lives in memory only (this module has no file, no database and never
   touches ``ai.activity_store``), is shown to the user before anything is
   drafted, and is dropped after the draft (or on cancel, or
   :data:`STOPPED_TTL_SECONDS` after the stop).
3. The output is a spec, not a replay (:func:`to_idea`, deterministic, no
   model call here).
4. Cloud engines (decided for wave 16: REFUSE). The trace is personal text
   (#31/#52/#51). When the main or the drafting engine is a cloud one and
   personal text is not allowed, a recording is refused at start with a
   reason, and a draft is refused again if the setting changed meanwhile
   (:func:`cloud_refusal`). Redacting would leave a spec with no paths or
   names, i.e. a tool that cannot do the demonstrated job.
5. If a built-in or a Shortcut already does the steps (``map_to_tool`` in
   ``ai.routines``), the idea is that built-in instead of a new tool.

While a recording is on, the orchestrator hands every event to
:meth:`DemonstrationController.intercept` before anything else (before
#87 delivery, the activity history, observers and idea sources):

* step events (:data:`STEP_EVENT_TYPES`) are recorded and go no further:
  no agent turn, no idea source, no card while the user is demonstrating;
* ``user_message`` passes through (the user asked; chat keeps working);
* every other event (mail, iMessage, a timer's idea...) is deferred in
  memory, in order, and pushed back on the bus when the recording ends,
  so nothing is lost and nothing interrupts the demonstration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Iterable, Optional

from ai import privacy, routines
from ai.delivery import BYPASS_KEY
from events.event_bus import ContextEvent

logger = logging.getLogger(__name__)

SOURCE = "demonstration"
TRIGGER = "demonstration"

MAX_SECONDS = 300.0
MAX_EVENTS = 300
MAX_DEFERRED = 50
MAX_STEPS_IN_IDEA = 40
STOPPED_TTL_SECONDS = 1800.0
MAX_SENTENCE_CHARS = 300

STEP_EVENT_TYPES: frozenset[str] = frozenset({"app_activated", "file", "clipboard_changed"})
PASS_THROUGH_TYPES: frozenset[str] = frozenset({"user_message"})
# Clicking the menu bar icon to stop activates this app: not a step.
OWN_APP_NAMES: frozenset[str] = frozenset({"menubarui", "bot-inator", "botinator"})
BROWSERS: frozenset[str] = frozenset({"safari", "google chrome"})
_TEMP_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")

PATTERN_SOURCE_INTRO = (
    "The user recorded one demonstration of a task on their Mac (they "
    "started and stopped it themselves, reviewed the steps and described "
    "the task in a sentence), and a tool that does those steps was proposed."
)

# States shown by the panel.
IDLE, RECORDING, STOPPED, DRAFTED, REFUSED = "idle", "recording", "stopped", "drafted", "refused"

Clock = Callable[[], float]
Enrich = Callable[[ContextEvent], dict[str, Any]]
SendFrame = Callable[[dict[str, Any]], Awaitable[None]]
Publish = Callable[[ContextEvent], Awaitable[None]]


class DemonstrationRefused(RuntimeError):
    """A recording or a draft that may not happen; ``str()`` is the reason."""


# ---------------------------------------------------------------------------
# Privacy (decision 4)
# ---------------------------------------------------------------------------


def cloud_refusal() -> Optional[str]:
    """The reason a demonstration may not be recorded or drafted, or None."""
    roles = [r for r in ("main", "drafting") if privacy.should_redact(r)]
    if not roles:
        return None
    return (
        f"The {' and '.join(roles)} engine runs in the cloud, and a "
        "demonstration holds file names, paths, window titles and URLs. "
        "Switch that engine to local, or allow personal text for cloud "
        "engines in Settings, to record one."
    )


# ---------------------------------------------------------------------------
# Steps and the deterministic summarizer
# ---------------------------------------------------------------------------


@dataclass
class RawEntry:
    index: int
    ts: float
    event_type: str
    app_name: str
    metadata: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    id: str
    kind: str  # app | file_new | file_move | file_rename | file_change | copy
    text: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text}


def _home_rel(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _name(path: str) -> str:
    return PurePosixPath(path).name or path


def _folder(path: str) -> str:
    return _home_rel(str(PurePosixPath(path).parent))


def _is_own_app(name: str) -> bool:
    return name.strip().lower() in OWN_APP_NAMES


def _app_text(app: str, extra: dict[str, Any]) -> str:
    title = str(extra.get("title") or "").strip()
    url = str(extra.get("url") or "").strip()
    text = f"Switched to {app}"
    if title:
        text += f": {title[:120]}"
    if url:
        text += f" ({url[:200]})"
    return text


def summarize(raw: Iterable[RawEntry]) -> list[Step]:
    """Collapse raw events into the steps the user reviews.

    Deterministic: app switches with nothing in between collapse to the
    last one; a file that appears and is then moved or renamed is one step;
    repeated changes to a file seen in this recording are absorbed; this
    app's own activation (stopping the recording) is not a step. A step's
    id comes from its first raw event, so it is stable while recording.
    """
    steps: list[Step] = []
    by_path: dict[str, Step] = {}

    def last() -> Optional[Step]:
        return steps[-1] if steps else None

    for e in raw:
        sid = f"s{e.index}"
        meta = e.metadata or {}
        if e.event_type == "app_activated":
            app = (e.app_name or "").strip()
            if not app or _is_own_app(app):
                continue
            step = Step(sid, "app", _app_text(app, e.extra), {"app": app, **{
                k: e.extra[k] for k in ("title", "url") if e.extra.get(k)}})
            prev = last()
            if prev is not None and prev.kind == "app":
                if prev.detail.get("app") == app and not e.extra:
                    continue
                steps[-1] = step
            else:
                steps.append(step)
        elif e.event_type == "file":
            path = str(meta.get("path") or "")
            if not path:
                continue
            action = str(meta.get("action") or "")
            src = str(meta.get("from") or "")
            if action == "renamed" and src:
                earlier = by_path.pop(src, None)
                downloaded = src.lower().endswith(_TEMP_SUFFIXES)
                if downloaded:
                    step = earlier or Step(sid, "file_new", "", {})
                    step.kind, step.detail = "file_new", {"path": path, "downloaded": True}
                    step.text = f"Downloaded {_name(path)} to {_folder(path)}"
                    if earlier is None:
                        steps.append(step)
                    by_path[path] = step
                    continue
                same_dir = PurePosixPath(src).parent == PurePosixPath(path).parent
                kind = "file_rename" if same_dir else "file_move"
                if same_dir:
                    text = f"Renamed {_name(src)} to {_name(path)} in {_folder(path)}"
                else:
                    text = f"Moved {_name(src)} from {_folder(src)} to {_folder(path)}"
                    if _name(src) != _name(path):
                        text += f" as {_name(path)}"
                detail = {"from": src, "path": path}
                if earlier is not None and earlier.kind == "file_new":
                    verb = "downloaded to" if earlier.detail.get("downloaded") else "arrived in"
                    if same_dir:
                        text = (f"{_name(src)} {verb} {_folder(src)}, then renamed "
                                f"to {_name(path)}")
                    else:
                        text = (f"{_name(src)} {verb} {_folder(src)}, then moved to "
                                f"{_folder(path)}")
                        if _name(src) != _name(path):
                            text += f" as {_name(path)}"
                    detail["arrived"] = True
                    earlier.kind, earlier.text, earlier.detail = kind, text, detail
                    by_path[path] = earlier
                    continue
                if earlier is not None:
                    detail["after"] = earlier.kind
                    earlier.kind, earlier.text, earlier.detail = kind, text, detail
                    by_path[path] = earlier
                    continue
                step = Step(sid, kind, text, detail)
                steps.append(step)
                by_path[path] = step
            elif action == "created":
                if path in by_path:
                    continue
                step = Step(sid, "file_new", f"New file {_name(path)} in {_folder(path)}",
                            {"path": path})
                steps.append(step)
                by_path[path] = step
            else:  # modified (or anything else): a change
                if path in by_path:
                    continue
                step = Step(sid, "file_change", f"Changed {_name(path)} in {_folder(path)}",
                            {"path": path})
                steps.append(step)
                by_path[path] = step
        elif e.event_type == "clipboard_changed":
            # The kind only: the watcher never reads the copied content.
            kind = str(meta.get("kind") or "something")
            app = str(meta.get("app") or e.app_name or "").strip()
            text = f"Copied {kind}" + (f" in {app}" if app else "")
            prev = last()
            if prev is not None and prev.kind == "copy" and prev.text == text:
                continue
            steps.append(Step(sid, "copy", text, {"kind": kind, "app": app}))
    return steps


# ---------------------------------------------------------------------------
# The spec (decision 3) and the built-in check (decision 5)
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an the to it and my of then into in from i me this that these those "
    "is are was be for on at with each every any".split()
)


def tool_name_for(sentence: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", sentence.lower()) if w not in _STOPWORDS]
    name = "_".join(words[:5])[:40].strip("_")
    if not name:
        return "demonstrated_task"
    if name[0].isdigit():
        name = "do_" + name
    return name


def _clean_sentence(sentence: str) -> str:
    text = " ".join(str(sentence or "").split())[:MAX_SENTENCE_CHARS]
    return text.replace('"', "'")


def _builtin_for(steps: list[Step], sentence: str, shortcuts: list[str]) -> Optional[dict[str, Any]]:
    """``map_to_tool``'s pick when a built-in already does the steps."""
    lowered = sentence.lower()
    for name in shortcuts:
        if name and name.lower() in lowered:
            return {"tool": "run_shortcut", "args": {"name": name}}
    if steps and all(s.kind == "app" for s in steps):
        apps: list[str] = []
        for s in steps:
            app = str(s.detail.get("app") or "")
            if app and app not in apps:
                apps.append(app)
        mapped = routines.map_to_tool({"action": {"type": "app_launch", "apps": apps}}, shortcuts)
        if mapped.get("tool") in ("open_app", "run_shortcut"):
            return mapped
    return None


def to_idea(steps: list[Step], sentence: str, *,
            shortcuts: Iterable[str] = ()) -> dict[str, Any]:
    """The ``pattern_detected`` metadata for a reviewed demonstration.

    Raises :class:`DemonstrationRefused` for no steps, no sentence, or a
    cloud engine that may not see the trace (decision 4).
    """
    reason = cloud_refusal()
    if reason:
        raise DemonstrationRefused(reason)
    sentence = _clean_sentence(sentence)
    if not sentence:
        raise DemonstrationRefused("Say in a sentence what the task was.")
    if not steps:
        raise DemonstrationRefused("No steps are left to make a tool from.")
    shown = steps[:MAX_STEPS_IN_IDEA]
    numbered = "\n".join(f"{i}. {s.text}" for i, s in enumerate(shown, 1))
    card_text = f"From your demonstration ({len(steps)} steps):\n{numbered}"
    base: dict[str, Any] = {
        "source": SOURCE,
        "trigger": TRIGGER,
        "description": sentence,
        "card_text": card_text,
        "demonstration_steps": len(steps),
        # The user asked for this just now: never held or capped (#87).
        BYPASS_KEY: True,
    }
    builtin = _builtin_for(steps, sentence, [str(s) for s in shortcuts])
    if builtin is not None:
        tool = str(builtin["tool"])
        calls = builtin.get("calls") or [builtin.get("args") or {}]
        details = (f"{sentence}\nSteps:\n{numbered}\n"
                   f"Arguments: {json.dumps(calls, sort_keys=True)}")
        base.update({
            "tool_name": tool,
            "builtin_tool": tool,
            "expected_logic": f"The built-in {tool} already does this.",
            "event_details": details,
            "event_details_redacted": privacy.PLACEHOLDER,
        })
        return base
    logic = (
        "The user showed these steps once, in order:\n"
        f"{numbered}\n"
        f"In the user's words, the task is: {sentence}\n"
        "Write a tool that does the same job again for a new input. Take the "
        "values that change from run to run (for example the file that "
        "arrived, or the folder it goes to) as parameters, with the "
        "demonstrated folders as defaults where that makes sense. Work on "
        "files, folders, apps and URLs directly with the Python standard "
        "library and macOS commands (`open`, `osascript`); do not replay "
        "clicks or keystrokes. Never delete anything. Return a dict saying "
        "what was done, and an error when an input is missing."
    )
    base.update({"tool_name": tool_name_for(sentence), "expected_logic": logic})
    return base


# ---------------------------------------------------------------------------
# Enrichment at a step (read-only tools)
# ---------------------------------------------------------------------------


def default_enrich(event: ContextEvent) -> dict[str, Any]:
    """Window title / active tab for an app switch (blocking, read-only).

    Uses ``read_active_tab`` for Safari / Chrome (a sensitive-site URL is
    dropped) and ``list_windows`` otherwise. Any failure returns ``{}``.
    """
    if event.event_type != "app_activated":
        return {}
    app = str(event.app_name or "")
    try:
        from ai.delivery import is_never_watched

        if is_never_watched((event.metadata or {}).get("bundle_id"), app):
            return {}
        if app.lower() in BROWSERS:
            from tools import read_active_tab as rat

            tab = rat._find_active_tab() or {}
            url = str(tab.get("url") or "")
            if url and rat.is_sensitive_url(url):
                return {}
            return {k: v for k, v in (("title", tab.get("title")), ("url", url)) if v}
        from tools.windows import list_windows

        result = list_windows()
        for w in (result or {}).get("windows", []):
            if w.get("app") == app and w.get("title"):
                return {"title": str(w["title"])}
    except Exception:
        logger.debug("demonstration enrich failed", exc_info=True)
    return {}


# ---------------------------------------------------------------------------
# The recorder (in memory only)
# ---------------------------------------------------------------------------


class Recorder:
    """One recording: raw events in order, the deadline, removed lines."""

    def __init__(self, *, clock: Clock = time.time, max_seconds: float = MAX_SECONDS,
                 max_events: int = MAX_EVENTS) -> None:
        self.clock = clock
        self.max_seconds = max_seconds
        self.max_events = max_events
        self.state = IDLE
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self.stop_reason = ""
        self.raw: list[RawEntry] = []
        self.removed: set[str] = set()
        self._index = 0

    @property
    def recording(self) -> bool:
        return self.state == RECORDING

    @property
    def deadline(self) -> Optional[float]:
        return None if self.started_at is None else self.started_at + self.max_seconds

    def start(self) -> None:
        self.clear()
        self.state = RECORDING
        self.started_at = self.clock()

    def expired(self) -> bool:
        return self.recording and self.deadline is not None and self.clock() >= self.deadline

    def add(self, event: ContextEvent, extra: Optional[dict[str, Any]] = None) -> bool:
        """Record a step event; False when not recording, over time or full."""
        if not self.recording or event.event_type not in STEP_EVENT_TYPES:
            return False
        if self.expired():
            self.stop("time")
            return False
        if len(self.raw) >= self.max_events:
            return False
        self._index += 1
        self.raw.append(RawEntry(
            index=self._index, ts=self.clock(), event_type=event.event_type,
            app_name=str(event.app_name or ""), metadata=dict(event.metadata or {}),
            extra=dict(extra or {}),
        ))
        return True

    def stop(self, reason: str = "user") -> None:
        if self.recording:
            self.state = STOPPED
            self.stopped_at = self.clock()
            self.stop_reason = reason

    def steps(self) -> list[Step]:
        return [s for s in summarize(self.raw) if s.id not in self.removed]

    def remove(self, step_ids: Iterable[str]) -> None:
        self.removed.update(str(s) for s in step_ids)

    def clear(self) -> None:
        """Drop the trace (after the draft, on cancel, on a new start)."""
        self.state = IDLE
        self.started_at = self.stopped_at = None
        self.stop_reason = ""
        self.raw = []
        self.removed = set()
        self._index = 0


# ---------------------------------------------------------------------------
# The controller the runner wires (orchestrator + WS commands)
# ---------------------------------------------------------------------------


async def _no_send(_: dict[str, Any]) -> None:
    return None


class DemonstrationController:
    """Start / stop / cancel / draft, the event gate and the 5-minute stop.

    ``send`` broadcasts a ``demonstration_trace`` payload (a dict, see
    :meth:`trace`); ``publish`` puts the finished idea on the bus;
    ``requeue`` puts deferred events back (defaults to ``publish``).
    """

    def __init__(
        self,
        *,
        publish: Publish,
        send: SendFrame = _no_send,
        requeue: Optional[Publish] = None,
        enrich: Optional[Enrich] = default_enrich,
        shortcuts: Optional[Callable[[], Iterable[str]]] = None,
        clock: Clock = time.time,
        max_seconds: float = MAX_SECONDS,
        stopped_ttl: float = STOPPED_TTL_SECONDS,
    ) -> None:
        self.recorder = Recorder(clock=clock, max_seconds=max_seconds)
        self._publish = publish
        self._send = send
        self._requeue = requeue or publish
        self._enrich = enrich
        self._shortcuts = shortcuts
        self.stopped_ttl = stopped_ttl
        self.deferred: list[ContextEvent] = []
        self.reason = ""
        self.tool_name = ""
        self.recording_id = ""
        self._timer: Optional[asyncio.Task[None]] = None

    @property
    def recording(self) -> bool:
        return self.recorder.recording

    # ---- the frame ------------------------------------------------------

    def trace(self) -> dict[str, Any]:
        """The ``demonstration_trace`` payload."""
        rec = self.recorder
        state = rec.state
        if state == IDLE and self.tool_name:
            state = DRAFTED
        elif state == IDLE and self.reason:
            state = REFUSED
        return {
            "recording_id": self.recording_id,
            "state": state,
            "started_at": rec.started_at,
            "ends_at": rec.deadline if rec.recording else None,
            "stop_reason": rec.stop_reason or None,
            "steps": [s.to_dict() for s in rec.steps()] if rec.state != IDLE else [],
            "reason": self.reason or None,
            "tool_name": self.tool_name or None,
            "deferred_count": len(self.deferred),
        }

    async def _broadcast(self) -> None:
        try:
            await self._send(self.trace())
        except Exception:
            logger.exception("demonstration_trace send failed")

    # ---- commands ---------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        self._cancel_timer()
        self.reason = self.tool_name = ""
        refusal = cloud_refusal()
        if refusal:
            self.recorder.clear()
            self.reason = refusal
            await self._broadcast()
            return self.trace()
        self.recorder.start()
        self.recording_id = f"demo_{uuid.uuid4().hex[:10]}"
        self._timer = asyncio.ensure_future(self._after(self.recorder.max_seconds, "time"))
        await self._broadcast()
        return self.trace()

    async def stop(self, reason: str = "user") -> dict[str, Any]:
        if self.recording:
            self.recorder.stop(reason)
            self._cancel_timer()
            self._timer = asyncio.ensure_future(self._after(self.stopped_ttl, "ttl"))
            await self._release_deferred()
        await self._broadcast()
        return self.trace()

    async def cancel(self) -> dict[str, Any]:
        self._cancel_timer()
        self.recorder.clear()
        self.reason = self.tool_name = ""
        await self._release_deferred()
        await self._broadcast()
        return self.trace()

    async def draft(self, sentence: str, removed_step_ids: Iterable[str] = ()) -> dict[str, Any]:
        """Turn the reviewed trace into an idea and publish it; drop the trace."""
        if self.recording:
            await self.stop("user")
        rec = self.recorder
        if rec.state != STOPPED:
            self.reason = "There is no stopped recording to make a tool from."
            await self._broadcast()
            return self.trace()
        rec.remove(removed_step_ids)
        try:
            names = list(self._shortcuts()) if self._shortcuts else []
        except Exception:
            logger.info("demonstration: shortcuts unavailable", exc_info=True)
            names = []
        try:
            idea = to_idea(rec.steps(), sentence, shortcuts=names)
        except DemonstrationRefused as exc:
            self.reason = str(exc)
            await self._broadcast()
            return self.trace()
        self._cancel_timer()
        rec.clear()
        self.reason = ""
        self.tool_name = str(idea.get("tool_name") or "")
        await self._publish(ContextEvent(event_type="pattern_detected", metadata=idea))
        await self._broadcast()
        return self.trace()

    # ---- the event gate ---------------------------------------------------

    async def intercept(self, event: ContextEvent) -> bool:
        """True when ``event`` was taken by the recording (handle nothing else).

        Called first in ``Orchestrator._handle_event`` (after notices).
        """
        if not self.recording:
            return False
        if self.recorder.expired():
            await self.stop("time")
            return False
        etype = event.event_type
        if etype in PASS_THROUGH_TYPES or (event.metadata or {}).get("source") == SOURCE:
            return False
        if etype in STEP_EVENT_TYPES:
            extra: dict[str, Any] = {}
            if self._enrich is not None and etype == "app_activated" \
                    and not _is_own_app(str(event.app_name or "")):
                try:
                    extra = await asyncio.to_thread(self._enrich, event) or {}
                except Exception:
                    logger.debug("demonstration enrich failed", exc_info=True)
            if self.recorder.add(event, extra):
                await self._broadcast()
            return True
        if len(self.deferred) < MAX_DEFERRED:
            self.deferred.append(event)
        else:
            logger.warning("demonstration: deferred queue full, dropped %s", etype)
        return True

    async def _release_deferred(self) -> None:
        pending, self.deferred = self.deferred, []
        for event in pending:
            try:
                await self._requeue(event)
            except Exception:
                logger.exception("demonstration: requeue failed for %s", event.event_type)

    # ---- timers -------------------------------------------------------------

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done() and timer is not _current_task():
            timer.cancel()

    async def _after(self, seconds: float, why: str) -> None:
        try:
            await asyncio.sleep(max(0.0, seconds))
        except asyncio.CancelledError:
            return
        self._timer = None
        if why == "time" and self.recording:
            await self.stop("time")
        elif why == "ttl" and self.recorder.state == STOPPED:
            self.recorder.clear()
            await self._broadcast()


def _current_task() -> Optional[asyncio.Task[Any]]:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
