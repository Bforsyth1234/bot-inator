"""Tests for the sandboxed dry run of drafted tools (Phase 5.1)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools import draft_dry_run as dr
from tools.draft_dry_run import DryRunCall, DryRunPlan, parse_plan, run_dry_run

needs_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="macOS sandbox-exec required",
)

HEADER = '''"""t."""
from __future__ import annotations
from smolagents import tool
import glob
import os
import shutil
import subprocess
import threading
import urllib.request
'''

# The real draft Qwen2.5-Coder-14B produced: a file matching both patterns
# is moved twice, so the second shutil.move raises.
DOUBLE_MOVE = HEADER + '''

@tool
def file_invoices() -> dict:
    """Move invoices.

    Returns:
        dict.
    """
    src = os.path.expanduser("~/Downloads")
    dst = os.path.expanduser("~/Documents/Invoices")
    os.makedirs(dst, exist_ok=True)
    files = glob.glob(os.path.join(src, "inv*.pdf"))
    files.extend(glob.glob(os.path.join(src, "*invoice*.pdf")))
    moved = []
    for f in files:
        target = os.path.join(dst, os.path.basename(f))
        shutil.move(f, target)
        moved.append(target)
    return {"status": "ok", "moved": moved}
'''
FIXED = DOUBLE_MOVE.replace(
    'files.extend(glob.glob(os.path.join(src, "*invoice*.pdf")))',
    'files = sorted(set(files) | set(glob.glob(os.path.join(src, "*invoice*.pdf"))))',
)
INVOICE_PLAN = DryRunPlan(
    files={"Downloads/invoice1.pdf": "x", "Downloads/inv1041.pdf": "x", "Downloads/notes.pdf": "x"},
    calls=[DryRunCall(
        kwargs={}, expect_status="ok",
        expect_exists=["Documents/Invoices/invoice1.pdf", "Documents/Invoices/inv1041.pdf", "Downloads/notes.pdf"],
        expect_missing=["Downloads/invoice1.pdf"],
    )],
)


@pytest.fixture(autouse=True)
def _fresh_sandbox_cache():
    dr.remove_sandbox_cache()
    yield
    dr.remove_sandbox_cache()


def _tool(body: str, name: str = "t", params: str = "") -> str:
    return HEADER + f'''

@tool
def {name}({params}) -> dict:
    """t.

    Returns:
        dict.
    """
{body}
'''


def test_parse_plan_drops_unsafe_paths_and_caps_sizes() -> None:
    raw = "<think>x</think>" + json.dumps({
        "files": {
            "Downloads/a.pdf": "a",
            "~/Downloads/b.pdf": "b" * 10_000,
            "/etc/passwd": "x",
            "../escape.txt": "x",
            **{f"f{i}.txt": "" for i in range(40)},
        },
        "calls": [{"kwargs": {"dry_run": True}, "expect_status": "maybe",
                   "expect_exists": ["Downloads/a.pdf", "/abs"], "expect_missing": ["../x"]}] * 5,
    })
    plan = parse_plan(raw)
    assert plan is not None
    assert "Downloads/a.pdf" in plan.files and "Downloads/b.pdf" in plan.files
    assert len(plan.files["Downloads/b.pdf"]) == dr.MAX_FIXTURE_BYTES
    assert not any(k.startswith("/") or ".." in k for k in plan.files)
    assert len(plan.files) <= dr.MAX_FIXTURE_FILES
    assert len(plan.calls) == dr.MAX_CALLS
    assert plan.calls[0].expect_status is None
    assert plan.calls[0].expect_exists == ["Downloads/a.pdf"]
    assert plan.calls[0].expect_missing == []


def test_parse_plan_keeps_exists_checks_only_for_fixtures() -> None:
    plan = parse_plan(json.dumps({
        "files": {"Downloads/inv1.pdf": "", "Downloads/notes.pdf": ""},
        "calls": [{"kwargs": {}, "expect_exists": ["Downloads/notes.pdf", "Documents/Invoices/inv1_1.pdf"],
                   "expect_missing": ["Downloads/inv1.pdf"]}],
    }))
    assert plan.calls[0].expect_exists == ["Downloads/notes.pdf"]
    assert plan.calls[0].expect_missing == ["Downloads/inv1.pdf"]


@pytest.mark.parametrize("raw", ["no json", '{"files": {}}', '{"calls": "x"}', "{bad"])
def test_parse_plan_rejects_unusable(raw: str) -> None:
    assert parse_plan(raw) is None


@needs_sandbox
def test_dry_run_catches_double_move() -> None:
    problems = run_dry_run("file_invoices", DOUBLE_MOVE, INVOICE_PLAN).all
    assert any("raised FileNotFoundError" in p or "No such file" in p for p in problems), problems


@needs_sandbox
def test_dry_run_passes_fixed_version() -> None:
    assert run_dry_run("file_invoices", FIXED, INVOICE_PLAN).all == []


@needs_sandbox
def test_dry_run_reports_wrong_file_effects() -> None:
    plan = DryRunPlan(files=INVOICE_PLAN.files, calls=[DryRunCall(
        kwargs={}, expect_exists=["Documents/Invoices/missing.pdf"],
    )])
    problems = run_dry_run("file_invoices", FIXED, plan).all
    assert any("missing.pdf to exist" in p for p in problems)
    assert any("files after the call" in p for p in problems)


@needs_sandbox
def test_sandbox_blocks_escape_and_stubs_side_effects(tmp_path: Path) -> None:
    escape = Path.home() / "bot-inator-dry-run-escape.txt"
    body = f'''    results = {{}}
    try:
        open({str(escape)!r}, "w").write("x")
        results["real_home_write"] = "allowed"
    except OSError:
        results["real_home_write"] = "blocked"
    open(os.path.expanduser("~/inside.txt"), "w").write("x")
    subprocess.run(["osascript", "-e", "display notification \\"hi\\""])
    try:
        urllib.request.urlopen("https://example.org")
    except Exception as exc:
        results["network"] = type(exc).__name__
    results["home"] = os.path.expanduser("~")
    results["secret"] = os.environ.get("GROQ_API_KEY", "")
    return {{"status": "ok", **results}}'''
    os.environ["GROQ_API_KEY"] = "sk-test-not-real"
    try:
        out, after = dr._execute("t", _tool(body), {}, {})
    finally:
        os.environ.pop("GROQ_API_KEY", None)
    result = out["result"]
    assert result["real_home_write"] == "blocked"
    assert not escape.exists()
    assert result["network"] == "URLError"
    assert out["network"] == ["https://example.org"]
    assert out["subprocess"][0][0] == "osascript"
    assert "inside.txt" in after
    assert result["home"] != str(Path.home())
    assert result["secret"] == ""


@needs_sandbox
def test_dry_run_reports_thread_errors_and_bad_returns() -> None:
    body = '''    threading.Thread(target=lambda: show_notification("x")).start()
    return "done"'''
    problems = run_dry_run("t", _tool(body), DryRunPlan(files={}, calls=[DryRunCall()])).all
    assert any("background thread raised NameError" in p for p in problems)
    assert any("instead of a dict with a 'status' key" in p for p in problems)


@needs_sandbox
def test_status_mismatch_ignored_when_network_was_needed() -> None:
    body = '''    try:
        urllib.request.urlopen("https://api.weatherapi.com/v1/current.json")
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok"}'''
    plan = DryRunPlan(files={}, calls=[DryRunCall(expect_status="ok")])
    assert run_dry_run("t", _tool(body), plan).all == []


@needs_sandbox
def test_status_mismatch_reported_without_network() -> None:
    body = '''    return {"status": "error", "message": "nothing to do"}'''
    plan = DryRunPlan(files={}, calls=[DryRunCall(expect_status="ok")])
    problems = run_dry_run("t", _tool(body), plan).all
    assert problems and "spec implies 'ok'" in problems[0]


def test_dry_run_skipped_without_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr, "_SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    assert run_dry_run("file_invoices", DOUBLE_MOVE, INVOICE_PLAN).all == []


def test_loop_feeds_dry_run_problems_and_plans_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg
    from tests.test_draft_checks import GOOD

    plans: list[int] = []
    seen: list[list[str]] = []
    results = iter([["dry run 1: raised FileNotFoundError"], []])

    def fake_draft(engine, name, desc, logic, previous_source="", problems=None):
        seen.append(list(problems or []))
        return GOOD

    monkeypatch.setattr(mtg, "_draft_with_engine", fake_draft)
    monkeypatch.setattr(mtg, "plan_dry_run", lambda *a: plans.append(1) or INVOICE_PLAN)
    monkeypatch.setattr(mtg, "run_dry_run", lambda *a: dr.DryRunResult(hard=next(results)))
    monkeypatch.setattr(mtg, "critique", lambda *a: [])
    _, attempts = mtg._draft_until_clean(object(), "alert_in", "d", "l")
    assert attempts == 2
    assert seen[1] == ["dry run 1: raised FileNotFoundError"]
    assert plans == [1]


def test_parse_plan_reads_fixture_ages() -> None:
    plan = parse_plan(json.dumps({
        "files": {"Downloads/old.dmg": {"content": "x", "age_days": 10}, "Downloads/new.dmg": "y",
                  "Downloads/bad.dmg": {"content": "z", "age_days": "lots"}},
        "calls": [{"kwargs": {}}],
    }))
    assert plan is not None
    assert plan.files == {"Downloads/old.dmg": "x", "Downloads/new.dmg": "y", "Downloads/bad.dmg": "z"}
    assert plan.ages == {"Downloads/old.dmg": 10.0}


@needs_sandbox
def test_fixture_age_sets_mtime() -> None:
    body = '''    import time
    p = os.path.expanduser("~/Downloads/old.dmg")
    return {"status": "ok", "age_days": (time.time() - os.path.getmtime(p)) / 86400}'''
    out, _ = dr._execute("t", _tool(body), {"Downloads/old.dmg": "x"}, {}, {"Downloads/old.dmg": 10})
    assert 9.9 < out["result"]["age_days"] < 10.1


@needs_sandbox
def test_long_timer_does_not_block_the_dry_run() -> None:
    body = '''    threading.Timer(300, lambda: None).start()
    return {"status": "ok"}'''
    import time as _time
    start = _time.time()
    assert run_dry_run("t", _tool(body), DryRunPlan(files={}, calls=[DryRunCall(expect_status="ok")])).all == []
    assert _time.time() - start < 10


@needs_sandbox
def test_exception_reports_draft_line() -> None:
    problems = run_dry_run("file_invoices", DOUBLE_MOVE, INVOICE_PLAN).all
    assert any("at line" in p and "shutil.move(f, target)" in p for p in problems), problems



@needs_sandbox
def test_expectation_mismatches_are_soft_and_crashes_are_hard() -> None:
    plan = DryRunPlan(files=INVOICE_PLAN.files, calls=[DryRunCall(
        kwargs={}, expect_status="error", expect_exists=["Downloads/inv1041.pdf"],
    )])
    fixed = run_dry_run("file_invoices", FIXED, plan)
    assert fixed.hard == []
    assert any("inv1041.pdf to exist" in p for p in fixed.soft)
    assert any("spec implies 'error'" in p for p in fixed.soft)
    crashed = run_dry_run("file_invoices", DOUBLE_MOVE, INVOICE_PLAN)
    assert any("FileNotFoundError" in p for p in crashed.hard)


def test_soft_notes_reach_reviewer_without_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import meta_tool_generator as mtg
    from tests.test_draft_checks import GOOD

    drafts: list[int] = []
    monkeypatch.setattr(mtg, "_draft_with_engine", lambda *a, **k: drafts.append(1) or GOOD)
    monkeypatch.setattr(mtg, "plan_dry_run", lambda *a: INVOICE_PLAN)
    monkeypatch.setattr(mtg, "run_dry_run", lambda *a: dr.DryRunResult(soft=["expected x to stay"]))
    monkeypatch.setattr(mtg, "critique", lambda *a: [])
    source, attempts = mtg._draft_until_clean(object(), "alert_in", "d", "l")
    assert attempts == 1 and drafts == [1]
    assert "# - expected x to stay" in source and source.endswith(GOOD)
