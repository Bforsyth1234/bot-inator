"""#129: an identical tool call repeated in one agent turn is skipped."""
from __future__ import annotations

import asyncio
import os

os.environ["AGENT_DISABLE_LISTENERS"] = "1"

from ai import repeat_guard  # noqa: E402
from evals.suites import tool_calls  # noqa: E402
from tests.test_evals import SETTINGS, _case, _scripted_model  # noqa: E402
from tools import AVAILABLE_TOOLS  # noqa: E402


def _agent(*steps):
    calls: list[dict] = []
    agent = repeat_guard.agent_class()(
        tools=tool_calls.stub_tools(list(AVAILABLE_TOOLS), calls),
        model=_scripted_model(*steps), verbosity_level=0,
    )
    return agent, calls


def test_identical_repeat_is_skipped_and_model_told_to_answer():
    url = {"url": "https://example.com"}
    agent, calls = _agent(("open_url", url), ("open_url", dict(url)))
    agent.run("open example.com", max_steps=4)
    assert [c["tool"] for c in calls] == ["open_url"]
    assert agent.repeats_skipped == 1
    observations = " ".join(str(getattr(s, "observations", "")) for s in agent.memory.steps)
    assert "Do not call it again" in observations


def test_different_arguments_both_run():
    agent, calls = _agent(("open_url", {"url": "https://a.example"}),
                          ("open_url", {"url": "https://b.example"}))
    agent.run("open both", max_steps=4)
    assert [c["args"]["url"] for c in calls] == ["https://a.example", "https://b.example"]
    assert agent.repeats_skipped == 0


def test_each_run_starts_fresh():
    url = {"url": "https://example.com"}
    agent, calls = _agent(("open_url", url))
    agent.run("first", max_steps=3)
    agent.model.pending = [("open_url", dict(url))]
    agent.run("second", max_steps=3)
    assert len(calls) == 2 and agent.repeats_skipped == 0


def test_eval_case_with_repeat_now_passes():
    """The live Qwen3.5-4B failure: right tool, then the same call again."""
    case = _case("chat_open_url")
    url = {"url": "https://github.com"}
    result = asyncio.run(tool_calls.run_case(
        case, None, SETTINGS, model=_scripted_model(("open_url", url), ("open_url", dict(url)))))
    assert result["got"] == "open_url" and result["ok"], result


def test_orchestrator_builds_the_guarded_agent(monkeypatch):
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus

    orch = Orchestrator(engine=None, event_bus=EventBus(), tools=[])
    monkeypatch.setattr(orch, "_build_smolagents_model", lambda: _scripted_model())
    assert isinstance(orch._build_agent(), repeat_guard.agent_class())
