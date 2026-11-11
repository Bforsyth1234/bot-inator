"""#84 "Watch me": a recorded demonstration becomes a reviewed tool idea."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import activity_store, privacy
from ai import demonstration as demo
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent
from tests.test_wave15_wiring import _Loop, _cards, _live_orch
from tools import meta_tool_generator as mtg

HOME = str(Path.home())
DL = f"{HOME}/Downloads"
RECEIPTS = f"{HOME}/Documents/Receipts"


# ---- helpers ---------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _app(name: str) -> ContextEvent:
    return ContextEvent(event_type="app_activated", app_name=name, metadata={"bundle_id": "x." + name})


def _file(path: str, action: str, src: str = "") -> ContextEvent:
    meta = {"path": path, "action": action}
    if src:
        meta["from"] = src
    return ContextEvent(event_type="file", metadata=meta)


def _pdf_demo() -> list[ContextEvent]:
    """Download a PDF, move it to a folder, open that folder in Finder."""
    return [
        _app("Safari"),
        _file(f"{DL}/invoice.pdf", "renamed", f"{DL}/invoice.pdf.download"),
        _file(f"{DL}/invoice.pdf", "modified"),
        _app("Finder"),
        _app("Mail"),          # a stray switch, collapsed into the next one
        _app("Finder"),
        _file(f"{RECEIPTS}/invoice.pdf", "renamed", f"{DL}/invoice.pdf"),
        ContextEvent(event_type="clipboard_changed", app_name="Finder",
                     metadata={"app": "Finder", "kind": "file"}),
        _app("MenuBarUI"),     # clicking Stop: not a step
    ]


def _controller(clock: Clock | None = None, **kw: Any) -> tuple[demo.DemonstrationController, dict]:
    out: dict[str, list] = {"published": [], "frames": [], "requeued": []}

    async def publish(e: ContextEvent) -> None:
        out["published"].append(e)

    async def send(frame: dict) -> None:
        out["frames"].append(frame)

    async def requeue(e: ContextEvent) -> None:
        out["requeued"].append(e)

    kw.setdefault("enrich", None)
    ctl = demo.DemonstrationController(
        publish=publish, send=send, requeue=requeue, clock=clock or Clock(), **kw)
    return ctl, out


@pytest.fixture(autouse=True)
def _local_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: False)
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: False)


async def _record(ctl: demo.DemonstrationController, events: list[ContextEvent]) -> list[bool]:
    await ctl.start()
    return [await ctl.intercept(e) for e in events]


# ---- the summarizer --------------------------------------------------------


def test_stop_yields_summarized_steps_in_order() -> None:
    ctl, out = _controller()

    async def run() -> dict:
        await _record(ctl, _pdf_demo())
        return await ctl.stop()

    trace = asyncio.run(run())
    assert trace["state"] == "stopped"
    texts = [s["text"] for s in trace["steps"]]
    assert texts == [
        "Switched to Safari",
        "invoice.pdf downloaded to ~/Downloads, then moved to ~/Documents/Receipts",
        "Switched to Finder",
        "Copied file in Finder",
    ]
    # Ids are unique and come from the raw events (stable while recording).
    ids = [s["id"] for s in trace["steps"]]
    assert len(set(ids)) == len(ids)
    assert out["frames"][-1] == trace


def test_rename_in_place_and_new_file() -> None:
    raw = [demo.RawEntry(1, 0, "file", "", {"path": f"{DL}/a.txt", "action": "created"}),
           demo.RawEntry(2, 0, "file", "", {"path": f"{DL}/b.txt", "action": "renamed",
                                            "from": f"{DL}/a.txt"}),
           demo.RawEntry(3, 0, "file", "", {"path": f"{DL}/c.txt", "action": "created"})]
    steps = demo.summarize(raw)
    assert [s.text for s in steps] == [
        "a.txt arrived in ~/Downloads, then renamed to b.txt",
        "New file c.txt in ~/Downloads",
    ]
    assert [s.kind for s in steps] == ["file_rename", "file_new"]


def test_enrichment_adds_title_and_url_at_an_app_step() -> None:
    def enrich(event: ContextEvent) -> dict:
        return {"title": "Invoices", "url": "https://example.org/inv"} if event.app_name == "Safari" else {}

    ctl, _ = _controller(enrich=enrich)

    async def run() -> dict:
        await _record(ctl, [_app("Safari")])
        return await ctl.stop()

    [step] = asyncio.run(run())["steps"]
    assert step["text"] == "Switched to Safari: Invoices (https://example.org/inv)"


# ---- the event gate --------------------------------------------------------


def _wired_handle_event(orch: Orchestrator, ctl: demo.DemonstrationController):
    """The runner's wiring: the recording sees each event first (after notices)."""
    original = Orchestrator._handle_event

    async def handle(event: ContextEvent) -> None:
        if event.event_type != "system_notice" and await ctl.intercept(event):
            return
        await original(orch, event)

    return handle


