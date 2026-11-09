"""Tests for tools/shortcuts.py (#43). ``subprocess.run`` is always mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools import shortcuts as sc

LIST_OUT = "Open App\nTake a Break\nText Last Image\n"


def _done(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


class FakeRun:
    """Records calls; answers ``shortcuts list`` and ``shortcuts run``."""

    def __init__(self, run_result=None, run_exc=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.run_result = run_result or {}
        self.run_exc = run_exc
        self.input_seen: str | None = None

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if args[:2] == ["shortcuts", "list"]:
            return _done(args, stdout=LIST_OUT)
        if "--input-path" in args:
            self.input_seen = Path(args[args.index("--input-path") + 1]).read_text()
        if self.run_exc:
            raise self.run_exc
        return _done(args, **self.run_result)

    def run_calls(self):
        return [c for c in self.calls if c[0][:2] == ["shortcuts", "run"]]


def test_list_shortcuts_returns_names():
    fake = FakeRun()
    with patch.object(sc.subprocess, "run", fake):
        out = sc.list_shortcuts()
    assert out == {"shortcuts": ["Open App", "Take a Break", "Text Last Image"], "status": "ok"}
    assert fake.calls[0][0] == ["shortcuts", "list"]


def test_list_shortcuts_error():
    with patch.object(sc.subprocess, "run", side_effect=FileNotFoundError("shortcuts")):
        out = sc.list_shortcuts()
    assert out["status"] == "error" and out["shortcuts"] == []


@pytest.mark.parametrize("name", ["Nope", "take a break", "Take a", "Take a Break "])
def test_unknown_name_runs_nothing(name):
    fake = FakeRun()
    with patch.object(sc.subprocess, "run", fake):
        out = sc.run_shortcut(name)
    assert out["status"] == "error"
    assert "list_shortcuts" in out["error"]
    assert fake.run_calls() == []


def test_run_returns_stdout_with_arg_list_and_timeout():
    fake = FakeRun(run_result={"stdout": "done!\n"})
    with patch.object(sc.subprocess, "run", fake):
        out = sc.run_shortcut("Take a Break")
    assert out == {"status": "ok", "output": "done!\n"}
    (args, kwargs), = fake.run_calls()
    assert args == ["shortcuts", "run", "Take a Break"]
    assert kwargs["timeout"] == 60
    assert not kwargs.get("shell")


def test_run_passes_input_text_and_cleans_up():
    fake = FakeRun(run_result={"stdout": "ok"})
    with patch.object(sc.subprocess, "run", fake):
        out = sc.run_shortcut("Open App", input_text="Safari")
    assert out["status"] == "ok"
    assert fake.input_seen == "Safari"
    (args, _), = fake.run_calls()
    assert not Path(args[args.index("--input-path") + 1]).exists()


def test_nonzero_exit_reported():
    fake = FakeRun(run_result={"returncode": 1, "stdout": "partial", "stderr": "boom\n"})
    with patch.object(sc.subprocess, "run", fake):
        out = sc.run_shortcut("Open App")
    assert out["status"] == "error"
    assert out["returncode"] == 1
    assert out["error"] == "boom"
    assert out["output"] == "partial"


def test_timeout_reported():
    fake = FakeRun(run_exc=subprocess.TimeoutExpired(["shortcuts"], 60))
    with patch.object(sc.subprocess, "run", fake):
        out = sc.run_shortcut("Open App")
    assert out["status"] == "error" and "timed out" in out["error"]


# --- approval gate (shared decision 5) ---------------------------------

def test_intended_gate_membership():
    assert "list_shortcuts" in sc.READ_ONLY
    assert "run_shortcut" in sc.GATED
    assert not (sc.READ_ONLY & sc.GATED)


def test_registered_gate_membership():
    import tools

    registered = {getattr(t, "name", getattr(t, "__name__", None)) for t in tools.AVAILABLE_TOOLS}
    missing = (sc.READ_ONLY | sc.GATED) - registered
    if missing:
        pytest.skip(f"#43 tools not registered in tools.AVAILABLE_TOOLS yet: {sorted(missing)}")
    assert sc.READ_ONLY <= tools.READ_ONLY_TOOL_NAMES
    assert not (sc.GATED & tools.READ_ONLY_TOOL_NAMES), "run_shortcut must be approval-gated"
