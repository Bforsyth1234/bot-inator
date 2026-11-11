"""Tests for prepared offers (todo #76): read-only preparation, bundled
approval card, and running only the ticked actions after approval."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import prepared_offers as po
from events.event_bus import ContextEvent
from schemas.ws_messages import ApprovalRequest, ApprovalRequestPayload

READ_ONLY = frozenset({"summarize_file", "find_files", "read_email"})

INVOICE_TEXT = (
    "ACME Hosting Ltd\nInvoice #4471\nInvoice date: 2026-11-01\n"
    "Amount due: $1,234.50\nDue date: November 20, 2026\nThank you."
)


class FakeTools(dict):
    """Name -> callable; records every call, gated or not."""

    def __init__(self, **overrides: Any) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        defaults = {
            "summarize_file": lambda path, max_chars=2000: {"status": "ok", "preview": INVOICE_TEXT},
            "find_files": lambda query, folder="", kind="", limit=20: {"status": "ok", "results": []},
            "read_email": lambda message_id: {"status": "error", "error": "none"},
            "move_file": lambda src, dst_dir: {"status": "ok", "new_path": f"{dst_dir}/{Path(src).name}"},
            "create_reminder": lambda title, due="", list_name="", notes="": {"status": "ok", "title": title},
            "create_event": lambda title, start, end="", calendar="", location="", notes="": {"status": "ok"},
        }
        defaults.update(overrides)
        for name, fn in defaults.items():
            self[name] = self._record(name, fn)

    def _record(self, name: str, fn: Any) -> Any:
        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            return fn(**kwargs)
        # Keep the signature visible for argument allow-listing.
        call.__signature__ = __import__("inspect").signature(fn)  # type: ignore[attr-defined]
        return call

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def _evaluator(reply: Any, calls: list[str]):
    async def evaluate(prompt: str) -> str:
        calls.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)
    return evaluate


@pytest.fixture
def home(tmp_path: Path) -> dict[str, Path]:
    downloads = tmp_path / "Downloads"
    invoices = tmp_path / "Documents" / "Invoices"
    downloads.mkdir()
    invoices.mkdir(parents=True)
    doc = downloads / "acme_invoice_4471.txt"
    doc.write_text(INVOICE_TEXT)
    return {"downloads": downloads, "invoices": invoices, "doc": doc}


def _doc_event(path: Path) -> ContextEvent:
    return ContextEvent(event_type="file", metadata={"path": str(path), "action": "created"})


def _idea(trigger: str = po.DOCUMENT_TRIGGER) -> dict[str, Any]:
    return {"tool_name": po.DOCUMENT_IDEA_NAME, "builtin_tool": "move_file", "trigger": trigger,
            "source": "next_step", "description": po.DOCUMENT_FOLLOW_UP}


def _preparer(tools: FakeTools, prompts: list[str], reply: Any = None, **kw: Any) -> po.Preparer:
    kw.setdefault("redact", lambda role: False)
    kw.setdefault("history", lambda: [])
    return po.Preparer(
        evaluate=_evaluator(reply if reply is not None else {"vendor": "ACME Hosting"}, prompts),
        tools=tools, read_only=READ_ONLY, **kw,
    )


def _similar(invoices: Path) -> Any:
    def find(query, folder="", kind="", limit=20):
        return {"status": "ok", "results": [
            {"path": str(invoices / "old_invoice_1.pdf")},
            {"path": str(invoices / "old_invoice_2.pdf")},
        ]}
    return find


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---- classification ---------------------------------------------------------


@pytest.mark.parametrize("path,kind", [
    ("/Users/me/Downloads/Invoice-ACME-2026.pdf", "invoice"),
    ("/Users/me/Downloads/receipt_123.pdf", "receipt"),
    ("/Users/me/Downloads/Bank Statement Oct.pdf", "statement"),
    ("/Users/me/Downloads/eticket-LHR.pdf", "ticket"),
    ("/Users/me/Documents/invoice.pdf", None),  # not in Downloads
    ("/Users/me/Downloads/invoice.pdf.crdownload", None),
    ("/Users/me/Downloads/holiday.pdf", None),
    ("/Users/me/Downloads/invoice.zip", None),
])
def test_document_kind(path: str, kind: Any) -> None:
    assert po.document_kind(path) == kind


def test_extraction_helpers() -> None:
    assert po.find_due_date(INVOICE_TEXT) == "2026-11-20"
    assert po.find_amount(INVOICE_TEXT) == "$1,234.50"
    assert po.find_due_date("Payment due 2026-12-01.") == "2026-12-01"
    assert po.find_due_date("Due: 20 Nov 2026") == "2026-11-20"
    assert po.find_due_date("Issued 2026-11-01, no deadline") is None
    assert po.vendor_from_name("/x/Downloads/acme_invoice_4471.pdf") == "Acme"


# ---- (a) invoice -> bundled offer ---------------------------------------------


def test_invoice_file_gives_bundle_with_name_folder_due_and_both_actions(home) -> None:
    tools = FakeTools(find_files=_similar(home["invoices"]))
    prompts: list[str] = []
    prep = _preparer(tools, prompts)
    idea = _run(prep.prepare(_idea(), _doc_event(home["doc"])))

    offer = idea[po.PREPARED_KEY]
    facts = offer["facts"]
    assert facts["vendor"] == "ACME Hosting"
    assert facts["amount"] == "$1,234.50"
    assert facts["due_date"] == "2026-11-20"
    assert facts["file_name"] == "2026-11-20 ACME Hosting invoice.txt"
    assert facts["folder"] == str(home["invoices"])
    assert [a["tool"] for a in offer["actions"]] == ["move_file", "create_reminder"]
    move, remind = offer["actions"]
    assert move["args"] == {"src": str(home["doc"]), "dst_dir": str(home["invoices"])}
    assert remind["args"]["due"] == "2026-11-20"
    assert remind["args"]["title"] == "Pay ACME Hosting invoice"
    assert "$1,234.50" in remind["args"]["notes"]
    assert all(a["enabled"] for a in offer["actions"])
    assert "remind you on 2026-11-20" in offer["headline"]
    assert idea["card_text"] == offer["headline"]
    # Budget: one model call; nothing gated ran before approval.
    assert prep.model_calls == 1 and len(prompts) == 1
    assert set(tools.names()) <= READ_ONLY
    assert "move_file" not in tools.names() and "create_reminder" not in tools.names()

    card = po.PreparedOffer.from_dict(offer).to_card()
    assert card["tool_name"] == po.PREPARED_TOOL_NAME
    payload = ApprovalRequestPayload(request_id="r", event_id="e", **card)
    assert [a.tool for a in payload.actions or []] == ["move_file", "create_reminder"]


def test_pdf_text_is_read_locally(home) -> None:
    pdf = home["downloads"] / "Invoice-Acme.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    tools = FakeTools(find_files=_similar(home["invoices"]))
    read: list[str] = []
    prep = _preparer(tools, [], read_pdf=lambda p: read.append(p) or INVOICE_TEXT)
    idea = _run(prep.prepare(_idea(), _doc_event(pdf)))
    assert read == [str(pdf)]
    assert idea[po.PREPARED_KEY]["facts"]["due_date"] == "2026-11-20"
    assert "summarize_file" not in tools.names()


def test_history_destination_wins_over_find_files(home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    taxes = tmp_path / "Documents" / "Taxes"
    taxes.mkdir()
    rows = [{"event_type": "file", "ext": ".txt", "from_folder": "~/Downloads",
             "folder": "~/Documents/Taxes", "action": "renamed"}] * 2
    tools = FakeTools(find_files=_similar(home["invoices"]))
    prep = _preparer(tools, [], history=lambda: rows)
    idea = _run(prep.prepare(_idea(), _doc_event(home["doc"])))
    assert idea[po.PREPARED_KEY]["facts"]["folder"] == str(taxes)
    assert "find_files" not in tools.names()


def test_no_folder_and_no_due_date_is_plain_offer(home) -> None:
    home["doc"].write_text("ACME invoice, thanks for your business.")
    tools = FakeTools(summarize_file=lambda path, max_chars=2000: {"status": "ok", "preview": "ACME invoice"})
    idea = _idea()
    out = _run(_preparer(tools, [], reply={"vendor": "ACME"}).prepare(idea, _doc_event(home["doc"])))
    assert out is idea and po.PREPARED_KEY not in out


# ---- failures -> plain offer --------------------------------------------------


@pytest.mark.parametrize("case", ["model_raises", "tool_raises", "timeout"])
def test_preparation_failure_falls_back_to_plain_offer(home, case) -> None:
    overrides: dict[str, Any] = {"find_files": _similar(home["invoices"])}
    reply: Any = {"vendor": "ACME"}
    budget = po.BUDGET_SECONDS
    if case == "model_raises":
        reply = RuntimeError("engine down")
    elif case == "tool_raises":
        def boom(path, max_chars=2000):
            raise OSError("unreadable")
        overrides["summarize_file"] = boom
    tools = FakeTools(**overrides)
    prompts: list[str] = []
    prep = _preparer(tools, prompts, reply=reply, budget_seconds=budget)
    if case == "timeout":
        async def slow(prompt: str) -> str:
            await asyncio.sleep(5)
            return "{}"
        prep._evaluate = slow
        prep.budget_seconds = 0.05
    idea = _idea()
    out = _run(prep.prepare(idea, _doc_event(home["doc"])))
    assert out is idea and po.PREPARED_KEY not in out
    assert "move_file" not in tools.names() and "create_reminder" not in tools.names()


def test_gated_tool_is_refused_during_preparation() -> None:
    tools = FakeTools()
    guard = po.ReadOnlyTools(tools, READ_ONLY)
    with pytest.raises(po.GatedToolError):
        guard.call("move_file", src="/a", dst_dir="/b")
    assert tools.calls == []
    # The real READ_ONLY_TOOL_NAMES are the default allow-list.
    with pytest.raises(po.GatedToolError):
        po.ReadOnlyTools(tools).call("create_reminder", title="x")
    assert tools.calls == []


def test_gated_step_skips_preparation(home) -> None:
    """A preparation that would need a gated tool gives the plain offer."""
    tools = FakeTools()
    prep = po.Preparer(evaluate=None, tools=tools, read_only=frozenset({"find_files"}),
                       redact=lambda r: False, history=lambda: [])
    idea = _idea()
    assert _run(prep.prepare(idea, _doc_event(home["doc"]))) is idea
    assert tools.calls == []


# ---- privacy -----------------------------------------------------------------


def test_cloud_engine_skips_model_but_keeps_local_extraction(home) -> None:
    tools = FakeTools(find_files=_similar(home["invoices"]))
    prompts: list[str] = []
    prep = _preparer(tools, prompts, redact=lambda role: True)
    idea = _run(prep.prepare(_idea(), _doc_event(home["doc"])))
    assert prompts == [] and prep.model_calls == 0
    facts = idea[po.PREPARED_KEY]["facts"]
    assert facts["due_date"] == "2026-11-20" and facts["vendor"] == "Acme"


def test_cloud_engine_mail_is_plain_offer() -> None:
    tools = FakeTools()
    prompts: list[str] = []
    prep = _preparer(tools, prompts, redact=lambda role: True)
    idea = _idea("mail_invite")
    assert _run(prep.prepare(idea, _mail())) is idea
    assert prompts == [] and tools.calls == []


# ---- (b) mail -> filled create_event / create_reminder ----------------------------


def _mail(mid: int = 42) -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": "Sam <sam@example.com>", "subject": "Invitation: Design review",
        "message_id": mid, "mailbox": "INBOX"})


def _mail_tools() -> FakeTools:
    return FakeTools(read_email=lambda message_id: {
        "status": "ok", "sender": "Sam <sam@example.com>", "subject": "Invitation: Design review",
        "date": "2026-11-10", "body": "Join us Thursday 2026-11-12 14:00-15:00 in Room 4."})


def test_mail_invite_fills_create_event() -> None:
    tools = _mail_tools()
    prompts: list[str] = []
    reply = {"title": "Design review", "start": "2026-11-12T14:00", "end": "2026-11-12T15:00",
             "location": "Room 4", "attendees": ["Sam", "Lee"]}
    idea = _run(_preparer(tools, prompts, reply=reply).prepare(_idea("mail_invite"), _mail()))
    offer = idea[po.PREPARED_KEY]
    assert len(offer["actions"]) == 1
    args = offer["actions"][0]["args"]
    assert args == {"title": "Design review", "start": "2026-11-12T14:00", "end": "2026-11-12T15:00",
                    "location": "Room 4", "notes": "Attendees: Sam, Lee"}
    card = po.PreparedOffer.from_dict(offer).to_card()
    assert card["tool_name"] == "create_event" and card["tool_args"]["kwargs"] == args
    assert tools.names() == ["read_email"] and len(prompts) == 1


def test_mail_deadline_fills_create_reminder() -> None:
    tools = _mail_tools()
    reply = {"title": "Send the signed contract", "due": "2026-11-14"}
    idea = _run(_preparer(tools, [], reply=reply).prepare(_idea("mail_deadline"), _mail()))
    action = idea[po.PREPARED_KEY]["actions"][0]
    assert action["tool"] == "create_reminder"
    assert action["args"] == {"title": "Send the signed contract", "due": "2026-11-14"}


def test_mail_without_a_date_is_plain_offer() -> None:
    idea = _idea("mail_invite")
    assert _run(_preparer(_mail_tools(), [], reply={}).prepare(idea, _mail())) is idea


# ---- after approval: only ticked actions run ------------------------------------


def _bundle(home) -> dict[str, Any]:
    tools = FakeTools(find_files=_similar(home["invoices"]))
    return _run(_preparer(tools, []).prepare(_idea(), _doc_event(home["doc"])))[po.PREPARED_KEY]


def _returned(offer: dict[str, Any], enabled: list[bool], **edits: Any) -> dict[str, Any]:
    actions = []
    for a, on in zip(offer["actions"], enabled):
        args = dict(a["args"])
        args.update(edits.get(a["tool"], {}))
        actions.append({"tool": a["tool"], "args": args, "enabled": on, "label": a["label"]})
    return {"actions": actions}


def test_unticking_reminder_runs_only_the_move(home) -> None:
    offer = _bundle(home)
    tools = FakeTools()
    results = po.run_approved_actions(offer, _returned(offer, [True, False]), tools)
    assert tools.names() == ["move_file"]
    assert [r["tool"] for r in results] == ["move_file"]


def test_unticking_move_runs_only_the_reminder(home) -> None:
    offer = _bundle(home)
    tools = FakeTools()
    po.run_approved_actions(offer, _returned(offer, [False, True]), tools)
    assert tools.names() == ["create_reminder"]


def test_no_edits_runs_both(home) -> None:
    offer = _bundle(home)
    tools = FakeTools()
    po.run_approved_actions(offer, None, tools)
    assert tools.names() == ["move_file", "create_reminder"]


def test_edits_apply_but_tools_cannot_be_swapped_or_added(home) -> None:
    offer = _bundle(home)
    tools = FakeTools()
    returned = _returned(offer, [True, True],
                         create_reminder={"title": "Pay ACME", "evil": "x"})
    returned["actions"][0]["tool"] = "send_imessage"  # swap attempt: ignored
    returned["actions"].append({"tool": "send_imessage", "args": {}, "enabled": True})
    po.run_approved_actions(offer, returned, tools)
    assert tools.names() == ["move_file", "create_reminder"]
    reminder_kwargs = tools.calls[1][1]
    assert reminder_kwargs["title"] == "Pay ACME" and "evil" not in reminder_kwargs


def test_single_action_card_takes_plain_kwargs_edits() -> None:
    tools = _mail_tools()
    reply = {"title": "Design review", "start": "2026-11-12T14:00"}
    offer = _run(_preparer(tools, [], reply=reply).prepare(_idea("mail_invite"), _mail()))[po.PREPARED_KEY]
    run = FakeTools()
    po.run_approved_actions(offer, {"args": [], "kwargs": {"title": "Review (moved)",
                                                           "start": "2026-11-12T14:00"}}, run)
    assert run.calls == [("create_event", {"title": "Review (moved)", "start": "2026-11-12T14:00"})]


def test_failing_action_does_not_stop_the_next(home) -> None:
    offer = _bundle(home)
    def broken(src, dst_dir):
        raise OSError("disk full")
    tools = FakeTools(move_file=broken)
    results = po.run_approved_actions(offer, None, tools)
    assert results[0]["result"]["status"] == "error"
    assert tools.names() == ["move_file", "create_reminder"]


# ---- wire format ------------------------------------------------------------------


def test_approval_request_actions_round_trip_and_default_none() -> None:
    plain = ApprovalRequestPayload(request_id="r", event_id="e", tool_name="open_url",
                                   tool_args={}, reasoning="x")
    assert plain.actions is None
    msg = ApprovalRequest(seq=1, timestamp="2026-11-10T00:00:00Z", payload=ApprovalRequestPayload(
        request_id="r", event_id="e", tool_name=po.PREPARED_TOOL_NAME, tool_args={}, reasoning="x",
        actions=[{"tool": "move_file", "args": {"src": "/a", "dst_dir": "/b"}},
                 {"tool": "create_reminder", "args": {"title": "t"}, "enabled": False}]))
    back = ApprovalRequest.model_validate_json(msg.model_dump_json())
    assert [(a.tool, a.enabled) for a in back.payload.actions or []] == [
        ("move_file", True), ("create_reminder", False)]