def _spied_orch(loop: asyncio.AbstractEventLoop, seen: list[str]) -> Orchestrator:
    decide_calls: list[str] = []
    delivery = SimpleNamespace(decide=lambda e: (decide_calls.append(e.event_type),
                                                 SimpleNamespace(action="run", record_activity=True))[1],
                               note_ambient_card=lambda: None)
    orch = _live_orch(loop, delivery=delivery)
    orch.pattern_recognizer = SimpleNamespace(observe=lambda e: seen.append("observer"))
    orch.idea_sources = [SimpleNamespace(observe=lambda e: seen.append("source"))]

    async def run_agent(*_a: Any, **_k: Any) -> str:
        seen.append("agent")
        return "no action"

    async def analyse(_e: Any) -> str:
        return ""

    async def recall(_e: Any) -> list:
        return []

    orch._run_agent = run_agent  # type: ignore[method-assign]
    orch._analyse_event = analyse  # type: ignore[method-assign]
    orch._recall_memories = recall  # type: ignore[method-assign]
    orch.decide_calls = decide_calls  # type: ignore[attr-defined]
    return orch


@pytest.fixture
def activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> activity_store.ActivityStore:
    store = activity_store.ActivityStore(tmp_path / "activity.db")
    monkeypatch.setattr(activity_store, "_default", store)
    monkeypatch.setenv(activity_store.ENABLED_KEY, "1")
    yield store
    store.close()


def test_events_during_a_recording_start_no_turn_and_skip_the_activity_store(activity) -> None:
    seen: list[str] = []

    async def run() -> tuple[Orchestrator, demo.DemonstrationController, dict]:
        orch = _spied_orch(asyncio.get_running_loop(), seen)
        ctl, out = _controller()
        handle = _wired_handle_event(orch, ctl)
        await ctl.start()
        for e in _pdf_demo():
            await handle(e)
        mail = ContextEvent(event_type="mail_received", metadata={"subject": "hi", "mailbox": "INBOX"})
        await handle(mail)
        assert seen == [] and orch.decide_calls == []  # type: ignore[attr-defined]
        assert activity.count() == 0
        # A chat turn still runs while recording (the user asked).
        await handle(ContextEvent(event_type="user_message", metadata={"text": "hi"}))
        await ctl.stop()
        # The deferred mail comes back once, after the stop, in order.
        assert out["requeued"] == [mail] and ctl.deferred == []
        # Control: after the stop the same event is handled and recorded as usual.
        seen.clear()
        await handle(_app("Notes"))
        return orch, ctl, out

    orch, ctl, out = asyncio.run(run())
    assert "observer" in seen and "source" in seen
    assert activity.count() == 1
    assert [r["event_type"] for r in activity.rows()] == ["app_activated"]


def test_recorder_ignores_events_when_not_recording() -> None:
    ctl, _ = _controller()
    assert asyncio.run(ctl.intercept(_app("Safari"))) is False
    assert ctl.recorder.raw == []


