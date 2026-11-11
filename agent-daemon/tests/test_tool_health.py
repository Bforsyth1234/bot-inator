"""Installed-tool health log, the repair-offer rule and its routes (#90)."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import tool_health as th
from ai.idea_sources import IdeaState
from tools.tool_sandbox import ToolSandboxError, ToolSandboxTimeout


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def log(clock: Clock) -> th.ToolHealthLog:
    return th.ToolHealthLog(None, state=IdeaState(None, clock=clock), clock=clock)


def _fail(log: th.ToolHealthLog, msg: str = "AttributeError: 'NoneType' has no attribute 'x'",
          name: str = "notes_tool", **kw: Any) -> Any:
    return log.observe(name, exc=ToolSandboxError(msg), **kw)


# ---- the rule -----------------------------------------------------------------


def test_two_same_class_failures_give_one_repair_idea(log: th.ToolHealthLog) -> None:
    assert _fail(log) is None
    idea = _fail(log)
    assert idea is not None
    assert idea["source"] == "repair" and idea["builtin_tool"] == th.REPAIR_TOOL_NAME
    assert idea["repair_target"] == "notes_tool"
    assert idea["event_details"] == {"tool_name": "notes_tool"}
    # Cooldown: a third (and fourth) failure does not offer again.
    assert _fail(log) is None
    assert _fail(log) is None
    assert log.health("notes_tool")["repair_offered_until"] is not None


def test_two_different_classes_in_a_row_are_not_enough(log: th.ToolHealthLog) -> None:
    assert _fail(log, "AttributeError: a") is None
    assert _fail(log, "KeyError: 'b'") is None
    # ...but a third failure within 7 days is.
    assert _fail(log, "ValueError: c") is not None


def test_three_failures_must_fall_within_seven_days(log: th.ToolHealthLog, clock: Clock) -> None:
    assert _fail(log, "AttributeError: a") is None
    clock.now += 8 * 24 * 3600
    assert _fail(log, "KeyError: b") is None
    assert _fail(log, "ValueError: c") is None  # only 2 inside the window
    assert _fail(log, "IndexError: d") is not None


def test_ok_run_breaks_the_streak(log: th.ToolHealthLog) -> None:
    assert _fail(log) is None
    assert log.observe("notes_tool", result={"status": "ok"}) is None
    assert _fail(log) is None
    health = log.health("notes_tool")
    assert health["status"] == "failing" and health["ok_runs"] == 1


def test_error_result_dict_counts_as_failure(log: th.ToolHealthLog) -> None:
    bad = {"status": "error", "message": "execution error: Notes got an error: Can't get folder (-1728)"}
    assert log.observe("notes_tool", result=bad) is None
    idea = log.observe("notes_tool", result=bad)
    assert idea is not None and idea["error_class"] == "ToolError"
    assert "-1728" in idea["card_text"]


def test_cooldown_expires_after_seven_days(log: th.ToolHealthLog, clock: Clock) -> None:
    _fail(log)
    assert _fail(log) is not None
    clock.now += 7 * 24 * 3600 + 60
    assert _fail(log) is not None


def test_cooldown_lives_in_the_shared_idea_state(tmp_path: Path, clock: Clock) -> None:
    state_path = tmp_path / "idea_state.json"
    log = th.ToolHealthLog(None, state=IdeaState(state_path, clock=clock), clock=clock)
    _fail(log)
    assert _fail(log) is not None
    again = th.ToolHealthLog(None, state=IdeaState(state_path, clock=clock), clock=clock)
    _fail(again)
    assert _fail(again) is None  # the cooldown survived a restart


def test_never_for_a_disabled_tool(log: th.ToolHealthLog) -> None:
    assert _fail(log, disabled=True) is None
    assert _fail(log, disabled=True) is None
    assert log.evaluate("notes_tool", disabled=True)["reason"] == "disabled"


def test_repair_marker_resets_the_count(log: th.ToolHealthLog, clock: Clock) -> None:
    _fail(log)
    _fail(log)
    clock.now += 8 * 24 * 3600  # past the cooldown
    log.note_change("notes_tool", "repaired")
    assert _fail(log) is None  # the failures before the repair no longer count
    assert log.health("notes_tool")["last_change"]["kind"] == "repaired"


# ---- not repairable -------------------------------------------------------------


@pytest.mark.parametrize("exc,category", [
    (ToolSandboxError("PermissionError: [Errno 1] Operation not permitted: '/Users/x/Library/a'"), "sandbox"),
    (ToolSandboxError("RuntimeError: open: refused by the tool sandbox: Terminal"), "sandbox"),
    (ToolSandboxTimeout("TimeoutError: t did not finish within 30s (sandboxed)"), "timeout"),
    (ToolSandboxError("CalledProcessError: execution error: Not authorized to send Apple events to Notes. (-1743)"), "permission"),
    (ToolSandboxError("TypeError: notes_tool() got an unexpected keyword argument 'titel'"), "bad_arguments"),
    (ToolSandboxError("TypeError: notes_tool() missing 1 required positional argument: 'title'"), "bad_arguments"),
])
def test_unrepairable_failures_never_offer_and_say_why(
    log: th.ToolHealthLog, exc: Exception, category: str,
) -> None:
    for _ in range(4):
        assert log.observe("notes_tool", exc=exc) is None
    latest = log.health("notes_tool")["recent_failures"][0]
    assert latest["repairable"] is False
    assert latest["category"] == category
    assert latest["reason"] == th.REASONS[category]
    assert log.evaluate("notes_tool")["reason"] == f"not repairable ({category})"


def test_repairable_classification() -> None:
    ok, cat, reason = th.classify_failure("AttributeError: 'NoneType' object has no attribute 'x'",
                                          "AttributeError")
    assert ok and cat is None and reason is None


def test_error_class_from_sandbox_child_error() -> None:
    f = th.describe_failure(exc=ToolSandboxError("subprocess.CalledProcessError: Command x returned 1"))
    assert f is not None and f["error_class"] == "CalledProcessError"
    f = th.describe_failure(exc=KeyError("k"))
    assert f is not None and f["error_class"] == "KeyError"
    assert th.describe_failure(result={"status": "ok"}) is None


# ---- privacy --------------------------------------------------------------------


def test_argument_values_are_not_stored(log: th.ToolHealthLog) -> None:
    log.observe(
        "notes_tool", exc=ToolSandboxError("ValueError: no note called Secret Plans for Bob"),
        arg_values=[["Secret Plans for Bob"], {"folder": "Work"}],
    )
    entry = log.health("notes_tool")["recent_failures"][0]
    assert "Secret Plans" not in entry["message"]
    assert th.ARG_PLACEHOLDER in entry["message"]


def test_card_text_is_the_only_place_with_the_message() -> None:
    idea = th.repair_idea("notes_tool", {"error_class": "KeyError", "message": "KeyError: 'private'"})
    assert "private" in idea["card_text"]
    for key in ("description", "expected_logic", "event_details", "event_details_redacted"):
        assert "private" not in str(idea[key])


def test_persistence_round_trip_and_mode(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "tool_health.json"
    log = th.ToolHealthLog(path, state=IdeaState(None, clock=clock), clock=clock)
    _fail(log)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = th.ToolHealthLog(path, state=IdeaState(None, clock=clock), clock=clock)
    assert again.health("notes_tool")["recent_failures"][0]["error_class"] == "AttributeError"
    assert _fail(again) is not None  # the first failure counted after reload


# ---- the orchestrator hook --------------------------------------------------------


class FakeBus:
    loop = object()

    def __init__(self) -> None:
        self.events: list[Any] = []

    def push_threadsafe(self, event: Any) -> None:
        self.events.append(event)


class FakeOrch:
    def __init__(self, log: th.ToolHealthLog, gen: Path) -> None:
        self.tool_health = log
        self.event_bus = FakeBus()
        self.generated_tools_dir = gen


def test_record_call_publishes_pattern_detected(log: th.ToolHealthLog, tmp_path: Path) -> None:
    orch = FakeOrch(log, tmp_path)
    exc = ToolSandboxError("AttributeError: x")
    assert th.record_call(orch, "notes_tool", exc=exc, args=["a"], kwargs={}) is None
    idea = th.record_call(orch, "notes_tool", exc=exc)
    assert idea is not None
    assert [e.event_type for e in orch.event_bus.events] == ["pattern_detected"]
    assert orch.event_bus.events[0].metadata["repair_target"] == "notes_tool"


def test_record_call_skips_a_disabled_tool_and_never_raises(log: th.ToolHealthLog, tmp_path: Path) -> None:
    (tmp_path / "disabled").mkdir()
    (tmp_path / "disabled" / "notes_tool.py").write_text("x")
    orch = FakeOrch(log, tmp_path)
    for _ in range(3):
        assert th.record_call(orch, "notes_tool", exc=ToolSandboxError("AttributeError: x")) is None
    assert orch.event_bus.events == []

    class Broken:
        def observe(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("boom")

    assert th.record_call(type("O", (), {"tool_health": Broken()})(), "t", exc=KeyError()) is None
    assert th.record_call(object(), "t", exc=KeyError()) is None


# ---- routes -----------------------------------------------------------------------


def _client(log: Any, gen: Path, orch: Any = None) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import tool_health as route

    app = FastAPI()
    app.include_router(route.router)
    app.state.tool_health = log
    app.state.generated_tools_dir = gen
    app.state.orchestrator = orch
    return TestClient(app)


def test_health_route(log: th.ToolHealthLog, tmp_path: Path) -> None:
    _fail(log)
    client = _client(log, tmp_path)
    body = client.get("/api/tools/notes_tool/health").json()
    assert body["status"] == "failing" and body["can_restore"] is False
    assert body["recent_failures"][0]["error_class"] == "AttributeError"
    assert client.get("/api/tool-health").json()["tools"][0]["tool_name"] == "notes_tool"
    assert client.get("/api/tools/Bad-Name/health").status_code == 404
    assert client.post("/api/tools/notes_tool/restore-previous").status_code == 404


def test_health_route_503_without_a_log(tmp_path: Path) -> None:
    assert _client(None, tmp_path).get("/api/tool-health").status_code == 503
