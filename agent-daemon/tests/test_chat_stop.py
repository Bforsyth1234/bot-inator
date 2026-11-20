"""#143: Stop a chat turn that is still running.

Every test drives a real :class:`Orchestrator` loop (``start`` -> ``_run`` ->
``_handle_event`` -> ``_run_chat_turn``) with the real approval wrapper
(``_wrap_tool_for_approval``). The model is a fake: either a scripted agent
whose ``run`` executes in the turn's worker thread (as ``agent.run`` does in
production) or the real smolagents agent with a scripted ``Model``. Memory is
an in-memory SQLite DB; no store under ~/.bot-inator is touched (conftest).
"""
from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
import time
import types
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import chat_turns, knowledge_retrieval
from ai import knowledge_index as ki
from ai.memory import Memory, _fallback_embed
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator, _make_mlx_model_class
from ai.taint import KNOWLEDGE_KIND
from events import remote_channel as remote_mod
from events.event_bus import ContextEvent, EventBus
from schemas.ws_messages import (
    ApprovalRequest, ApprovalResolved, ApprovalResponsePayload, Command, Thought,
)

QUESTION = "move my notes around please"
REPLY = "Here is the real answer."
EID = "msg_143stop"


# ---- helpers ----------------------------------------------------------------


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class NoteTool:
    """A side-effecting tool (``create_note``: not read-only, needs a card)."""

    name = "create_note"
    inputs = {"title": {"type": "string", "description": "title"}}

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.started = threading.Event()
        self.calls: list[str] = []
        self.finished: list[str] = []

    def forward(self, title: str = "") -> dict[str, Any]:
        self.calls.append(title)
        self.started.set()
        time.sleep(self.delay)
        self.finished.append(title)
        return {"status": "ok", "title": title}


class ScriptAgent:
    """Stands in for the smolagents agent: ``run`` executes ``script`` in the
    worker thread the orchestrator gives it."""

    def __init__(self, script: Callable[[str], Any]) -> None:
        self.script = script
        self.runs = 0
        self.interrupted = False

    def run(self, prompt: str, max_steps: Optional[int] = None) -> Any:
        self.runs += 1
        return self.script(prompt)

    def interrupt(self) -> None:
        self.interrupted = True