def test_module_never_touches_the_activity_store() -> None:
    tree = ast.parse(Path(demo.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            names.add(node.module or "")
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    assert not any("activity_store" in n for n in names)
    assert "sqlite3" not in names and "open(" not in Path(demo.__file__).read_text()


# ---- the 5-minute stop -----------------------------------------------------


def test_hard_stop_after_five_minutes_on_the_clock() -> None:
    clock = Clock()
    ctl, _ = _controller(clock)

    async def run() -> dict:
        await ctl.start()
        assert ctl.trace()["ends_at"] == clock.t + 300
        await ctl.intercept(_app("Safari"))
        clock.t += 301
        taken = await ctl.intercept(_app("Finder"))
        assert taken is False  # recording is over: the event is handled as usual
        return ctl.trace()

    trace = asyncio.run(run())
    assert demo.MAX_SECONDS == 300
    assert trace["state"] == "stopped" and trace["stop_reason"] == "time"
    assert [s["text"] for s in trace["steps"]] == ["Switched to Safari"]


def test_hard_stop_timer_fires_without_events() -> None:
    ctl, out = _controller(max_seconds=0.05)

    async def run() -> None:
        await ctl.start()
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert ctl.recorder.state == "stopped" and ctl.recorder.stop_reason == "time"
    assert out["frames"][-1]["state"] == "stopped"


# ---- the idea ----------------------------------------------------------------


def _drafted(removed: list[str] | None = None, sentence: str = "File invoice PDFs into Receipts and open the folder") -> tuple[demo.DemonstrationController, dict, dict]:
    ctl, out = _controller()

    async def run() -> dict:
        await _record(ctl, _pdf_demo())
        trace = await ctl.stop()
        drop = removed if removed is not None else []
        if removed == ["copy"]:
            drop = [s["id"] for s in trace["steps"] if s["text"].startswith("Copied")]
        return await ctl.draft(sentence, drop)

    final = asyncio.run(run())
    return ctl, out, final


def test_a_removed_line_is_absent_from_the_idea_and_the_trace_is_dropped() -> None:
    ctl, out, final = _drafted(["copy"])
    [event] = out["published"]
    idea = event.metadata
    assert event.event_type == "pattern_detected"
    assert idea["source"] == "demonstration" and idea["trigger"] == "demonstration"
    assert "Copied" not in idea["expected_logic"] and "Copied" not in idea["card_text"]
    assert "then moved to ~/Documents/Receipts" in idea["expected_logic"]
    assert "Switched to Finder" in idea["expected_logic"]
    assert idea["tool_name"] == "file_invoice_pdfs_receipts_open"
    assert "builtin_tool" not in idea and idea["delivery_bypass"] is True
    # Dropped after the draft.
    assert ctl.recorder.raw == [] and final["state"] == "drafted" and final["steps"] == []


def test_without_removal_the_line_is_in_the_idea() -> None:
    _, out, _ = _drafted([])
    assert "Copied file in Finder" in out["published"][0].metadata["expected_logic"]


def test_draft_needs_a_sentence() -> None:
    ctl, out, final = _drafted([], sentence="  ")
    assert out["published"] == [] and final["state"] == "stopped"
    assert "sentence" in final["reason"]


def test_app_only_demo_maps_to_the_built_in() -> None:
    ctl, out = _controller()

    async def run() -> None:
        await _record(ctl, [_app("Slack"), _file(f"{DL}/x", "modified"), _app("Calendar")])
        trace = await ctl.stop()
        drop = [s["id"] for s in trace["steps"] if s["text"].startswith("Changed")]
        await ctl.draft("Open my morning apps", drop)

    asyncio.run(run())
    idea = out["published"][0].metadata
    assert idea["builtin_tool"] == "open_app" and idea["tool_name"] == "open_app"
    assert '"name": "Slack"' in idea["event_details"] and '"name": "Calendar"' in idea["event_details"]


def test_a_named_shortcut_is_offered_instead_of_a_new_tool() -> None:
    ctl, out = _controller(shortcuts=lambda: ["File Receipts", "Other"])

    async def run() -> None:
        await _record(ctl, _pdf_demo())
        await ctl.stop()
        await ctl.draft("Do what file receipts does", [])

    asyncio.run(run())
    idea = out["published"][0].metadata
    assert idea["builtin_tool"] == "run_shortcut"
    assert '"name": "File Receipts"' in idea["event_details"]


@pytest.fixture
def install_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gen = tmp_path / "generated"
    gen.mkdir()
    monkeypatch.setattr(mtg, "_ORCHESTRATOR", SimpleNamespace(
        tools=[], _dynamic_tool_names=set(), mark_tool_ready_for_rerun=lambda n: None,
        load_dynamic_tools=lambda: [], _current_event_id="evt_demo", _turn_kind="pattern"))
    monkeypatch.setattr(mtg, "_ENGINE", object())
    monkeypatch.setattr(mtg, "_DRAFT_ENGINE", None)
    monkeypatch.setattr(mtg, "_GENERATED_DIR", gen)
    monkeypatch.setattr(mtg, "_CURRENT_IDEA", None)
    drafts: list[tuple] = []
    code_cards: list[tuple] = []
    src = "from smolagents import tool\n"

    def draft(_engine: Any, name: str, description: str, logic: str) -> tuple[str, int]:
        drafts.append((name, description, logic))
        return src, 1

    def code_card(*a: Any, **k: Any) -> Any:
        code_cards.append(a)
        return SimpleNamespace(approved=False, edited_code=None, user_note="not now")

    monkeypatch.setattr(mtg, "_draft_until_clean", draft)
    monkeypatch.setattr(mtg, "_request_code_approval", code_card)
    monkeypatch.setattr(mtg, "_plain_summary", lambda *a: "")
    return SimpleNamespace(drafts=drafts, code_cards=code_cards)


def test_the_idea_reaches_generate_custom_tool_through_the_idea_card_and_the_code_card(install_env) -> None:
    _, out, _ = _drafted(["copy"])
    event = out["published"][0]
    idea = event.metadata
    prompt = Orchestrator._build_pattern_prompt(event)
    assert "generate_custom_tool" in prompt and repr(idea["tool_name"]) in prompt
    assert demo.PATTERN_SOURCE_INTRO  # the runner adds it to _PATTERN_SOURCE_INTROS

    with _Loop() as loop:
        orch = _live_orch(loop)
        orch._turn_kind = "pattern"
        orch._current_event_type = "pattern_detected"
        orch._current_idea = idea
        gated = Orchestrator._wrap_tool_for_approval(orch, mtg.generate_custom_tool)
        # What the agent's one call in the pattern turn looks like.
        result = gated.forward(tool_name=idea["tool_name"], description=idea["description"],
                               expected_logic=idea["expected_logic"])

    # 1. The idea card (7b gate one), carrying the reviewed steps.
    [card] = _cards(orch)
    assert card.tool_name == "generate_custom_tool"
    assert "From your demonstration (3 steps)" in (card.summary or "")
    assert "Copied" not in (card.summary or "")
    # 2. The spec went to the drafter unchanged, then 3. the code card.
    assert install_env.drafts == [(idea["tool_name"], idea["description"], idea["expected_logic"])]
    assert [c[0] for c in install_env.code_cards] == [idea["tool_name"]]
    assert result["status"] == "denied"  # code card denied: nothing installed


# ---- cloud engines (decision 4) ------------------------------------------------


def test_cloud_drafting_engine_refuses_the_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "drafting")
    ctl, out = _controller()
    trace = asyncio.run(ctl.start())
    assert trace["state"] == "refused" and "drafting engine runs in the cloud" in trace["reason"]
    assert ctl.recording is False
    assert asyncio.run(ctl.intercept(_app("Safari"))) is False


def test_cloud_switch_mid_recording_refuses_the_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    ctl, out = _controller()

    async def run() -> dict:
        await _record(ctl, _pdf_demo())
        await ctl.stop()
        monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
        return await ctl.draft("File invoices", [])

    final = asyncio.run(run())
    assert out["published"] == [] and "main engine" in final["reason"]


def test_cloud_with_personal_text_allowed_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: True)
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: True)
    ctl, _ = _controller()
    assert asyncio.run(ctl.start())["state"] == "recording"


def test_cancel_drops_the_trace_and_releases_deferred() -> None:
    ctl, out = _controller()

    async def run() -> dict:
        await ctl.start()
        await ctl.intercept(_app("Safari"))
        await ctl.intercept(ContextEvent(event_type="imessage_received", metadata={"text": "x"}))
        return await ctl.cancel()

    trace = asyncio.run(run())
    assert trace["state"] == "idle" and trace["steps"] == [] and ctl.recorder.raw == []
    assert [e.event_type for e in out["requeued"]] == ["imessage_received"]
