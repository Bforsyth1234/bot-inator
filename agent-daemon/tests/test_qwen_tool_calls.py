"""Qwen3.5+ output handling: XML tool calls and template-opened think blocks."""
from ai.mlx_engine import THINK_BLOCK_RE
from ai.orchestrator import _make_mlx_model_class, _parse_xml_tool_call


def test_parse_xml_tool_call_decodes_json_values() -> None:
    body = (
        "<function=send_email>\n"
        "<parameter=to>\nsam@example.org\n</parameter>\n"
        "<parameter=count>\n3\n</parameter>\n"
        '<parameter=tags>\n["a", "b"]\n</parameter>\n'
        "</function>"
    )
    assert _parse_xml_tool_call(body) == {
        "name": "send_email",
        "arguments": {"to": "sam@example.org", "count": 3, "tags": ["a", "b"]},
    }


def test_parse_xml_tool_call_keeps_multiline_strings() -> None:
    body = "<function=note>\n<parameter=text>\nline one\nline two\n</parameter>\n</function>"
    assert _parse_xml_tool_call(body)["arguments"] == {"text": "line one\nline two"}


def test_parse_xml_tool_call_rejects_non_xml() -> None:
    assert _parse_xml_tool_call("not a call") is None


def test_extract_tool_calls_handles_xml_and_json() -> None:
    model_cls = _make_mlx_model_class()
    text = (
        "Sure.\n<tool_call>\n<function=final_answer>\n<parameter=answer>\n"
        "done\n</parameter>\n</function>\n</tool_call>\n"
        '<tool_call>{"name": "ping", "arguments": {}}</tool_call>'
    )
    remaining, calls = model_cls._extract_tool_calls(text)
    assert remaining == "Sure."
    assert [(c.function.name, c.function.arguments) for c in calls] == [
        ("final_answer", {"answer": "done"}),
        ("ping", {}),
    ]


def test_think_regex_strips_full_and_orphan_blocks() -> None:
    assert THINK_BLOCK_RE.sub("", "<think>hm</think>answer").strip() == "answer"
    # The template opened <think> in the prompt, so only </think> is output.
    assert THINK_BLOCK_RE.sub("", "reasoning\n</think>\n\nanswer").strip() == "answer"
    assert THINK_BLOCK_RE.sub("", "plain answer") == "plain answer"
