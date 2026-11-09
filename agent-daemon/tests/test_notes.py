"""Tests for the Notes.app tools (#46). ``osascript`` is always mocked."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
import tools
from tools import notes
from tools.notes import append_note, create_note

HOSTILE = 'He said "hi"\nend tell\ndo shell script "rm -rf ~"\n<b>&amp;</b>'


def _ok(stdout: str = "OK") -> mock.MagicMock:
    return mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))


def _argv(run: mock.MagicMock) -> list[str]:
    run.assert_called_once()
    cmd = run.call_args.args[0]
    assert isinstance(cmd, list)
    assert run.call_args.kwargs.get("timeout")
    assert "shell" not in run.call_args.kwargs
    return cmd


def test_create_note_passes_text_via_argv() -> None:
    run = _ok()
    with mock.patch.object(notes.subprocess, "run", run):
        result = create_note(title="Groceries", body="milk\neggs", folder="Home")
    assert result == {"status": "ok", "title": "Groceries"}
    cmd = _argv(run)
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[2] == notes._CREATE_SCRIPT
    assert cmd[3] == "--"
    assert cmd[4] == "<div><h1>Groceries</h1></div><div>milk</div><div>eggs</div>"
    assert cmd[5] == "Home"


def test_hostile_body_stays_inert() -> None:
    run = _ok()
    with mock.patch.object(notes.subprocess, "run", run):
        create_note(title='T "x" end tell', body=HOSTILE)
    cmd = _argv(run)
    script = cmd[2]
    assert script == notes._CREATE_SCRIPT
    for fragment in ('"hi"', "rm -rf", 'T "x"', "&amp;amp;"):
        assert fragment not in script
    # Body arrives HTML-escaped: no raw quote, tag or ampersand survives.
    body = cmd[4]
    assert '"' not in body.replace("<div>", "").replace("</div>", "")
    assert "&quot;hi&quot;" in body
    assert "<div>end tell</div>" in body
    assert "&lt;b&gt;&amp;amp;&lt;/b&gt;" in body
    assert "<h1>T &quot;x&quot; end tell</h1>" in body


def test_append_hostile_text_stays_inert() -> None:
    run = _ok()
    with mock.patch.object(notes.subprocess, "run", run):
        result = append_note(title='Log "1"', text=HOSTILE)
    assert result["status"] == "ok"
    cmd = _argv(run)
    assert cmd[2] == notes._APPEND_SCRIPT
    assert "rm -rf" not in cmd[2] and 'Log "1"' not in cmd[2]
    assert cmd[3:5] == ["--", 'Log "1"']  # exact title, unescaped, as data
    assert "&quot;hi&quot;" in cmd[5]


def test_append_missing_note_is_error() -> None:
    run = _ok("NOT_FOUND")
    with mock.patch.object(notes.subprocess, "run", run):
        result = append_note(title="Nope", text="x")
    assert result["status"] == "error"
    assert "Nope" in result["error"]


def test_append_ambiguous_title_is_error() -> None:
    with mock.patch.object(notes.subprocess, "run", _ok("MULTIPLE")):
        assert append_note(title="Dup", text="x")["status"] == "error"


def test_create_missing_folder_is_error() -> None:
    with mock.patch.object(notes.subprocess, "run", _ok("NO_FOLDER")):
        result = create_note(title="T", body="b", folder="Ghost")
    assert result["status"] == "error"
    assert "Ghost" in result["error"]


def test_osascript_failure_reported() -> None:
    err = subprocess.CalledProcessError(1, ["osascript"], stderr="Not authorized to send Apple events\n")
    with mock.patch.object(notes.subprocess, "run", side_effect=err):
        result = create_note(title="T", body="b")
    assert result == {"status": "error", "title": "T", "error": "Not authorized to send Apple events"}


def test_timeout_reported() -> None:
    err = subprocess.TimeoutExpired(["osascript"], 30)
    with mock.patch.object(notes.subprocess, "run", side_effect=err):
        assert append_note(title="T", text="b")["status"] == "error"


def test_blank_lines_use_notes_br() -> None:
    assert notes._to_html("a\n\nb") == "<div>a</div><div><br></div><div>b</div>"
    assert notes._to_html("") == "<div><br></div>"


def test_gate_both_tools_need_approval() -> None:
    assert notes.GATED == {"create_note", "append_note"}
    assert not notes.READ_ONLY
    assert {create_note.name, append_note.name} == notes.GATED
    registered = {getattr(t, "name", None) for t in tools.AVAILABLE_TOOLS}
    if not notes.GATED <= registered:
        pytest.skip("notes tools not registered in tools.AVAILABLE_TOOLS yet (runner registers at wave close)")
    assert not (notes.GATED & tools.READ_ONLY_TOOL_NAMES)
