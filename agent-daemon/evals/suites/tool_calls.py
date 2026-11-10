"""Tool calls: given a chat message or event, does the agent call the
right tool with usable arguments?

Each case runs the real prompt (``Orchestrator._build_chat_prompt`` /
``_build_prompt``) through the real smolagents model adapter and the
built-in tool schemas, with the production step budget. The tools are
stubs: a call is recorded and answered with a canned result, so nothing
is opened, moved, saved or sent, and no approval is asked.

Case shape (``evals/cases/tool_calls.json``)::

    {"id": "...", "kind": "chat", "text": "Open github.com",
     "expect": [{"tool": "open_url", "args": {"url": {"contains": "github.com"}}}],
     "allow": ["list_windows"],            # may appear anywhere, ignored
     "tool_results": {"read_email": {...}}}  # canned stub results

``kind`` is ``chat`` or ``event`` (then ``event`` holds the event).
``expect`` is the exact ordered list of tool calls, ``[]`` for "answer
without a tool". ``tool`` may be a list of acceptable names. An ``args``
matcher is ``{"contains": text}`` (case-insensitive) or ``{"equals": v}``.

Limits: only the built-in tools are offered (not the user's generated
ones), there are no recalled memories, and stub ``read_email`` results
are not redacted for cloud engines (the cases are synthetic).
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Optional

DEFAULT_CASES_PATH = Path(__file__).resolve().parent.parent / "cases" / "tool_calls.json"
DEFAULT_TOOL_RESULT = {"status": "ok"}
CHAT_MAX_STEPS = 6  # as Orchestrator._handle_user_message


def load_cases(path: Optional[Path] = None) -> list[dict[str, Any]]:
    return json.loads(Path(path or DEFAULT_CASES_PATH).read_text())


def role(settings: Any) -> str:
    return "main"


# --- scoring (pure) ---------------------------------------------------------

def _arg_matches(matcher: dict[str, Any], value: Any) -> bool:
    if "equals" in matcher:
        return str(value).strip() == str(matcher["equals"])
    if "contains" in matcher:
        return str(matcher["contains"]).lower() in str(value).lower()
    raise ValueError(f"unknown matcher {matcher!r}")


def score(case: dict[str, Any], calls: list[dict[str, Any]]) -> tuple[bool, str]:
    """``(ok, reason)`` for the recorded ``calls`` (``{"tool", "args"}``)."""
    allow = set(case.get("allow") or [])
    actual = [c for c in calls if c["tool"] not in allow]
    expect = case["expect"]
    for i, want in enumerate(expect):
        names = want["tool"] if isinstance(want["tool"], list) else [want["tool"]]
        if i >= len(actual):
            return False, f"missing call: {' or '.join(names)}"
        got = actual[i]
        if got["tool"] not in names:
            return False, f"wrong tool: wanted {' or '.join(names)}, got {got['tool']}"
        for arg, matcher in (want.get("args") or {}).items():
            value = got["args"].get(arg)
            if value is None:
                return False, f"{got['tool']}: missing argument {arg}"
            if not _arg_matches(matcher, value):
                return False, f"{got['tool']}: {arg}={value!r} does not match {matcher}"
    if len(actual) > len(expect):
        return False, f"extra call: {actual[len(expect)]['tool']}"
    return True, ""


# --- running ----------------------------------------------------------------

def stub_tools(tools: list[Any], calls: list[dict[str, Any]],
               results: Optional[dict[str, Any]] = None) -> list[Any]:
    """Copies of ``tools`` that record each call in ``calls`` and return a
    canned result instead of running."""
    results = results or {}
    out = []
    for tool in tools:
        stub = copy.copy(tool)

        def forward(*args: Any, _name: str = tool.name, **kwargs: Any) -> Any:
            # None is dropped, as the approval wrapper does in production.
            kept = {k: v for k, v in kwargs.items() if v is not None}
            if args:
                kept["_positional"] = list(args)
            calls.append({"tool": _name, "args": kept})
            return results.get(_name, DEFAULT_TOOL_RESULT)

        stub.forward = forward  # type: ignore[attr-defined]
        out.append(stub)
    return out


def _prompt_and_steps(case: dict[str, Any], orchestrator: Any) -> tuple[str, int]:
    from events.event_bus import ContextEvent

    if case["kind"] == "chat":
        event = ContextEvent(event_type="user_message", metadata={"text": case["text"]})
        return orchestrator._build_chat_prompt(event), CHAT_MAX_STEPS
    raw = case["event"]
    event = ContextEvent(
        event_type=str(raw["event_type"]), app_name=raw.get("app_name"),
        window_title=raw.get("window_title"), metadata=dict(raw.get("metadata") or {}),
    )
    # Step budgets as Orchestrator._handle_event.
    return orchestrator._build_prompt(event), 3 if event.event_type == "mail_received" else 2


def _step_errors(agent: Any) -> list[str]:
    """Errors smolagents fed back to the model: unparseable calls, unknown
    tools, arguments that fail the tool's schema."""
    steps = getattr(getattr(agent, "memory", None), "steps", None) or []
    return [
        f"{type(err).__name__}: {str(err)[:160]}"
        for err in (getattr(step, "error", None) for step in steps) if err
    ]


async def run_case(case: dict[str, Any], engine: Any, settings: Any, *, model: Any = None) -> dict[str, Any]:
    """One agent turn. ``model`` (a smolagents ``Model``) replaces the
    engine's adapter in tests."""
    from smolagents import ToolCallingAgent

    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus
    from tools import AVAILABLE_TOOLS

    orchestrator = Orchestrator(engine=engine, event_bus=EventBus(), tools=[])
    if model is None:
        if not getattr(engine, "loaded", True):
            await engine.load()
        model = orchestrator._build_smolagents_model()
        if model is None:
            raise RuntimeError(f"no smolagents model adapter for {type(engine).__name__}")
    calls: list[dict[str, Any]] = []
    agent = ToolCallingAgent(
        tools=stub_tools(list(AVAILABLE_TOOLS), calls, case.get("tool_results")),
        model=model, verbosity_level=0,
    )
    prompt, max_steps = _prompt_and_steps(case, orchestrator)
    answer = await asyncio.to_thread(lambda: agent.run(prompt, max_steps=max_steps))
    ok, reason = score(case, calls)
    return {
        "ok": ok,
        "got": " > ".join(c["tool"] for c in calls) or "no tool",
        "reason": reason,
        "calls": calls,
        "step_errors": _step_errors(agent),
        "answer": str(answer)[:300],
    }
