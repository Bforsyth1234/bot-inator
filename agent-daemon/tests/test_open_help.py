"""Tests for the open-ended help source: generate, filter, learn."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.open_help import (
    CONTENT_EVENT_TYPES,
    GUIDING_QUESTION,
    IDEA_JUDGE_SYSTEM_PROMPT,
    OPEN_HELP_SYSTEM_PROMPT,
    IdeaFeedback,
    OpenHelpSuggester,
    evaluate_windows,
    grounded_in_events,
    is_app_switcher,
    load_windows,
    parse_judge,
    parse_score,
    passes_checks,
    summarize,
)
from events.event_bus import ContextEvent, EventBus

IDEA = {
    "tool_name": "rename_screenshots_by_date",
    "description": "Rename new screenshots on the Desktop to their capture date.",
    "expected_logic": (
        "Input: folder (str, default ~/Desktop). For each file matching "
        "'Screenshot *.png', rename it to YYYY-MM-DD_HHMMSS.png. "
        "dry_run defaults to True and only lists the renames."
    ),
}


def _file(name: str) -> ContextEvent:
    return ContextEvent(
        event_type="file", metadata={"path": f"/Users/me/Desktop/{name}", "action": "created"}
    )


def _run(
    events: list[ContextEvent],
    *,
    reply: str = json.dumps(IDEA),
    score: str = '{"score": 9, "reason": "matches events"}',
    feedback: IdeaFeedback | None = None,
    **kwargs,
):
    published: list[ContextEvent] = []
    asks: list[str] = []
    judged: list[str] = []

    async def evaluate(text: str) -> str:
        asks.append(text)
        return reply

    async def judge(text: str) -> str:
        judged.append(text)
        return score

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario():
        s = OpenHelpSuggester(
            event_bus=EventBus(), evaluate=evaluate, judge=judge,
            feedback=feedback, publish=publish, **kwargs,
        )
        for e in events:
            task = s.observe(e)
            if task:
                await task
        return s

    suggester = asyncio.run(scenario())
    return published, asks, judged, suggester


def test_prompt_asks_the_guiding_question() -> None:
    assert GUIDING_QUESTION in OPEN_HELP_SYSTEM_PROMPT


def test_publishes_judged_idea_after_min_events() -> None:
    events = [_file(f"Screenshot {i}.png") for i in range(3)]
    published, asks, judged, _ = _run(events)
    assert len(asks) == 1 and len(judged) == 1
    assert "Screenshot 2.png" in asks[0]
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert meta["source"] == "open_help"
    assert meta["judge_score"] == 9


def test_waits_for_min_events() -> None:
    published, asks, _, _ = _run([_file("a.png"), _file("b.png")])
    assert asks == [] and published == []


def test_rate_limited_by_interval() -> None:
    events = [_file(f"{i}.png") for i in range(9)]
    _, asks, _, _ = _run(events, interval_seconds=600)
    assert len(asks) == 1
    _, asks, _, _ = _run(events, interval_seconds=0)
    assert len(asks) == 3


def test_ignores_agent_and_chat_events() -> None:
    events = [ContextEvent(event_type="pattern_detected", metadata=IDEA)] * 3
    events += [ContextEvent(event_type="user_message", metadata={"text": "hi"})] * 3
    _, asks, _, _ = _run(events)
    assert asks == []


def test_low_judge_score_is_dropped() -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, score='{"score": 4, "reason": "generic"}')
    assert judged and published == []


def test_unparseable_judge_reply_is_dropped() -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, _, _ = _run(events, score="sounds great!")
    assert published == []


def test_no_suggestion_skips_judge() -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    published, asks, judged, _ = _run(events, reply="NO_SUGGESTION")
    assert asks and judged == [] and published == []


def test_malformed_idea_skips_judge() -> None:
    bad = dict(IDEA, expected_logic="do it")
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, reply=json.dumps(bad))
    assert judged == [] and published == []


def test_same_idea_not_repeated() -> None:
    events = [_file(f"{i}.png") for i in range(6)]
    published, asks, _, _ = _run(events, interval_seconds=0)
    assert len(asks) == 2 and len(published) == 1


def test_rejected_idea_is_suppressed_and_history_reaches_judge() -> None:
    fb = IdeaFeedback()
    fb.record("rename_screenshots_by_date", IDEA["description"], approved=False)
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, feedback=fb)
    assert published == [] and judged == []

    fb = IdeaFeedback()
    fb.record("open_zoom_links", "Open Zoom links from iMessage", approved=True)
    published, _, judged, _ = _run(events, feedback=fb)
    assert len(published) == 1
    assert "approved: open_zoom_links" in judged[0]


def test_record_feedback_persists(tmp_path: Path) -> None:
    path = tmp_path / "fb.json"
    _, _, _, s = _run([], feedback=IdeaFeedback(path))
    s.record_feedback("tidy_downloads", "Sort Downloads by type", approved=False)
    reloaded = IdeaFeedback(path)
    assert reloaded.resembles_rejected("tidy_downloads", "")
    assert reloaded.resembles_rejected("sort_downloads", "Sort Downloads by file type")
    assert not reloaded.resembles_rejected("add_ics_invite", "Add calendar invite")


def test_parse_score() -> None:
    assert parse_score('{"score": 8, "reason": "x"}') == 8
    assert parse_score('Sure: {"score": 12}') == 10
    assert parse_score("score: 3") == 3
    assert parse_score("") is None


def test_passes_checks() -> None:
    assert passes_checks(IDEA)
    assert not passes_checks(dict(IDEA, tool_name="Bad Name"))
    assert not passes_checks(dict(IDEA, description=""))


def test_orchestrator_routes_feedback_to_sources() -> None:
    from ai.orchestrator import Orchestrator

    calls: list[tuple] = []

    class Source:
        def record_feedback(self, name, desc, approved):
            calls.append((name, desc, approved))

    orch = Orchestrator.__new__(Orchestrator)
    orch.idea_sources = [Source(), object()]
    orch._record_idea_feedback({"kwargs": {"tool_name": "t", "description": "d"}}, False)
    orch._record_idea_feedback({"args": ["u", "e"], "kwargs": {}}, True)
    orch._record_idea_feedback({"kwargs": {}}, True)
    assert calls == [("t", "d", False), ("u", "e", True)]


def test_pattern_prompt_has_open_help_intro() -> None:
    from ai.orchestrator import Orchestrator

    prompt = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata={**IDEA, "source": "open_help"})
    )
    assert "how it could help" in prompt


# ---- #36: score logging, grounding check, evaluation windows ----------


def test_pass_logs_score_and_reason(caplog: pytest.LogCaptureFixture) -> None:
    events = [_file(f"Screenshot {i}.png") for i in range(3)]
    with caplog.at_level(logging.INFO, logger="ai.open_help"):
        published, _, _, _ = _run(events)
    assert published[0].metadata["judge_reason"] == "matches events"
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "passed 'rename_screenshots_by_date'" in m and "score 9" in m and "matches events" in m
        for m in lines
    ), lines


def test_drop_logs_score_and_reason(caplog: pytest.LogCaptureFixture) -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    with caplog.at_level(logging.INFO, logger="ai.open_help"):
        _run(events, score='{"score": 4, "reason": "only files appeared"}')
    assert any(
        "dropped 'rename_screenshots_by_date'" in r.getMessage()
        and "score 4" in r.getMessage() and "only files appeared" in r.getMessage()
        for r in caplog.records
    )


def test_parse_judge() -> None:
    assert parse_judge('{"score": 8, "reason": "renames by hand"}') == (8, "renames by hand")
    assert parse_judge("score: 3") == (3, "")
    assert parse_judge("nope") == (None, "")


def test_judge_prompt_is_strict_about_incidental_files() -> None:
    assert "test, sample" in IDEA_JUDGE_SYSTEM_PROMPT
    assert "at most 4" in IDEA_JUDGE_SYSTEM_PROMPT
    assert '{"score": <integer 0-10>' in IDEA_JUDGE_SYSTEM_PROMPT


def test_grounded_in_events() -> None:
    text = "Recent events (oldest first):\n- file created: /Users/me/Downloads/test_invoice_1.txt"
    assert grounded_in_events({"tool_name": "invoices_to_pdf", "description": "x"}, text)
    assert grounded_in_events({"tool_name": "x_tool", "description": "Read each .txt file"}, text)
    assert grounded_in_events({"tool_name": "x_tool", "description": "Sort my Downloads"}, text)
    # Words from the prompt header and event verbs do not count.
    assert not grounded_in_events(
        {"tool_name": "recent_events_digest", "description": "Show what was created first"}, text
    )
    assert not grounded_in_events(
        {"tool_name": "hydration_reminder", "description": "Remind me to drink water"}, text
    )


def test_ungrounded_idea_skips_judge() -> None:
    idea = dict(IDEA, tool_name="hydration_reminder", description="Remind me to drink water.",
                expected_logic="Input: interval_minutes (int). Show a notification with "
                "osascript every interval until stopped; error if under 5.")
    events = [_file(f"Screenshot {i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, reply=json.dumps(idea))
    assert judged == [] and published == []


def _windows_with(score: int):
    async def judge(_: str) -> str:
        return json.dumps({"score": score, "reason": "fake"})

    return asyncio.run(evaluate_windows(load_windows(), judge=judge))


def test_evaluation_windows_fixture() -> None:
    cases = load_windows()
    ids = {c["id"] for c in cases}
    assert len(cases) >= 5
    assert {"live_invoice_template", "live_csv_merge", "live_csv_diff"} <= ids
    assert {c["expected"] for c in cases} == {"pass", "drop"}
    for case in cases:
        # Every recorded idea is well formed; the only ones the
        # deterministic checks may refuse are the app switchers (#66).
        assert passes_checks(case["idea"]) or is_app_switcher(case["idea"]), case["id"]


def test_good_windows_survive_deterministic_checks() -> None:
    # With a judge that passes everything, only the deterministic stages
    # can drop an idea: every expected-pass window must still pass.
    results = {r["id"]: r for r in _windows_with(10)}
    for case in load_windows():
        if case["expected"] == "pass":
            assert results[case["id"]]["got"] == "pass", results[case["id"]]
    assert results["unrelated_water_reminder"]["stage"] == "grounding"
    assert results["live_csv_merge"]["stage"] == "passed"


def test_evaluation_reports_pass_rate() -> None:
    results = _windows_with(2)
    assert all(r["got"] == "drop" for r in results)
    report = summarize(results)
    assert f"pass rate 0/{len(results)}" in report


# ---- #66: content grounding, no app switchers -------------------------

SWITCHER = {
    "tool_name": "toggle_chrome_claude",
    "description": "Switches focus between Google Chrome and Claude with a single hotkey.",
    "expected_logic": (
        "Input: none. Read the frontmost app with osascript; if it is Google "
        "Chrome activate Claude, otherwise activate Google Chrome."
    ),
}


def _app(name: str, title: str | None = None) -> ContextEvent:
    return ContextEvent(event_type="app_activated", app_name=name, window_title=title)


def test_app_only_window_makes_no_model_call() -> None:
    # Six app switches, far past min_events, with an idea that names both
    # apps: before #66 this asked the model, judged and published.
    events = [_app("Google Chrome"), _app("Claude")] * 3
    published, asks, judged, s = _run(events, reply=json.dumps(SWITCHER), interval_seconds=0)
    assert asks == [] and judged == [] and published == []
    assert s.last_verdict["stage"] == "no_content"


def test_clipboard_and_window_titles_are_not_content() -> None:
    assert "app_activated" not in CONTENT_EVENT_TYPES
    assert "clipboard_changed" not in CONTENT_EVENT_TYPES
    events = [
        _app("Numbers", "Q3 budget.numbers"),
        ContextEvent(event_type="clipboard_changed", app_name="Numbers",
                     metadata={"app": "Numbers", "kind": "text"}),
        _app("Mail", "Q3 budget"),
    ] * 2
    idea = dict(IDEA, tool_name="budget_to_mail", description="Paste the Q3 budget into Mail.")
    published, asks, _, _ = _run(events, reply=json.dumps(idea), interval_seconds=0)
    assert asks == [] and published == []


def test_content_event_after_app_switches_asks_at_once() -> None:
    # The app-only stretch must not swallow the trigger: the first content
    # event asks, and the idea naming that file passes.
    events = [_app("Finder"), _app("Preview"), _app("Finder"), _file("Screenshot 1.png")]
    published, asks, judged, _ = _run(events)
    assert len(asks) == 1 and len(judged) == 1 and len(published) == 1
    assert "switched to Finder" in asks[0] and "Screenshot 1.png" in asks[0]


def test_no_second_call_until_new_content() -> None:
    files = [_file(f"Screenshot {i}.png") for i in range(3)]
    switches = [_app("Google Chrome"), _app("Claude")] * 3
    _, asks, _, _ = _run(files + switches, interval_seconds=0)
    assert len(asks) == 1
    _, asks, _, _ = _run(files + switches + [_file("Screenshot 9.png")], interval_seconds=0)
    assert len(asks) == 2


def test_app_switcher_idea_dropped_before_judge() -> None:
    # Files are in the window, so the model is asked; its app-switcher
    # answer never reaches the judge.
    events = [_file("report 1.csv"), _app("Google Chrome"), _app("Claude")]
    published, asks, judged, s = _run(events, reply=json.dumps(SWITCHER))
    assert len(asks) == 1 and judged == [] and published == []
    assert s.last_verdict["stage"] == "app_switcher"


def test_idea_naming_a_file_event_still_passes_among_app_switches() -> None:
    events = [_app("Finder"), _file("Screenshot 1.png"), _app("Preview"),
              _file("Screenshot 2.png"), _app("Finder"), _file("Screenshot 3.png")]
    published, _, judged, s = _run(events)
    assert len(judged) == 1 and len(published) == 1
    assert published[0].metadata["tool_name"] == "rename_screenshots_by_date"
    assert s.last_verdict["stage"] == "passed"


def test_app_name_alone_does_not_ground_an_idea() -> None:
    text = (
        "Recent events (oldest first):\n"
        "- switched to Google Chrome — GitHub\n"
        "- file modified: /Users/me/dev/shop/src/cart.py\n"
        "- switched to Claude"
    )
    bookmarks = {"tool_name": "chrome_bookmark_exporter",
                 "description": "Export Google Chrome bookmarks to an HTML page."}
    assert not grounded_in_events(bookmarks, text)
    assert not grounded_in_events({"tool_name": "github_opener", "description": "x"}, text)
    assert grounded_in_events({"tool_name": "run_cart_tests", "description": "x"}, text)
    # Mail and calendar events ground through their subject and title,
    # not through their metadata keys.
    mail = '- mail_received: {"sender": "a@b.c", "subject": "Invoice 1042 overdue", "bulk": false}'
    assert grounded_in_events({"tool_name": "file_invoice", "description": "x"}, mail)
    assert not grounded_in_events({"tool_name": "subject_sorter", "description": "Sort by date"}, mail)


def test_is_app_switcher() -> None:
    def idea(name: str, desc: str) -> dict:
        return {"tool_name": name, "description": desc}

    apps = ("Google Chrome", "Claude", "Microsoft Teams", "System Settings")
    # The real ideas from 2026-11-10 (idea_feedback.json).
    for name, desc in [
        ("app_switcher", "Quickly switch between frequently used apps"),
        ("recently_used_apps", "Show a list of the 5 most recently used apps"),
        ("toggle_chrome_claude", "Switches focus between Google Chrome and Claude with a single hotkey."),
        ("chrome_teams_switcher", "Toggle focus between Google Chrome and Microsoft Teams."),
        ("switch_between_chrome_and_claude",
         "Automatically activates Claude after Chrome becomes the frontmost app."),
        ("system_settings_switcher",
         "Switch between System Settings and Claude by typing the first letters of each name."),
        ("count_app_switches",
         'Counts occurrences of each app in recent "switched to" events and returns a sorted frequency list.'),
        ("summarize_app_switches",
         "Parse activity logs to count app switches and extract received iMessage texts."),
    ]:
        assert is_app_switcher(idea(name, desc), apps), name
        assert not passes_checks(dict(IDEA, tool_name=name, description=desc), apps), name
    # The name alone is enough, and so is the description alone.
    assert is_app_switcher(idea("count_app_switches", "x"))
    assert is_app_switcher(idea("workflow_helper", "Quickly switch between frequently used apps"))
    assert is_app_switcher(idea("toggle_chrome_claude", "x"), apps)
    # Not app switchers.
    for name, desc in [
        (IDEA["tool_name"], IDEA["description"]),
        ("toggle_dark_mode", "Toggle dark mode on a schedule."),
        ("list_downloads", "List the newest files in Downloads."),
        ("count_words", "Count the words in a text file."),
        ("convert_heic_to_jpeg", "Convert HEIC photos to JPEG next to the original, without opening Preview."),
        ("merge_csv_exports", "Merge CSV exports so you stop switching between Numbers and Mail."),
        ("mail_totals_to_numbers",
         "Copy invoice totals from selected Mail messages into the open Numbers sheet."),
    ]:
        assert not is_app_switcher(idea(name, desc), ("Mail", "Numbers", "Preview")), name


def test_prompts_rule_out_app_switchers() -> None:
    assert "Do not propose app switchers" in OPEN_HELP_SYSTEM_PROMPT
    assert "switches, toggles, focuses, lists or counts apps" in IDEA_JUDGE_SYSTEM_PROMPT
    assert "always scores 0-3" in IDEA_JUDGE_SYSTEM_PROMPT


def test_fixture_app_switch_windows_drop_at_the_expected_stage() -> None:
    # Judge passes everything: only the deterministic stages can drop.
    cases = {c["id"]: c for c in load_windows()}
    results = {r["id"]: r for r in _windows_with(10)}
    staged = {i: c["stage"] for i, c in cases.items() if "stage" in c}
    assert {"no_content", "app_switcher", "grounding"} <= set(staged.values())
    assert sum(s == "no_content" for s in staged.values()) >= 3
    for case_id, stage in staged.items():
        assert results[case_id]["got"] == "drop", results[case_id]
        assert results[case_id]["stage"] == stage, results[case_id]
    # App-only windows hold no content event at all.
    for case_id, stage in staged.items():
        types = {e["event_type"] for e in cases[case_id]["events"]}
        assert (stage == "no_content") == (not types & CONTENT_EVENT_TYPES), case_id
