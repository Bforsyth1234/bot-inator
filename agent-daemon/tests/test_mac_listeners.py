"""Tests for MacAppListener's supervisor (#16) and debounce (#17).

No NSWorkspace: the helper is replaced with a tiny Python script written to
``tmp_path`` that prints JSON lines and/or exits, so the restart/backoff loop
runs against real subprocesses.
"""

from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from events.mac_listeners import MacAppListener


class _FakeBus:
    def __init__(self) -> None:
        self.events: list = []
        self._lock = threading.Lock()

    def push_threadsafe(self, event) -> None:
        with self._lock:
            self.events.append(event)

    def names(self) -> list:
        with self._lock:
            return [e.app_name for e in self.events]


def _script(tmp_path: Path, body: str, name: str = "helper.py") -> list[str]:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return [sys.executable, str(path)]


def _line(app: str, pid: int) -> str:
    return json.dumps({"app_name": app, "bundle_id": f"com.x.{app}", "pid": pid})


def _wait_for(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ------------------------------------------------------------------ #16


def test_helper_restarts_after_crash_with_backoff(tmp_path):
    # Each run appends a timestamp, then crashes with code 3.
    log = tmp_path / "starts.log"
    cmd = _script(
        tmp_path,
        f"""
        import sys, time
        with open({str(log)!r}, "a") as f:
            f.write(repr(time.monotonic()) + "\\n")
        sys.exit(3)
        """,
    )
    listener = MacAppListener(
        _FakeBus(), helper_cmd=cmd, backoff_initial=0.05, backoff_max=0.2,
        stable_after=10.0,
    )
    listener.start()
    try:
        # Generous deadline: 4 interpreter starts under a busy full suite can
        # exceed 2 s (todo #50). The gap assertions below stay exact.
        assert _wait_for(
            lambda: log.exists() and len(log.read_text().split()) >= 4, timeout=10.0,
        )
    finally:
        listener.stop()
    starts = [float(x) for x in log.read_text().split()]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # Backoff doubles 0.05 -> 0.10 -> 0.20 (gaps add spawn overhead on top,
    # so lower bounds are exact; a flat 0.05 s retry fails the last two).
    assert gaps[0] >= 0.05
    assert gaps[1] >= 0.10
    assert gaps[2] >= 0.20
    assert listener.restart_count >= 3
    assert listener.last_exit_code == 3
    assert "code 3" in (listener.last_error or "")


def test_backoff_caps_at_max(tmp_path):
    cmd = _script(tmp_path, "import sys; sys.exit(1)\n")
    listener = MacAppListener(
        _FakeBus(), helper_cmd=cmd, backoff_initial=0.01, backoff_max=0.04,
        stable_after=10.0,
    )
    listener.start()
    try:
        assert _wait_for(lambda: listener.restart_count >= 5)
        assert listener.current_backoff == pytest.approx(0.04)
    finally:
        listener.stop()


def test_backoff_resets_after_stable_uptime(tmp_path):
    # Runs 1-3 crash at once; later runs stay up 0.15 s (> stable_after).
    counter = tmp_path / "n"
    cmd = _script(
        tmp_path,
        f"""
        import os, sys, time
        p = {str(counter)!r}
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, "w").write(str(n + 1))
        if n >= 3:
            time.sleep(0.15)
        sys.exit(1)
        """,
    )
    listener = MacAppListener(
        _FakeBus(), helper_cmd=cmd, backoff_initial=0.02, backoff_max=1.0,
        stable_after=0.1,
    )
    listener.start()
    try:
        # After 3 fast crashes the backoff has grown to 0.16; once a run
        # survives past stable_after it drops back to the initial value.
        assert _wait_for(lambda: counter.exists() and int(counter.read_text()) >= 5)
        assert listener.current_backoff <= 0.04
    finally:
        listener.stop()


def test_stop_does_not_restart_and_reports_liveness(tmp_path):
    starts = tmp_path / "starts.log"
    cmd = _script(
        tmp_path,
        f"""
        import time
        open({str(starts)!r}, "a").write("x\\n")
        time.sleep(30)
        """,
    )
    listener = MacAppListener(
        _FakeBus(), helper_cmd=cmd, backoff_initial=0.01, backoff_max=0.01,
    )
    listener.start()
    assert _wait_for(lambda: listener.is_alive and starts.exists())
    status = listener.status()
    assert status["alive"] is True and status["restart_count"] == 0
    assert isinstance(status["pid"], int)
    listener.stop()
    time.sleep(0.1)
    assert listener.is_alive is False
    assert listener.restart_count == 0
    assert starts.read_text().count("x") == 1


# ------------------------------------------------------------------ #17


def _feeder(tmp_path: Path, steps: list[tuple[float, str]]) -> list[str]:
    """Helper that sleeps/prints per ``steps`` then idles until killed."""
    lines = "\n".join(
        f"time.sleep({delay}); print({text!r}, flush=True)" for delay, text in steps
    )
    return _script(tmp_path, f"import time\n{lines}\ntime.sleep(30)\n", "feeder.py")


def test_rapid_switch_cancels_pending_activation(tmp_path):
    bus = _FakeBus()
    cmd = _feeder(
        tmp_path,
        [(0.0, _line("A", 1)), (0.03, _line("B", 2)), (0.03, _line("C", 3))],
    )
    listener = MacAppListener(bus, helper_cmd=cmd, debounce_s=0.15)
    listener.start()
    try:
        assert _wait_for(lambda: bus.names() == ["C"])
        time.sleep(0.2)
        assert bus.names() == ["C"]
        assert bus.events[0].event_type == "app_activated"
        assert bus.events[0].metadata == {"bundle_id": "com.x.C", "pid": 3}
    finally:
        listener.stop()


def test_sustained_focus_emits_each_app(tmp_path):
    bus = _FakeBus()
    cmd = _feeder(tmp_path, [(0.0, _line("A", 1)), (0.12, _line("B", 2))])
    listener = MacAppListener(bus, helper_cmd=cmd, debounce_s=0.05)
    listener.start()
    try:
        assert _wait_for(lambda: bus.names() == ["A", "B"])
    finally:
        listener.stop()


def test_bounce_back_to_same_app_is_not_reemitted(tmp_path):
    bus = _FakeBus()
    cmd = _feeder(
        tmp_path,
        [(0.0, _line("A", 1)), (0.12, _line("B", 2)), (0.02, _line("A", 1))],
    )
    listener = MacAppListener(bus, helper_cmd=cmd, debounce_s=0.06)
    listener.start()
    try:
        assert _wait_for(lambda: bus.names() == ["A"])
        time.sleep(0.2)
        assert bus.names() == ["A"]
    finally:
        listener.stop()


def test_stop_cancels_pending_activation(tmp_path):
    bus = _FakeBus()
    got_line = tmp_path / "printed"
    cmd = _script(
        tmp_path,
        f"""
        import time
        print({_line("A", 1)!r}, flush=True)
        open({str(got_line)!r}, "w").write("1")
        time.sleep(30)
        """,
    )
    listener = MacAppListener(bus, helper_cmd=cmd, debounce_s=0.15)
    listener.start()
    assert _wait_for(got_line.exists)
    time.sleep(0.03)
    listener.stop()
    time.sleep(0.2)
    assert bus.names() == []


def test_non_json_lines_are_ignored(tmp_path):
    bus = _FakeBus()
    cmd = _feeder(tmp_path, [(0.0, "not json"), (0.0, "[1]"), (0.0, _line("A", 1))])
    listener = MacAppListener(bus, helper_cmd=cmd, debounce_s=0.02)
    listener.start()
    try:
        assert _wait_for(lambda: bus.names() == ["A"])
    finally:
        listener.stop()
