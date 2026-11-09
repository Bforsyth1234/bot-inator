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
