"""Groq agent model: plain-text replies become the final answer (live check).

Groq enforces smolagents' tool_choice="required" and returned 400 when the
model answered in text after a tool result, so chat turns failed.
"""

from __future__ import annotations

from typing import Any

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.orchestrator import _make_groq_model_class
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
)


class _FakeBase:
    def __init__(self, reply: ChatMessage) -> None:
        self.reply = reply
        self.kwargs: dict[str, Any] = {}

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        self.kwargs = kwargs
        return self.reply


def _model(reply: ChatMessage) -> Any:
    return _make_groq_model_class(_FakeBase)(reply)


def test_text_reply_becomes_final_answer_and_choice_is_auto() -> None:
    m = _model(ChatMessage(role="assistant", content="You have 3 shortcuts."))
    out = m.generate([], tools_to_call_from=[object()])
    assert m.kwargs["tool_choice"] == "auto"
    assert out.tool_calls[0].function.name == "final_answer"
    assert out.tool_calls[0].function.arguments == {"answer": "You have 3 shortcuts."}


def test_real_tool_call_is_untouched() -> None:
    call = ChatMessageToolCall(
        id="c1", type="function",
        function=ChatMessageToolCallFunction(name="list_shortcuts", arguments={}),
    )
    m = _model(ChatMessage(role="assistant", content="", tool_calls=[call]))
    out = m.generate([], tools_to_call_from=[object()])
    assert out.tool_calls == [call]


def test_no_tools_means_no_rewrite() -> None:
    m = _model(ChatMessage(role="assistant", content="plain"))
    out = m.generate([], tools_to_call_from=None)
    assert not out.tool_calls
    assert "tool_choice" not in m.kwargs


def test_optional_params_become_nullable() -> None:
    from ai.orchestrator import _allow_null_optionals

    spec = {"function": {"parameters": {
        "properties": {"title": {"type": "string"}, "due": {"type": "string"},
                       "tags": {"type": ["array"]}},
        "required": ["title"],
    }}}
    _allow_null_optionals(spec)
    props = spec["function"]["parameters"]["properties"]
    assert props["title"]["type"] == "string"
    assert props["due"]["type"] == ["string", "null"]
    assert props["tags"]["type"] == ["array", "null"]


