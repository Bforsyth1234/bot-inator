"""Tests for the Phase 5.1 draft → check → repair loop."""

from __future__ import annotations

import re

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools.draft_checks import check_draft, find_undefined_names

GOOD = '''"""Fire a notification after a delay."""
from __future__ import annotations
from smolagents import tool
from tools.show_notification import show_notification
import threading

_DEFAULT_TITLE = "Timer"


@tool
def alert_in(seconds: int, message: str) -> dict:
    """Schedule a notification.

    Args:
        seconds: Delay before it fires.
        message: Notification body.

    Returns:
        dict with status.
    """
    threading.Timer(seconds, lambda: show_notification(title=_DEFAULT_TITLE, message=message)).start()
    return {"status": "ok"}
'''


def test_good_draft_passes_including_subprocess_import() -> None:
    assert check_draft("alert_in", GOOD) == []


def test_syntax_error_reported_with_line() -> None:
    problems = check_draft("alert_in", "def broken(:\n", run_import=False)
    assert problems and problems[0].startswith("SyntaxError on line 1")


def test_missing_import_inside_thread_is_caught() -> None:
    bad = GOOD.replace("from tools.show_notification import show_notification\n", "")
    problems = check_draft("alert_in", bad, run_import=False)
    assert any("'show_notification'" in p for p in problems)


def test_top_level_code_is_rejected_before_import() -> None:
    bad = GOOD + "\nopen('/tmp/pwned', 'w').write('x')\n"
    problems = check_draft("alert_in", bad, run_import=False)
    assert any("not allowed at module level" in p for p in problems)


def test_computed_default_and_foreign_decorator_rejected() -> None:
    bad = GOOD.replace("message: str) -> dict", "message: str = str(1)) -> dict")
    bad = bad.replace("@tool\n", "@tool\n@staticmethod\n")
    problems = check_draft("alert_in", bad, run_import=False)
    assert any("default argument" in p for p in problems)
    assert any("bare @tool decorator" in p for p in problems)


def test_missing_type_hint_and_doc_arg() -> None:
    bad = GOOD.replace("message: str) -> dict", "message) -> dict").replace(
        "        message: Notification body.\n", ""
    )
    problems = check_draft("alert_in", bad, run_import=False)
    assert "parameter 'message' needs a type hint" in problems
    assert any("missing an entry for 'message'" in p for p in problems)


def test_static_validator_errors_are_returned() -> None:
    def validator(_name: str, _src: str) -> None:
        raise ValueError("disallowed import: requests")

    assert check_draft("alert_in", GOOD, static_validator=validator) == [
        "disallowed import: requests"
    ]


def test_import_failure_reported() -> None:
    bad = GOOD.replace(
        "from tools.show_notification import show_notification",
        "from tools.show_notification import no_such_thing as show_notification",
    )
    problems = check_draft("alert_in", bad)
    assert problems and "importing the module failed" in problems[0]
    assert "no_such_thing" in problems[0]


def test_undefined_names_ignores_builtins_locals_and_closures() -> None:
    src = (
        "import os\n"
        "def f(x):\n"
        "    y = [len(s) for s in os.listdir(x)]\n"
        "    def g():\n"
        "        return y\n"
        "    return g()\n"
    )
    assert find_undefined_names(src) == []


def test_repair_loop_feeds_problems_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg

    broken = GOOD.replace("from tools.show_notification import show_notification\n", "")
    calls: list[tuple[str, list]] = []

    def fake_draft(engine, name, desc, logic, previous_source="", problems=None):
        calls.append((previous_source, list(problems or [])))
        return broken if len(calls) == 1 else GOOD

    monkeypatch.setattr(mtg, "_draft_with_engine", fake_draft)
    monkeypatch.setattr(mtg, "critique", lambda *a: [])
    source, attempts = mtg._draft_until_clean(object(), "alert_in", "d", "l")

    assert source == GOOD and attempts == 2
    assert calls[0] == ("", [])
    assert calls[1][0] == broken
    assert any("show_notification" in p for p in calls[1][1])


def test_repair_loop_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg

    calls: list[int] = []

    def fake_draft(*_a, **_k):
        calls.append(1)
        return "def nope(:\n"

    monkeypatch.setattr(mtg, "_draft_with_engine", fake_draft)
    with pytest.raises(mtg.ToolGenerationError, match="after 3 attempts"):
        mtg._draft_until_clean(object(), "alert_in", "d", "l")
    assert len(calls) == mtg.MAX_DRAFT_ATTEMPTS == 3


def test_critic_problems_trigger_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg

    drafts: list[list[str]] = []
    verdicts = iter([["globs *.pdf: moves every PDF, not only invoices"], []])

    def fake_draft(engine, name, desc, logic, previous_source="", problems=None):
        drafts.append(list(problems or []))
        return GOOD

    monkeypatch.setattr(mtg, "_draft_with_engine", fake_draft)
    monkeypatch.setattr(mtg, "critique", lambda *a: next(verdicts))
    source, attempts = mtg._draft_until_clean(object(), "alert_in", "d", "l")

    assert attempts == 2 and source == GOOD
    assert drafts[1] == ["globs *.pdf: moves every PDF, not only invoices"]


