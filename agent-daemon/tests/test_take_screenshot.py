"""Tests for the built-in take_screenshot (todo #102). ``subprocess`` is
faked: no real screenshot is ever taken."""

from __future__ import annotations

import importlib
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools

ts = importlib.import_module("tools.take_screenshot")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    h = Path(os.path.realpath(tmp_path)) / "home"
    (h / "Desktop").mkdir(parents=True)
    (h / "Library").mkdir()
    (h / "Pictures").mkdir()
    monkeypatch.setattr(ts, "_home", lambda: h)
    monkeypatch.setattr(ts, "_now", lambda: datetime(2026, 11, 11, 9, 5, 7))
    monkeypatch.delenv("TMPDIR", raising=False)
    return h


@pytest.fixture()
def capture(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        Path(argv[-1]).write_bytes(b"\x89PNG fake")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    monkeypatch.setattr(ts, "_screen_recording_granted", lambda: True)
    return calls


def test_default_path_is_the_desktop(home, capture) -> None:
    result = ts.take_screenshot()
    expected = home / "Desktop" / "Screenshot 2026-11-11 at 09.05.07.png"
    assert result == {"status": "ok", "path": str(expected)}
    assert capture == [["/usr/sbin/screencapture", "-x", str(expected)]]


def test_given_path_under_home(home, capture) -> None:
    result = ts.take_screenshot(str(home / "Pictures" / "shot"))
    assert result["status"] == "ok"
    assert result["path"] == str(home / "Pictures" / "shot.png")


@pytest.mark.parametrize("path,why", [
    ("/tmp/shot.png", "home folder"),
    ("/etc/shot.png", "home folder"),
    ("relative/shot.png", "absolute"),
    ("lib/x.png", "protected"),
    ("library/Caches/x.png", "protected"),
    (".SSH/x.png", "protected"),
    ("Library/shot.png", "protected"),
    (".ssh/shot.png", "protected"),
    (".bot-inator/shot.png", "protected"),
    ("Desktop/shot.jpg", ".png"),
    ("Missing/shot.png", "does not exist"),
])
def test_refuses_paths(home, capture, path: str, why: str) -> None:
    if path == "lib/x.png":
        # Through a symlink into ~/Library: resolved, then refused.
        (home / "lib").symlink_to(home / "Library")
    (home / "Library" / "Caches").mkdir(exist_ok=True)
    (home / ".ssh").mkdir(exist_ok=True)
    full = path if path.startswith("/") or path.startswith("relative") else str(home / path)
    result = ts.take_screenshot(full)
    assert result["status"] == "error" and why in result["error"], result
    assert capture == []


def test_refuses_the_bot_inator_repo(home, capture, monkeypatch) -> None:
    repo = home / "dev" / "bot-inator"
    (repo / "agent-daemon").mkdir(parents=True)
    from tools import tool_sandbox

    monkeypatch.setattr(tool_sandbox, "_DAEMON_ROOT", repo / "agent-daemon")
    result = ts.take_screenshot(str(repo / "agent-daemon" / "x.png"))
    assert result["status"] == "error" and "protected" in result["error"]
    assert capture == []


def test_never_overwrites(home, capture) -> None:
    existing = home / "Desktop" / "a.png"
    existing.write_bytes(b"keep")
    result = ts.take_screenshot(str(existing))
    assert result["status"] == "error" and "already exists" in result["error"]
    assert existing.read_bytes() == b"keep" and capture == []


def test_tilde_paths_expand(home, capture, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    assert ts.take_screenshot("~/Desktop/t.png")["path"] == str(home / "Desktop" / "t.png")


def test_no_file_written_points_to_screen_recording(home, monkeypatch) -> None:
    monkeypatch.setattr(ts.subprocess, "run", lambda argv, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="could not create image from display"))
    monkeypatch.setattr(ts, "_screen_recording_granted", lambda: False)
    result = ts.take_screenshot()
    assert result["status"] == "error"
    assert "Screen Recording" in result["error"] and "could not create image" in result["error"]


def test_denied_permission_is_noted_on_success(home, capture, monkeypatch) -> None:
    monkeypatch.setattr(ts, "_screen_recording_granted", lambda: False)
    result = ts.take_screenshot()
    assert result["status"] == "ok" and "Screen Recording" in result["note"]


def test_screencapture_raising_is_an_error(home, monkeypatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired("screencapture", 20)

    monkeypatch.setattr(ts.subprocess, "run", boom)
    assert ts.take_screenshot()["status"] == "error"


def test_inside_the_tool_sandbox_it_says_to_call_it_directly(home, capture, monkeypatch) -> None:
    monkeypatch.setenv("TMPDIR", "/private/var/folders/x/tool-sandbox-abc/tmp/")
    result = ts.take_screenshot()
    assert result["status"] == "error" and "directly" in result["error"]
    assert capture == []


def test_card_note_says_it_captures_everything(home) -> None:
    note = ts.card_note()
    assert "everything visible on screen" in note and "Desktop" in note
    assert "refused" in ts.card_note(path="/tmp/x.png")


def test_registered_and_approval_gated() -> None:
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    assert "take_screenshot" in names
    assert "take_screenshot" not in tools.READ_ONLY_TOOL_NAMES
    assert ts.GATED.isdisjoint(tools.READ_ONLY_TOOL_NAMES)


def test_the_sandbox_still_does_not_allow_screencapture() -> None:
    from tools import tool_sandbox

    assert "/usr/sbin/screencapture" not in tool_sandbox.ALLOWED_EXECUTABLES