class _FlakyBase(_FakeBase):
    def __init__(self, reply: ChatMessage, failures: int, message: str) -> None:
        super().__init__(reply)
        self.failures = failures
        self.message = message
        self.calls = 0

    def generate(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(self.message)
        return super().generate(*args, **kwargs)


def test_tool_call_rejection_is_retried_once() -> None:
    import pytest

    msg = "Error code: 400 - Failed to parse tool call arguments as JSON"
    ok = _make_groq_model_class(_FlakyBase)(ChatMessage(role="assistant", content="hi"), 1, msg)
    assert ok.generate([], tools_to_call_from=[object()]).tool_calls
    assert ok.calls == 2
    bad = _make_groq_model_class(_FlakyBase)(ChatMessage(role="assistant", content="hi"), 2, msg)
    with pytest.raises(RuntimeError):
        bad.generate([], tools_to_call_from=[object()])
    assert bad.calls == 2
    other = _make_groq_model_class(_FlakyBase)(ChatMessage(role="assistant", content="hi"), 1, "Error code: 401")
    with pytest.raises(RuntimeError):
        other.generate([], tools_to_call_from=[object()])
    assert other.calls == 1


def test_empty_reply_after_tool_result_ends_the_turn() -> None:
    m = _model(ChatMessage(role="assistant", content=""))
    history = [{"role": "user", "content": "open x"}, {"role": "tool-response", "content": "ok"}]
    out = m.generate(history, tools_to_call_from=[object()])
    assert out.tool_calls[0].function.name == "final_answer"
    assert out.tool_calls[0].function.arguments == {"answer": "Done."}


class _TwoReplies(_FakeBase):
    """Empty reply with tools offered; a real answer when asked without tools."""

    def __init__(self, reply: ChatMessage) -> None:
        super().__init__(reply)
        self.tool_free_calls = 0

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        if tools_to_call_from is None:
            self.tool_free_calls += 1
            assert "tool_choice" not in kwargs
            return ChatMessage(role="assistant", content="You have no events today.")
        return super().generate(messages, stop_sequences, response_format,
                                tools_to_call_from, **kwargs)


def test_empty_reply_gets_one_tool_free_answer_attempt() -> None:
    """Live check: "what's on my calendar today?" was answered "Done."."""
    m = _make_groq_model_class(_TwoReplies)(ChatMessage(role="assistant", content=""))
    history = [{"role": "user", "content": "calendar?"}, {"role": "tool-response", "content": "[]"}]
    out = m.generate(history, tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": "You have no events today."}
    assert m.tool_free_calls == 1


def test_empty_reply_before_any_tool_is_left_alone() -> None:
    m = _model(ChatMessage(role="assistant", content=""))
    out = m.generate([{"role": "user", "content": "hi"}], tools_to_call_from=[object()])
    assert not out.tool_calls


def test_answer_alias_is_offered_and_mapped_back() -> None:
    """Live check: Groq rejected turns where GPT-OSS called a tool 'answer'."""
    from ai.orchestrator import ANSWER_ALIAS, _answer_alias

    final = {"type": "function", "function": {"name": "final_answer", "description": "d",
             "parameters": {"properties": {"answer": {"type": "string"}}, "required": ["answer"]}}}
    other = {"type": "function", "function": {"name": "list_events", "parameters": {}}}
    alias = _answer_alias([other, final])
    assert alias["function"]["name"] == ANSWER_ALIAS
    assert alias["function"]["parameters"] == final["function"]["parameters"]
    assert final["function"]["name"] == "final_answer"  # original untouched
    assert _answer_alias([other]) is None            # no final_answer offered
    assert _answer_alias([final, alias]) is None      # already present

    call = ChatMessageToolCall(
        id="c1", type="function",
        function=ChatMessageToolCallFunction(name="answer", arguments={"answer": "hi"}),
    )
    m = _model(ChatMessage(role="assistant", content="", tool_calls=[call]))
    out = m.generate([], tools_to_call_from=[object()])
    assert out.tool_calls[0].function.name == "final_answer"
    assert out.tool_calls[0].function.arguments == {"answer": "hi"}


def test_bare_done_after_tool_result_is_replaced_by_the_real_result() -> None:
    """Live check: 'open Numbers' failed but the reply was just 'Done.'."""
    call = ChatMessageToolCall(
        id="c1", type="function",
        function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": "Done."}),
    )
    m = _make_groq_model_class(_TwoReplies)(ChatMessage(role="assistant", content="", tool_calls=[call]))
    history = [{"role": "user", "content": "open Numbers"}, {"role": "tool-response", "content": "error"}]
    out = m.generate(history, tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": "You have no events today."}
    assert m.tool_free_calls == 1


def test_real_final_answer_is_left_alone() -> None:
    call = ChatMessageToolCall(
        id="c1", type="function",
        function=ChatMessageToolCallFunction(name="final_answer", arguments={"answer": "391"}),
    )
    m = _make_groq_model_class(_TwoReplies)(ChatMessage(role="assistant", content="", tool_calls=[call]))
    out = m.generate([{"role": "tool-response", "content": "x"}], tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": "391"}
    assert m.tool_free_calls == 0


def test_chat_prompt_forbids_bare_done() -> None:
    from ai.orchestrator import Orchestrator
    from events.event_bus import ContextEvent

    prompt = Orchestrator._build_chat_prompt(
        ContextEvent(event_type="user_message", metadata={"text": "open Numbers"})
    )
    assert "Never answer with just 'Done.'" in prompt


def test_bare_done_as_json_string_arguments_is_caught() -> None:
    """The API returns tool-call arguments as a JSON string."""
    from ai.orchestrator import _is_bare_done

    assert _is_bare_done('{"answer": "Done."}')
    assert _is_bare_done({"answer": "done"})
    assert not _is_bare_done('{"answer": "391"}')
    call = ChatMessageToolCall(
        id="c1", type="function",
        function=ChatMessageToolCallFunction(name="final_answer", arguments='{"answer": "Done."}'),
    )
    m = _make_groq_model_class(_TwoReplies)(ChatMessage(role="assistant", content="", tool_calls=[call]))
    out = m.generate([{"role": "tool-response", "content": "error"}], tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": "You have no events today."}


def test_fallback_reports_the_last_tool_error() -> None:
    """Live check: GPT-OSS wrote nothing after open_app failed."""
    from ai.orchestrator import _fallback_from_tool_result

    msgs = [{"role": "user", "content": "open Numbers"},
            {"role": "tool-response", "content": [{"type": "text", "text":
                "{'name': 'Numbers', 'status': 'error', 'error': 'no app with that name in /Applications'}"}]}]
    assert _fallback_from_tool_result(msgs) == "That didn't work: no app with that name in /Applications"
    ok = [{"role": "tool-response", "content": "{'status': 'ok'}"}]
    assert _fallback_from_tool_result(ok) == "Done."


class _EmptyWithStops(_FakeBase):
    """Empty when stop sequences are sent; a real answer without them."""

    def __init__(self, reply: ChatMessage) -> None:
        super().__init__(reply)
        self.stops_seen: list = []

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        if tools_to_call_from is None:
            self.stops_seen.append(stop_sequences)
            text = "" if stop_sequences else "Numbers isn't installed."
            return ChatMessage(role="assistant", content=text)
        return super().generate(messages, stop_sequences, response_format,
                                tools_to_call_from, **kwargs)


def test_reask_retries_without_stop_sequences() -> None:
    m = _make_groq_model_class(_EmptyWithStops)(ChatMessage(role="assistant", content=""))
    out = m.generate([{"role": "tool-response", "content": "x"}],
                     stop_sequences=["Observation:"], tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": "Numbers isn't installed."}
    assert m.stops_seen == [["Observation:"], None]


# --- junk arguments (found by evals/tool_calls on 2026-11-10) ---------------

class _Tool:
    def __init__(self, name: str, inputs: dict) -> None:
        self.name, self.inputs = name, inputs


_LIST_WINDOWS = _Tool("list_windows", {})
_LIST_EVENTS = _Tool("list_events", {"start": {"type": "string"}, "end": {"type": "string"}})
_GENERATE = _Tool("generate_custom_tool", {"tool_name": {}, "description": {}, "expected_logic": {}})


def _cleaned(name: str, arguments: Any) -> Any:
    call = ChatMessageToolCall(
        id="c1", type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=arguments),
    )
    m = _model(ChatMessage(role="assistant", content="", tool_calls=[call]))
    out = m.generate([], tools_to_call_from=[_LIST_WINDOWS, _LIST_EVENTS, _GENERATE])
    return out.tool_calls[0].function.arguments


def test_junk_arguments_to_a_no_argument_tool_are_dropped() -> None:
    # Each of these made smolagents reject list_windows until steps ran out.
    assert _cleaned("list_windows", '{"": ""}') == {}
    assert _cleaned("list_windows", {"end": None}) == {}
    assert _cleaned("list_windows", '{"arguments": {}}') == {}
    assert _cleaned("list_windows", "") == {}


def test_wrapped_and_empty_arguments_are_cleaned() -> None:
    real = {"tool_name": "start_timer", "description": "d", "expected_logic": "l"}
    assert _cleaned("generate_custom_tool", {"arguments": real}) == real
    assert _cleaned("generate_custom_tool", {"arguments": '{"tool_name": "t"}'}) == {"tool_name": "t"}
    assert _cleaned("list_events", '{"start": "2026-12-01", "": "", "calendar": null}') == {"start": "2026-12-01"}
    # A known parameter keeps even an empty value.
    assert _cleaned("list_events", {"start": "", "end": None}) == {"start": "", "end": None}


def test_unknown_argument_with_a_value_is_kept_for_the_model_to_see() -> None:
    assert _cleaned("list_events", {"day": "today"}) == {"day": "today"}
    # Unparseable arguments and unknown tools are left as they were.
    assert _cleaned("list_events", "{not json") == "{not json"
    assert _cleaned("no_such_tool", {"": 1}) == {"": 1}