def test_unresolved_critic_notes_reach_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg

    monkeypatch.setattr(mtg, "_draft_with_engine", lambda *a, **k: GOOD)
    monkeypatch.setattr(mtg, "critique", lambda *a: ["may move the same file twice"])
    source, attempts = mtg._draft_until_clean(object(), "alert_in", "d", "l")

    assert attempts == 3
    assert source.startswith("# REVIEW NOTES")
    assert "# - may move the same file twice" in source
    assert source.endswith(GOOD)
    # The annotated source must still pass the structural checks.
    assert check_draft("alert_in", source, run_import=False) == []


def test_structural_failure_on_last_attempt_falls_back_to_clean_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import meta_tool_generator as mtg

    drafts = iter([GOOD, "def nope(:\n", "def nope(:\n"])
    monkeypatch.setattr(mtg, "_draft_with_engine", lambda *a, **k: next(drafts))
    monkeypatch.setattr(mtg, "critique", lambda *a: ["off-by-one"])
    source, _ = mtg._draft_until_clean(object(), "alert_in", "d", "l")
    assert "# - off-by-one" in source and source.endswith(GOOD)


def test_critic_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg

    calls: list[int] = []
    monkeypatch.setenv("AGENT_DRAFT_CRITIC", "0")
    monkeypatch.setattr(mtg, "_draft_with_engine", lambda *a, **k: GOOD)
    monkeypatch.setattr(mtg, "critique", lambda *a: calls.append(1) or ["x"])
    _, attempts = mtg._draft_until_clean(object(), "alert_in", "d", "l")
    assert attempts == 1 and calls == []


SPEC = "description: File invoices.\nexpected_logic: Scan ~/Downloads for files matching inv*.pdf (case-insensitive)."
SRC = 'pattern = os.path.join(downloads_dir, "*.pdf")\ninvoice_files = glob.glob(pattern)'


def _item(spec_quote: str, code_quote: str, problem: str = "selects every PDF") -> dict:
    return {"spec_quote": spec_quote, "code_quote": code_quote, "problem": problem}


@pytest.mark.parametrize("problems,expected_count", [
    ([], 0),
    ([_item("files matching inv*.pdf", 'os.path.join(downloads_dir, "*.pdf")')], 1),
    # whitespace and case differences in quotes are tolerated
    ([_item("Files  matching INV*.pdf", 'os.path.join(downloads_dir,   "*.pdf")')], 1),
    # invented spec requirement -> dropped
    ([_item("handle permission errors", "glob.glob(pattern)")], 0),
    # code that is not in the draft -> dropped
    ([_item("files matching inv*.pdf", "fnmatch.fnmatch(name, pat)")], 0),
    # missing quotes -> dropped
    ([{"problem": "no error handling"}], 0),
    (["a bare string problem"], 0),
    ([_item("files matching inv*.pdf", "glob.glob(pattern)", f"p{i}") for i in range(9)], 5),
])
def test_parse_critique_keeps_only_grounded_problems(problems, expected_count: int) -> None:
    import json as _json

    from tools.draft_critic import parse_critique

    raw = "<think>x</think> " + _json.dumps({"problems": problems})
    kept = parse_critique(raw, spec=SPEC, source=SRC)
    assert len(kept) == expected_count
    if expected_count:
        assert "spec:" in kept[0] and "code:" in kept[0]


@pytest.mark.parametrize("raw", ["looks fine to me", "{not json}", '{"problems": "none"}'])
def test_parse_critique_unparseable_is_pass(raw: str) -> None:
    from tools.draft_critic import parse_critique

    assert parse_critique(raw, spec=SPEC, source=SRC) == []


def test_critique_swallows_engine_errors() -> None:
    from tools.draft_critic import critique

    class Broken:
        def generate_chat_sync(self, *a, **k):
            raise RuntimeError("metal OOM")

    assert critique(Broken(), "t", "d", "l", GOOD) == []


def test_drafter_prompt_uses_real_show_notification_arguments() -> None:
    """Regression: the prompt taught ``message=``; the real argument is ``body``."""
    import inspect

    from tools import meta_tool_generator as mtg
    from tools.show_notification import show_notification

    real = set(show_notification.inputs)
    assert real == {"title", "body"}
    assert "message=" not in mtg._SYSTEM_PROMPT_BASE.replace("body=message", "")
    assert 'show_notification(title="Done", body="...")' in mtg._SYSTEM_PROMPT_BASE
    context = mtg._format_available_tools_context()
    assert "call: show_notification(title: string, body: string)" in context
    del inspect


@pytest.mark.parametrize("snippet,banned", [
    ("pattern = re.compile(r'inv.*')", None),
    ("value = ast.literal_eval('[1]')", None),
    ("model.evaluate(x)", None),
    ("eval('1+1')", "eval("),
    ("exec('x=1')", "exec("),
    ("compile('x', 'f', 'exec')", "compile("),
    ("os.system('ls')", "os.system("),
    ("os.popen('ls')", "os.popen("),
    ("builtins.eval('1')", "builtins.eval("),
])
def test_banned_calls_matched_on_ast_not_substrings(snippet: str, banned) -> None:
    from tools import meta_tool_generator as mtg

    source = GOOD.replace('    return {"status": "ok"}', f"    {snippet}\n    return {{\"status\": \"ok\"}}")
    if banned is None:
        mtg._validate_source("alert_in", source)
    else:
        with pytest.raises(mtg.ToolGenerationError, match=re.escape(banned)):
            mtg._validate_source("alert_in", source)