class Recorder:
    """approval_decisions / training_corpus / action_journal stand-ins."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def record(self, *args: Any, **kwargs: Any) -> Any:
        self.rows.append((args, kwargs))
        return {"label": "x"}


class Harness:
    def __init__(self, *, tools: tuple = (), approval_timeout: float = 5.0) -> None:
        self.bus = EventBus()
        engine = MLXEngine()
        engine.evaluate_event = AsyncMock(return_value="")
        self.mem = Memory(db_path=":memory:", embedder=lambda t: _fallback_embed(t))
        with patch.object(Orchestrator, "_build_agent", return_value=None):
            self.orch = Orchestrator(engine=engine, event_bus=self.bus, memory=self.mem,
                                     tools=list(tools), approval_timeout=approval_timeout)
        self.orch.idle_seconds = lambda: None  # no #117 countdown
        self.frames: list[Any] = []

        async def sub(msg: Any) -> None:
            self.frames.append(msg)

        self.orch.add_subscriber(sub)

    async def start(self, agent: Any) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        with patch.object(Orchestrator, "_build_agent", return_value=None):
            await self.orch.start()
        self.orch._agent = agent

    async def stop(self) -> None:
        await self.orch.stop()

    def wrapped(self, tool: Any) -> Any:
        return self.orch._wrap_tool_for_approval(tool)

    def push_chat(self, eid: str = EID, text: str = QUESTION, **meta: Any) -> None:
        self.bus.push_threadsafe(ContextEvent(
            event_type="user_message", metadata={"event_id": eid, "text": text, **meta}))

    def completes(self, eid: str = EID) -> list[str]:
        return [f.payload.content for f in self.frames
                if isinstance(f, Thought) and f.payload.event_id == eid
                and f.payload.stage == "complete"]

    def cards(self) -> list[Any]:
        return [f.payload for f in self.frames if isinstance(f, ApprovalRequest)]

    def resolved(self) -> list[Any]:
        return [f.payload for f in self.frames if isinstance(f, ApprovalResolved)]

    def log(self) -> list[tuple[str, str, str]]:
        return [(r["event_id"], r["role"], r["text"]) for r in self.mem.get_chat_log(50)]

    async def wait_stopped(self, eid: str = EID) -> str:
        await _until(lambda: self.completes(eid))
        await _until(lambda: any(r[0] == eid and r[1] == "assistant" for r in self.log()))
        return self.completes(eid)[-1]


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


# ---- 1. slow model: Stop -> "Stopped.", stopped row --------------------------


def test_stop_during_a_slow_model_ends_the_turn_with_a_stopped_row() -> None:
    async def main() -> None:
        h = Harness()
        thinking = threading.Event()

        def slow_model(prompt: str) -> str:
            thinking.set()
            time.sleep(0.6)  # the model is generating; can't be interrupted
            return REPLY

        agent = ScriptAgent(slow_model)
        await h.start(agent)
        try:
            h.push_chat()
            await _until(thinking.is_set)
            turn = h.orch._chat_turns[EID]
            task = turn.task
            assert h.orch.cancel_chat_turn(EID) is True
            await asyncio.sleep(0.05)
            # The turn stopped waiting at once; it is not done until the
            # worker thread has exited (nothing else may use the agent).
            assert not task.done()
            assert agent.interrupted
            text = await h.wait_stopped()
            assert text.startswith(chat_turns.STOPPED_TEXT)
            assert REPLY not in " ".join(h.completes())
            assert h.completes() == [text]
            assert h.log() == [(EID, "user", QUESTION), (EID, "assistant", text)]
            assert EID not in h.orch._chat_turns
            assert task.done() and not task.cancelled()
        finally:
            await h.stop()

    _run(main())


# ---- 2. an open card is withdrawn, nothing runs -----------------------------


def test_stop_while_a_card_is_open_withdraws_it_and_runs_nothing() -> None:
    async def main() -> None:
        tool = NoteTool()
        h = Harness(tools=(tool,))
        decisions, corpus, journal = Recorder(), Recorder(), Recorder()
        h.orch.approval_decisions = decisions
        h.orch.training_corpus = corpus
        h.orch.action_journal = journal
        gate = h.wrapped(tool)
        results: list[Any] = []

        def script(prompt: str) -> str:
            results.append(gate.forward(title="groceries"))
            return REPLY

        await h.start(ScriptAgent(script))
        try:
            h.push_chat()
            await _until(lambda: h.cards())
            card = h.cards()[0]
            assert card.event_id == EID
            assert h.orch.cancel_chat_turn(EID) is True
            text = await h.wait_stopped()
            # The card is closed in every client as withdrawn, not approved.
            outcomes = [(r.request_id, r.outcome) for r in h.resolved()]
            assert outcomes == [(card.request_id, "withdrawn")]
            assert tool.calls == []
            assert results == [{"skipped": True, "reason": "user_denied"}]
            assert "withdrawn" in text and "nothing on it ran" in text
            # A late Approve finds nothing to approve.
            late = ApprovalResponsePayload(request_id=card.request_id, approved=True)
            assert h.orch.submit_approval_response(late) is False
            await asyncio.sleep(0.05)
            assert tool.calls == []
            # Stores: not a decision by the user; the corpus says withdrawn.
            assert decisions.rows == []
            assert [kw["verdict"] for _a, kw in corpus.rows] == ["withdrawn"]
            assert journal.rows == []
            assert h.orch._pending == {} and h.orch._card_turn == {}
        finally:
            await h.stop()

    _run(main())


# ---- 3. unknown / finished id: no-op ----------------------------------------


def test_chat_cancel_for_an_unknown_id_does_nothing() -> None:
    async def main() -> None:
        h = Harness()
        await h.start(ScriptAgent(lambda p: REPLY))
        try:
            before = list(h.frames)
            assert h.orch.cancel_chat_turn("msg_nope") is False
            assert h.orch.cancel_chat_turn("") is False
            assert h.orch.cancel_chat_turn(None) is False
            # Through the WS command, as the app sends it.
            import main as daemon_main

            app = types.SimpleNamespace(state=types.SimpleNamespace(orchestrator=h.orch))
            cmd = Command.model_validate({
                "type": "command", "seq": 1, "timestamp": "2026-11-20T13:05:00Z",
                "payload": {"action": "chat_cancel", "message_id": "msg_nope"},
            })
            await daemon_main._handle_command(app, cmd)
            # A finished turn: still a no-op, and its reply stands.
            h.push_chat()
            await _until(lambda: h.completes())
            await _until(lambda: len(h.log()) == 2)
            await daemon_main._handle_command(app, Command.model_validate({
                "type": "command", "seq": 2, "timestamp": "2026-11-20T13:05:00Z",
                "payload": {"action": "chat_cancel", "message_id": EID},
            }))
            await asyncio.sleep(0.05)
            assert h.completes() == [REPLY]
            assert h.log() == [(EID, "user", QUESTION), (EID, "assistant", REPLY)]
            assert [f for f in h.frames[len(before):]
                    if not (isinstance(f, Thought) and f.payload.event_id == EID)] == []
        finally:
            await h.stop()

    _run(main())


# ---- 4. an approved, running call finishes; the reply says so ---------------


def test_an_approved_running_call_finishes_and_is_recorded() -> None:
    async def main() -> None:
        tool = NoteTool(delay=0.5)
        h = Harness(tools=(tool,))
        journal = Recorder()
        h.orch.action_journal = journal
        gate = h.wrapped(tool)
        second: list[Any] = []

        def script(prompt: str) -> str:
            gate.forward(title="first")
            # The model goes on after the Stop: refused, no card.
            second.append(gate.forward(title="second"))
            return REPLY

        await h.start(ScriptAgent(script))
        try:
            h.push_chat()
            await _until(lambda: h.cards())
            card = h.cards()[0]
            h.orch.submit_approval_response(
                ApprovalResponsePayload(request_id=card.request_id, approved=True))
            await _until(tool.started.is_set)
            assert h.orch.cancel_chat_turn(EID) is True
            text = await h.wait_stopped()
            assert tool.finished == ["first"]          # not cut in half
            assert [a[0] for a, _kw in journal.rows] == ["create_note"]  # in History
            assert "create_note was already approved and running" in text
            assert "completed" in text
            assert second == [chat_turns.REFUSAL]
            assert len(h.cards()) == 1 and tool.calls == ["first"]
        finally:
            await h.stop()

    _run(main())


def test_approved_but_not_started_when_stopped_never_runs() -> None:
    """Approve and Stop land together: the approval was the user's (it is
    recorded), but the call had not started, so it does not run."""
    async def main() -> None:
        tool = NoteTool()
        h = Harness(tools=(tool,))
        decisions, journal = Recorder(), Recorder()
        h.orch.approval_decisions = decisions
        h.orch.action_journal = journal
        gate = h.wrapped(tool)
        results: list[Any] = []

        def script(prompt: str) -> str:
            results.append(gate.forward(title="late"))
            return REPLY

        await h.start(ScriptAgent(script))
        try:
            h.push_chat()
            await _until(lambda: h.cards())
            card = h.cards()[0]
            assert h.orch.submit_approval_response(
                ApprovalResponsePayload(request_id=card.request_id, approved=True))
            assert h.orch.cancel_chat_turn(EID) is True  # same tick
            text = await h.wait_stopped()
            assert tool.calls == [] and journal.rows == []
            assert results == [chat_turns.REFUSAL]
            assert [a[2] for a, _kw in decisions.rows] == ["approved"]
            assert [r.outcome for r in h.resolved()] == ["approved"]
            assert text == "Stopped. Nothing was changed."
        finally:
            await h.stop()

    _run(main())


# ---- 5. a model that calls a tool after the Stop (real smolagents) ----------


def _slow_scripted_model(release: threading.Event, generating: threading.Event):
    from smolagents.models import (
        ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, Model,
    )

    class Slow(Model):
        def __init__(self) -> None:
            super().__init__(model_id="fake/slow")
            self.calls = 0

        def generate(self, messages, stop_sequences=None, response_format=None,
                     tools_to_call_from=None, **kwargs):
            self.calls += 1
            generating.set()
            release.wait(10)  # mid-generation when the user presses Stop
            call = ChatMessageToolCall(
                function=ChatMessageToolCallFunction(
                    name="create_note", arguments={"title": f"n{self.calls}", "body": "b"}),
                id=f"call_{self.calls}", type="function",
            )
            return ChatMessage(role="assistant", content="", tool_calls=[call])

    return Slow()


def test_a_tool_call_the_model_makes_after_stop_never_runs() -> None:
    from evals.suites import tool_calls as tc
    from tools import AVAILABLE_TOOLS

    async def main() -> None:
        calls: list[dict] = []
        stub = tc.stub_tools([t for t in AVAILABLE_TOOLS if t.name == "create_note"], calls)
        h = Harness(tools=tuple(stub))
        release, generating = threading.Event(), threading.Event()
        model = _slow_scripted_model(release, generating)
        h.orch._build_smolagents_model = lambda: model  # type: ignore[method-assign]
        await h.start(None)
        h.orch._agent = h.orch._build_agent()  # the real NoRepeatToolCallingAgent
        try:
            h.push_chat()
            await _until(generating.is_set)
            assert h.orch.cancel_chat_turn(EID) is True
            release.set()  # the generation "returns" a create_note call
            text = await h.wait_stopped()
            assert calls == []          # the gate refused it
            assert h.cards() == []      # and no card was raised
            assert model.calls == 1     # the run ended: no further step
            observed = " ".join(str(getattr(s, "observations", "") or "")
                                for s in h.orch._agent.memory.steps)
            assert chat_turns.REFUSAL["message"] in observed  # refused at the gate
            assert text.startswith(chat_turns.STOPPED_TEXT)
        finally:
            release.set()
            await h.stop()

    _run(main())


def test_mlx_adapter_stops_between_tokens_and_drops_the_result(monkeypatch) -> None:
    """The local model runs in the worker thread; Stop breaks the token loop
    and nothing it generated comes back (so it can't call a tool)."""
    produced: list[int] = []

    def stream_generate(model, tokenizer, prompt, **kw):
        for i in range(500):
            time.sleep(0.005)
            produced.append(i)
            yield types.SimpleNamespace(text='<tool_call>{"name": "create_note"}' if i == 0 else "x")

    monkeypatch.setitem(sys.modules, "mlx_lm", types.SimpleNamespace(stream_generate=stream_generate))
    engine = types.SimpleNamespace(
        model_name="fake/mlx", loaded=True, model=object(),
        tokenizer=types.SimpleNamespace(apply_chat_template=lambda msgs, **kw: [1, 2, 3]),
        generation_lock=threading.Lock(), _maybe_clear_metal_cache_locked=lambda: None,
    )
    model = _make_mlx_model_class()(engine)
    turn = chat_turns.ChatTurn("msg_mlx")
    outcome: dict[str, Any] = {}

    def worker() -> None:
        chat_turns.bind(turn)
        try:
            outcome["msg"] = model.generate([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        except chat_turns.TurnStopped as exc:
            outcome["stopped"] = exc

    ctx = contextvars.copy_context()
    t = threading.Thread(target=ctx.run, args=(worker,))
    t.start()
    end = time.monotonic() + 5
    while len(produced) < 20 and time.monotonic() < end:
        time.sleep(0.005)
    turn.request_stop()
    t.join(5)
    assert "stopped" in outcome and "msg" not in outcome
    assert len(produced) < 200  # the loop broke; it did not run to the end


# ---- 6. the next message after a Stop runs normally --------------------------


def test_the_next_message_after_a_stop_runs_normally() -> None:
    async def main() -> None:
        h = Harness()
        thinking = threading.Event()

        def script(prompt: str) -> str:
            if "first question" in prompt:
                thinking.set()
                time.sleep(0.3)
                return "late first answer"
            return "second answer"

        await h.start(ScriptAgent(script))
        try:
            h.push_chat("msg_one", "first question")
            await _until(thinking.is_set)
            h.orch.cancel_chat_turn("msg_one")
            h.push_chat("msg_two", "second question")
            await _until(lambda: h.completes("msg_two"))
            assert h.completes("msg_two") == ["second answer"]
            assert h.completes("msg_one")[0].startswith(chat_turns.STOPPED_TEXT)
            await _until(lambda: len(h.log()) == 4)
            assert [r[1:] for r in h.log()] == [
                ("user", "first question"), ("assistant", h.completes("msg_one")[0]),
                ("user", "second question"), ("assistant", "second answer"),
            ]
        finally:
            await h.stop()

    _run(main())


def test_a_stop_for_a_queued_message_ends_it_when_it_comes_up() -> None:
    async def main() -> None:
        h = Harness()
        thinking = threading.Event()

        def script(prompt: str) -> str:
            thinking.set()
            time.sleep(0.3)
            return "answer one"

        agent = ScriptAgent(script)
        await h.start(agent)
        try:
            h.push_chat("msg_one", "first question")
            await _until(thinking.is_set)
            h.push_chat("msg_two", "second question")
            assert h.orch.cancel_chat_turn("msg_two") is False  # not running yet
            await _until(lambda: h.completes("msg_two"))
            assert h.completes("msg_one") == ["answer one"]
            assert h.completes("msg_two")[0].startswith(chat_turns.STOPPED_TEXT)
            assert agent.runs == 1  # the queued turn never reached the model
            await _until(lambda: len(h.log()) == 4)
            assert ("msg_two", "user", "second question") in h.log()
        finally:
            await h.stop()

    _run(main())


# ---- 7. an ambient event and a chat turn never interleave per-turn state -----


def _snippets() -> knowledge_retrieval.Retrieval:
    return knowledge_retrieval.Retrieval(snippets=[{
        "source": "mail", "title": "Lease renewal", "date": "2026-11-03",
        "text": "Your lease renews on January 1, 2027."}])


def test_ambient_event_waits_for_the_chat_turn_and_its_worker(monkeypatch) -> None:
    """A tainted chat turn (retrieved snippets) is stopped while its worker
    still runs; an iMessage event queued meanwhile must not begin its turn
    (which resets the shield and the turn fields) until that worker exited.
    The worker sees its own turn's taint and kind to the end, and the
    iMessage turn starts clean of the chat's retrieved text."""
    async def main() -> None:
        h = Harness()
        seen: dict[str, Any] = {}
        thinking = threading.Event()
        timeline: list[tuple[str, float]] = []

        async def for_chat(orch: Any, text: str, event_id: str = "") -> Any:
            return _snippets()

        monkeypatch.setattr(knowledge_retrieval, "for_chat", for_chat)
        shield = h.orch._taint
        real_begin = shield.begin_turn

        def begin(kind: str = "", **kw: Any) -> None:
            timeline.append(("begin:" + kind + ":" + str(kw.get("event_type", "")), time.monotonic()))
            real_begin(kind, **kw)

        shield.begin_turn = begin  # type: ignore[method-assign]

        def script(prompt: str) -> str:
            if "Lease" in prompt:  # the chat turn
                thinking.set()
                seen["chat_start"] = (h.orch._turn_kind, set(shield.read_kinds()))
                time.sleep(0.4)
                seen["chat_end"] = (h.orch._turn_kind, set(shield.read_kinds()))
                timeline.append(("chat_worker_exit", time.monotonic()))
                return REPLY
            seen["ambient"] = (h.orch._turn_kind, set(shield.read_kinds()))
            return "no action"

        await h.start(ScriptAgent(script))
        try:
            h.push_chat(text="When does my lease renew?")
            await _until(thinking.is_set)
            h.bus.push_threadsafe(ContextEvent(event_type="imessage_received", metadata={
                "event_id": "evt_ambient", "sender": "+15550100", "text": "hey are you free"}))
            await asyncio.sleep(0.05)
            h.orch.cancel_chat_turn(EID)
            await _until(lambda: "ambient" in seen)
            assert seen["chat_start"] == ("chat", {KNOWLEDGE_KIND})
            assert seen["chat_end"] == ("chat", {KNOWLEDGE_KIND})  # not cleared under it
            assert seen["ambient"][0] == "event"
            assert KNOWLEDGE_KIND not in seen["ambient"][1]          # no leak onto it
            assert "message" in seen["ambient"][1]
            exit_at = dict(timeline)["chat_worker_exit"]
            ambient_begin = [t for name, t in timeline if name.endswith(":imessage_received")]
            assert ambient_begin and ambient_begin[0] >= exit_at
        finally:
            await h.stop()

    _run(main())


# ---- 8. a phone turn that is stopped ends the phone turn ---------------------


class FakeRemote:
    timeout_s = 900.0

    def __init__(self) -> None:
        self.mirrored: list[str] = []
        self.forgot: list[str] = []
        self.replies: list[tuple[str, str]] = []

    def mirror_approval(self, request_id: str, *a: Any, **kw: Any) -> str:
        self.mirrored.append(request_id)
        return "a"

    def forget(self, request_id: str) -> None:
        self.forgot.append(request_id)

    def send_reply(self, event_id: str, reply: str) -> bool:
        self.replies.append((event_id, reply))
        return True


def test_a_stopped_phone_turn_ends_the_phone_turn_and_kills_the_code() -> None:
    async def main() -> None:
        tool = NoteTool()
        h = Harness(tools=(tool,))
        remote = FakeRemote()
        h.orch.remote_channel = remote
        gate = h.wrapped(tool)
        phone_active: list[bool] = []

        def script(prompt: str) -> str:
            phone_active.append(remote_mod.phone_turn_active())
            gate.forward(title="from the phone")
            return REPLY

        await h.start(ScriptAgent(script))
        try:
            h.push_chat(source=remote_mod.REMOTE_SOURCE)
            await _until(lambda: h.cards())
            card = h.cards()[0]
            assert remote.mirrored == [card.request_id]
            h.orch.cancel_chat_turn(EID)
            text = await h.wait_stopped()
            assert phone_active == [True]
            assert remote_mod.phone_turn_active() is False   # the finally ran
            assert h.orch._turn_from_phone is False
            assert remote.forgot == [card.request_id]        # "yes a" does nothing now
            assert remote.replies == [(EID, text)]
            assert tool.calls == []
        finally:
            await h.stop()

    _run(main())


# ---- 9. knowledge retrieval: taint released, indexed honestly ---------------


class FakeIndex:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str, str, bool]] = []

    def index_chat_turn(self, event_id: str, user_text: str, reply_text: str = "",
                        *, date: str = "", keep_reply: bool = True) -> bool:
        self.turns.append((event_id, user_text, reply_text, keep_reply))
        return True


def test_a_stopped_retrieval_turn_is_indexed_without_a_reply(monkeypatch) -> None:
    async def main() -> None:
        h = Harness()
        index = FakeIndex()
        h.orch.knowledge_index = index
        retrieve = {"on": True}

        async def for_chat(orch: Any, text: str, event_id: str = "") -> Any:
            return _snippets() if retrieve["on"] else knowledge_retrieval.Retrieval()

        monkeypatch.setattr(knowledge_retrieval, "for_chat", for_chat)
        thinking = threading.Event()
        kinds: list[set] = []

        def script(prompt: str) -> str:
            kinds.append(set(h.orch._taint.read_kinds()))
            if "lease" in prompt:
                thinking.set()
                time.sleep(0.2)
            return REPLY

        await h.start(ScriptAgent(script))
        try:
            h.push_chat(text="when does my lease renew")
            await _until(thinking.is_set)
            h.orch.cancel_chat_turn(EID)
            text = await h.wait_stopped()
            # The question only; "Stopped." is never indexed as a reply, now
            # or by a later background pass over chat_log.
            assert index.turns == [(EID, "when does my lease renew", "", False)]
            listing = ki.chat_listing(h.mem, kept_out=[EID])
            item = next(i for i in listing.items if i.item_id == EID)
            assert "when does my lease renew" in item.text and text not in item.text
            # The next turn starts clean: the stopped turn's retrieved text
            # (its taint) is gone, and knowledge no longer blocks its cards.
            retrieve["on"] = False
            h.push_chat("msg_next", "what time is it")
            await _until(lambda: h.completes("msg_next"))
            assert kinds == [{KNOWLEDGE_KIND}, set()]
            assert knowledge_retrieval.used_this_turn(h.orch._taint) is False
            assert index.turns[-1] == ("msg_next", "what time is it", REPLY, True)
        finally:
            await h.stop()

    _run(main())


# ---- 10. double Stop is harmless ---------------------------------------------


def test_pressing_stop_twice_is_harmless() -> None:
    async def main() -> None:
        tool = NoteTool()
        h = Harness(tools=(tool,))
        gate = h.wrapped(tool)

        def script(prompt: str) -> str:
            gate.forward(title="x")
            time.sleep(0.2)
            return REPLY

        await h.start(ScriptAgent(script))
        try:
            h.push_chat()
            await _until(lambda: h.cards())
            assert h.orch.cancel_chat_turn(EID) is True
            assert h.orch.cancel_chat_turn(EID) is False
            await asyncio.sleep(0.05)
            assert h.orch.cancel_chat_turn(EID) is False
            text = await h.wait_stopped()
            await asyncio.sleep(0.3)
            assert h.completes() == [text]
            assert [r[1] for r in h.log()] == ["user", "assistant"]
            assert [r.outcome for r in h.resolved()] == ["withdrawn"]
            assert h.orch.cancel_chat_turn(EID) is False   # finished
        finally:
            await h.stop()

    _run(main())


# ---- 11. the local time in the chat prompt (#144 leftover) -----------------


def test_chat_prompt_carries_the_users_local_time(monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    from ai import orchestrator as orch_mod

    event = ContextEvent(event_type="user_message", metadata={"text": "how many emails since 12 pm"})
    fixed = datetime(2026, 11, 20, 13, 5, 42, tzinfo=timezone(timedelta(hours=-5)))
    monkeypatch.setattr(orch_mod, "_local_now", lambda: fixed)
    prompt = Orchestrator._build_chat_prompt(event)
    line = "It is now Friday 2026-11-20T13:05-05:00 (the user's local time)."
    assert line in prompt
    # Right after the line quoting the user's text.
    assert '"how many emails since 12 pm"\n' + line + "\n\n" in prompt
    later = datetime(2026, 11, 21, 9, 30, tzinfo=timezone(timedelta(hours=1)))
    monkeypatch.setattr(orch_mod, "_local_now", lambda: later)
    assert "It is now Saturday 2026-11-21T09:30+01:00" in Orchestrator._build_chat_prompt(event)


def test_now_line_uses_the_real_local_clock() -> None:
    from ai.orchestrator import _now_line

    line = _now_line()
    now = datetime_now = __import__("datetime").datetime.now().astimezone()
    assert line.startswith(f"It is now {datetime_now.strftime('%A')} {now.date().isoformat()}T")
    assert line.endswith("(the user's local time).")


# ---- pure helpers ----------------------------------------------------------


def test_stopped_text_and_early_stops() -> None:
    turn = chat_turns.ChatTurn("e")
    assert chat_turns.stopped_text(turn) == "Stopped. Nothing was changed."
    turn.note_withdrawn("create_note")
    assert chat_turns.stopped_text(turn).startswith("Stopped. The approval card was withdrawn")
    assert turn.request_stop() and not turn.request_stop()
    assert not turn.mark_answered()
    answered = chat_turns.ChatTurn("f")
    assert answered.mark_answered() and not answered.request_stop()
    clock = {"t": 0.0}
    early = chat_turns.EarlyStops(clock=lambda: clock["t"])
    for i in range(chat_turns.MAX_EARLY_STOPS + 5):
        early.add(f"id{i}")
    assert len(early) == chat_turns.MAX_EARLY_STOPS and not early.take("id0")
    assert early.take(f"id{chat_turns.MAX_EARLY_STOPS + 4}")
    early.add("old")
    clock["t"] = chat_turns.EARLY_STOP_TTL_S + 1
    assert not early.take("old")


@pytest.mark.parametrize("verdict", ["withdrawn"])
def test_stores_accept_withdrawn(verdict: str) -> None:
    from ai import idea_outcomes, training_corpus

    assert verdict in idea_outcomes.VERDICTS and verdict in training_corpus.VERDICTS
    assert verdict not in training_corpus.TRAINABLE_VERDICTS
