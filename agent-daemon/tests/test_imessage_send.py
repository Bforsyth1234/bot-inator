"""#106: the handle and the body reach osascript as argv, never as script text.

``subprocess.run`` is mocked, so no iMessage is ever sent. The one real
``osascript`` call runs a harmless echo script (no Messages).
"""
from __future__ import annotations

import importlib
import shutil
import subprocess

import pytest

import ai  # noqa: F401  (load ai before tools: the two packages import each other)
from events import remote_channel as rc

# The module, not the tool object ``tools/__init__`` re-exports under that name.
si = importlib.import_module("tools.send_imessage")

OWN = "+14155550100"
# ’ → " \ newline emoji, plus text that would break out of an AppleScript string.
TRICKY = ('it’s 3 → 4 "quoted" back\\slash\nline two \U0001F600'
          ' " & (do shell script "x") & "')


def _capture_run(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _script_text(argv):
    return "\n".join(a for prev, a in zip(argv, argv[1:]) if prev == "-e")


def test_remote_channel_send_passes_body_as_argv(monkeypatch):
    calls = _capture_run(monkeypatch)
    assert rc.applescript_send(OWN, TRICKY) is True
    [(argv, kw)] = calls
    assert argv[0] == "osascript" and argv[-3:] == ["--", OWN, TRICKY]
    script = _script_text(argv)
    assert "on run argv" in script and "item 2 of argv" in script
    assert TRICKY not in script and "’" not in script and OWN not in script
    assert "\\u2019" not in " ".join(argv)  # no json.dumps escapes anywhere
    assert kw.get("check") is True


def test_send_imessage_tool_uses_the_same_helper(monkeypatch):
    calls = _capture_run(monkeypatch)
    assert si.send_imessage(OWN, TRICKY)["status"] == "ok"
    [(argv, _)] = calls
    assert argv == si.osascript_argv(OWN, TRICKY)
    assert argv[-2:] == [OWN, TRICKY] and TRICKY not in _script_text(argv)


def test_send_failure_is_reported(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, "", "nope")

    monkeypatch.setattr(subprocess, "run", boom)
    assert rc.applescript_send(OWN, "hi") is False
    assert si.send_imessage(OWN, "hi") == {"status": "error", "target": OWN, "error": "nope"}


@pytest.mark.skipif(shutil.which("osascript") is None, reason="needs macOS osascript")
def test_body_survives_a_real_osascript_round_trip():
    # The helper's argv tail fed to an echo script: the text reaches
    # AppleScript intact. Nothing talks to Messages.
    tail = si.osascript_argv(OWN, TRICKY)[-3:]
    out = subprocess.run(
        ["osascript", "-e", "on run argv", "-e", "return item 2 of argv", "-e", "end run", *tail],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    assert out[:-1] == TRICKY  # osascript adds one trailing newline
