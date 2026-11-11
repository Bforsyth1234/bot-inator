"""The eval runner (``evals/``), driven by fake engines: no model calls."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from evals import suites
from evals.run import aggregate, compare, previous_result, report, run_suite, save

SETTINGS = SimpleNamespace(open_help_min_score=7, pattern_detection_engine="main")


class FakeEngine:
    """Judges every idea with the same score."""

    loaded = True
    model_name = "fake/model-1"

    def __init__(self, score: int) -> None:
        self.score = score
        self.calls = 0

    def generate_chat_sync(self, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        return json.dumps({"score": self.score, "reason": "fake"})


def _run(score: int, repeats: int = 2):
    suite = suites.get("idea_judge")
    engine = FakeEngine(score)
    runs = asyncio.run(run_suite(suite, suite.load_cases(), engine, SETTINGS, repeats=repeats))
    return runs, engine


def _result(case_runs, model="fake/model-1", started="2026-11-10T10:00:00"):
    return {"suite": "idea_judge", "provider": "local", "model": model,
            "started_at": started, "summary": aggregate(case_runs), "cases": case_runs}


def test_idea_judge_suite_scores_against_expected() -> None:
    runs, engine = _run(score=2)
    assert engine.calls > 0
    assert suites.get("idea_judge").role(SETTINGS) == "main"
    for case in runs:
        assert len(case["runs"]) == 2
        # A judge that scores everything 2 drops everything, so only the
        # expected-drop cases are right.
        assert all(r["got"] == "drop" for r in case["runs"])
        assert all(r["ok"] == (case["expected"] == "drop") for r in case["runs"])
    summary = aggregate(runs)
    drops = sum(c["expected"] == "drop" for c in runs)
    assert summary["solid"] == drops and summary["flaky"] == []
    assert summary["accuracy"] == round(drops / len(runs), 3)


def test_generous_judge_scores_differently() -> None:
    # Non-vacuity: the score reaches the verdict, so a judge that passes
    # everything gets the expected-pass cases right instead.
    runs, _ = _run(score=10, repeats=1)
    by_expected = {c["expected"]: c for c in runs}
    assert by_expected["pass"]["runs"][0]["ok"] is True
    assert aggregate(runs)["accuracy"] != aggregate(_run(score=2, repeats=1)[0])["accuracy"]


def test_a_failing_run_is_recorded_not_raised() -> None:
    class Broken:
        @staticmethod
        async def run_case(case, engine, settings):
            raise RuntimeError("rate limited")

    runs = asyncio.run(run_suite(Broken, [{"id": "a", "expected": "pass"}], None, None, repeats=2))
    assert runs[0]["runs"] == [{"ok": False, "error": "RuntimeError: rate limited"}] * 2
    assert aggregate(runs)["errors"] == 2


def test_aggregate_flags_flaky_cases() -> None:
    summary = aggregate([
        {"id": "a", "runs": [{"ok": True, "got": "pass"}, {"ok": False, "got": "drop"}]},
        {"id": "b", "runs": [{"ok": True, "got": "drop"}, {"ok": True, "got": "drop"}]},
    ])
    assert summary == {"cases": 2, "runs": 4, "ok_runs": 3, "accuracy": 0.75,
                       "solid": 1, "flaky": ["a"], "errors": 0}


def test_compare_lists_changed_cases() -> None:
    def case(cid, oks):
        return {"id": cid, "expected": "pass", "runs": [{"ok": o, "got": "pass" if o else "drop"} for o in oks]}

    old = _result([case("a", [True, True]), case("b", [False, False]), case("c", [True, True])])
    new = _result([case("a", [True, False]), case("b", [True, True]), case("c", [True, True]),
                   case("d", [True, True])], model="fake/model-2")
    lines = compare(old, new)
    assert "fake/model-1" in lines[0] and "67% -> 88%" in lines[0]
    assert lines[1:] == ["  WORSE  a: 2/2 -> 1/2", "  BETTER b: 0/2 -> 2/2", "  NEW    d"]
    assert compare(old, old)[1:] == ["  no case changed"]


def test_save_and_previous_result(tmp_path) -> None:
    runs, _ = _run(score=2, repeats=1)
    first = save(_result(runs, started="2026-11-10T10:00:00"), tmp_path)
    save(_result(runs, model="fake/model-2", started="2026-11-10T11:00:00"), tmp_path)
    assert first.name == "20261110-100000-idea_judge-fake-model-1.json"
    assert previous_result("idea_judge", tmp_path)["model"] == "fake/model-2"
    assert previous_result("other_suite", tmp_path) is None
    assert "idea_judge on fake/model-1 (local)" in report(_result(runs))


# --- tool_calls suite -------------------------------------------------------

from evals.suites import tool_calls  # noqa: E402
from tools import AVAILABLE_TOOLS  # noqa: E402


def _scripted_model(*steps):
    """A smolagents Model that emits the given (tool, args) calls in order."""
    from smolagents.models import (
        ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, Model,
    )

    class Scripted(Model):
        def __init__(self) -> None:
            super().__init__(model_id="fake/scripted")
            self.pending = list(steps)

        def generate(self, messages, stop_sequences=None, response_format=None,
                     tools_to_call_from=None, **kwargs):
            name, args = self.pending.pop(0) if self.pending else ("final_answer", {"answer": "done"})
            call = ChatMessageToolCall(
                function=ChatMessageToolCallFunction(name=name, arguments=args),
                id=f"call_{len(self.pending)}", type="function",
            )
            return ChatMessage(role="assistant", content="", tool_calls=[call])

    return Scripted()


def _case(case_id: str) -> dict:
    return next(c for c in tool_calls.load_cases() if c["id"] == case_id)


def _run_tool_case(case: dict, *steps) -> dict:
    return asyncio.run(tool_calls.run_case(case, None, SETTINGS, model=_scripted_model(*steps)))


def test_tool_call_cases_name_real_tools_and_arguments() -> None:
    tools = {t.name: t for t in AVAILABLE_TOOLS}
    cases = tool_calls.load_cases()
    assert len({c["id"] for c in cases}) == len(cases) >= 20
    for case in cases:
        assert case["kind"] in ("chat", "event"), case["id"]
        assert case["text"] if case["kind"] == "chat" else case["event"]["event_type"], case["id"]
        assert set(case.get("allow", [])) | set(case.get("tool_results", {})) <= set(tools), case["id"]
        for want in case["expect"]:
            names = want["tool"] if isinstance(want["tool"], list) else [want["tool"]]
            for name in names:
                assert set(want.get("args", {})) <= set(tools[name].inputs), (case["id"], name)


def test_score_tool_calls() -> None:
    case = {"allow": ["list_windows"], "expect": [
        {"tool": ["open_app", "run_shortcut"], "args": {"name": {"contains": "calc"}}},
    ]}

    def call(tool, **args):
        return {"tool": tool, "args": args}

    good = call("open_app", name="Calculator")
    assert tool_calls.score(case, [good]) == (True, "")
    assert tool_calls.score(case, [call("list_windows"), good]) == (True, "")
    assert tool_calls.score(case, [call("run_shortcut", name="calc")])[0] is True
    assert "missing call" in tool_calls.score(case, [call("list_windows")])[1]
    assert "wrong tool" in tool_calls.score(case, [call("open_url", url="x")])[1]
    assert "missing argument name" in tool_calls.score(case, [call("open_app")])[1]
    assert "does not match" in tool_calls.score(case, [call("open_app", name="Notes")])[1]
    assert "extra call: open_url" in tool_calls.score(case, [good, call("open_url", url="x")])[1]
    assert tool_calls.score({"expect": []}, []) == (True, "")
    assert tool_calls.score({"expect": []}, [good])[0] is False
    equals = {"expect": [{"tool": "read_email", "args": {"message_id": {"equals": 4201}}}]}
    assert tool_calls.score(equals, [call("read_email", message_id="4201")])[0] is True
    assert tool_calls.score(equals, [call("read_email", message_id=42010)])[0] is False


def test_tool_call_run_records_calls_without_running_tools(monkeypatch) -> None:
    import webbrowser

    opened: list = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: opened.append(a))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: opened.append(a))
    before = [t.forward for t in AVAILABLE_TOOLS]

    run = _run_tool_case(_case("chat_open_url"), ("open_url", {"url": "https://github.com"}))
    assert run["ok"] is True and run["got"] == "open_url" and run["step_errors"] == []
    assert run["calls"] == [{"tool": "open_url", "args": {"url": "https://github.com"}}]
    assert opened == []  # the real tool never ran
    assert [t.forward for t in AVAILABLE_TOOLS] == before  # shared tools untouched


def test_tool_call_run_fails_wrong_tool_and_no_tool() -> None:
    wrong = _run_tool_case(_case("chat_open_url"), ("open_app", {"name": "Safari"}))
    assert wrong["ok"] is False and "wrong tool" in wrong["reason"]
    none = _run_tool_case(_case("chat_open_url"))
    assert none["ok"] is False and none["got"] == "no tool" and none["answer"] == "done"
    assert _run_tool_case(_case("chat_definition_no_tool"))["ok"] is True


def test_tool_call_mail_case_uses_canned_email_and_reports_bad_calls() -> None:
    case = _case("mail_task_reminder")
    run = _run_tool_case(
        case,
        ("read_email", {"message_id": 4201}),
        ("create_reminder", {"title": "Walk the dog", "due": None}),
    )
    assert run["ok"] is True and run["got"] == "read_email > create_reminder"
    assert run["calls"][1]["args"] == {"title": "Walk the dog"}  # None dropped
    # A call that fails the tool's schema is reported, and never recorded.
    bad = _run_tool_case(case, ("read_email", {"id": 4201}))
    assert bad["ok"] is False and bad["step_errors"] and bad["calls"] == []


def test_event_case_prompt_carries_memories_and_defaults_to_no_action() -> None:
    from ai.mlx_engine import MLXEngine
    from ai.orchestrator import Orchestrator
    from events.event_bus import EventBus

    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    prompt, steps = tool_calls._prompt_and_steps(_case("event_file_memory_move"), orch)
    assert steps == 2
    assert "move every new PDF in Downloads" in prompt
    assert prompt.index("Relevant memories") < prompt.index("Most events need no action")
    plain, _ = tool_calls._prompt_and_steps(_case("event_file_no_action"), orch)
    assert "Relevant memories" not in plain
    assert "Never call a tool just to tell the user the event happened" in plain
