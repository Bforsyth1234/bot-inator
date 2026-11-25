"""#151: a generated tool's cards say up front what it needs to work.

Covers the source scan (``ai.tool_needs.scan_source``), the idea-text guess,
the permission state, the dry run classing an unfixable refusal as a need
(not a problem for the drafter), and both card payloads carrying ``needs``.
The scan only explains: nothing here may set trust (invariant 7b, #104).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import permissions, tool_needs
from schemas.ws_messages import (
    ApprovalRequestPayload,
    CodeApprovalRequest,
    CodeApprovalRequestPayload,
    CodeApprovalResponsePayload,
)
from tests.test_wave13_wiring import _approval_orch
from tests.test_wave15_wiring import _cards
from tools import draft_dry_run as dr
from tools import tool_sandbox


def _tool(body: str, name: str = "t") -> str:
    return (
        "import subprocess\nfrom smolagents import tool\n\n\n@tool\n"
        f"def {name}() -> dict:\n"
        '    """Quit Spotify, read contacts, write ~/Library (docstrings are not scanned).\n\n'
        "    Returns:\n        dict.\n    \"\"\"\n"
        + body
        + '    return {"status": "ok"}\n'
    )


QUIT_SPOTIFY = _tool(
    "    subprocess.run(['osascript', '-e', 'tell application \"Spotify\" to quit'], check=False)\n"
)
READ_CONTACTS = _tool(
    "    from Contacts import CNContactStore\n"
    "    store = CNContactStore.alloc().init()\n"
)
DOCUMENTS_TOOL = _tool(
    "    from pathlib import Path\n"
    "    p = Path.home() / 'Documents' / 'bot-inator' / 'list.txt'\n"
    "    p.parent.mkdir(parents=True, exist_ok=True)\n"
    "    p.write_text('milk')\n"
    "    subprocess.run(['osascript', '-e', 'display notification \"saved\"'])\n"
)


def _kinds(source: str) -> list[str]:
    return [n.kind for n in tool_needs.scan_source(source)]


@pytest.fixture(autouse=True)
def _clean_dry_run():
    tool_needs.clear_dry_run()
    yield
    tool_needs.clear_dry_run()


# ---- the source scan -----------------------------------------------------------


def test_quit_spotify_needs_outside_the_sandbox_with_the_quit_reason() -> None:
    needs = tool_needs.scan_source(QUIT_SPOTIFY)
    assert [(n.kind, n.reason) for n in needs] == [
        (tool_needs.OUTSIDE_SANDBOX, tool_needs.REASON_QUIT)]
    assert "quits apps" in needs[0].reason


def test_quit_through_an_fstring_and_a_tell_block_too() -> None:
    fstr = _tool("    subprocess.run(['osascript', '-e', f'tell application \"{x}\" to quit'])\n")
    block = _tool("    s = 'tell application \"Music\"\\n  quit\\nend tell'\n"
                  "    subprocess.run(['osascript', '-e', s])\n")
    assert tool_needs.OUTSIDE_SANDBOX in _kinds(fstr.replace("{x}", "{app}"))
    assert _kinds(block) == [tool_needs.OUTSIDE_SANDBOX, "automation:Music"]


def test_reading_contacts_needs_contacts() -> None:
    assert _kinds(READ_CONTACTS) == [tool_needs.CONTACTS]
    script = _tool("    s = 'tell application \"Contacts\"\\n get name of every person\\nend tell'\n"
                   "    subprocess.run(['osascript', '-e', s])\n")
    assert _kinds(script) == [tool_needs.CONTACTS, "automation:Contacts"]


def test_a_plain_documents_tool_needs_nothing() -> None:
    assert tool_needs.scan_source(DOCUMENTS_TOOL) == []


def test_mail_message_data_and_denied_apps_and_programs() -> None:
    mail = _tool("    s = 'tell application \"Mail\"\\n set m to messages of inbox\\nend tell'\n"
                 "    subprocess.run(['osascript', '-e', s])\n")
    draft = _tool("    s = 'tell application \"Mail\"\\n make new outgoing message\\nend tell'\n"
                  "    subprocess.run(['osascript', '-e', s])\n")
    finder = _tool("    subprocess.run(['osascript', '-e', 'tell application \"Finder\" to activate'])\n")
    killall = _tool("    subprocess.run(['killall', 'Dock'])\n")
    reasons = [n.reason for n in tool_needs.scan_source(mail)]
    assert tool_needs.REASON_MAIL_DATA in reasons
    assert tool_needs.OUTSIDE_SANDBOX not in _kinds(draft)  # composing is not message data
    assert any("Finder" in n.reason and n.kind == tool_needs.OUTSIDE_SANDBOX
               for n in tool_needs.scan_source(finder))
    assert [n.reason for n in tool_needs.scan_source(killall)] == [
        "it runs killall, a program the sandbox blocks"]


def test_writes_into_library_and_full_disk_access_reads() -> None:
    lib = _tool("    import os\n"
                "    target = os.path.expanduser('~/Library/Preferences/x.plist')\n"
                "    open(target, 'w').write('x')\n")
    fda = _tool("    from pathlib import Path\n"
                "    db = Path.home() / 'Library' / 'Messages' / 'chat.db'\n")
    assert [n.reason for n in tool_needs.scan_source(lib)] == [
        "it writes into ~/Library, which the sandbox closes"]
    assert _kinds(fda) == [tool_needs.FULL_DISK_ACCESS]


def test_the_denied_names_match_the_sandbox_profile() -> None:
    assert {b.lower() for b in tool_sandbox.DENIED_APPLE_EVENT_TARGETS} == set(
        tool_needs._DENIED_BUNDLE_NAMES)


def test_unparseable_source_has_no_needs() -> None:
    assert tool_needs.scan_source("def (:") == []


# ---- idea text ------------------------------------------------------------------


def test_idea_text_guess() -> None:
    args = {"kwargs": {"tool_name": "quit_spotify", "description": "Quit Spotify at 6 pm",
                       "expected_logic": "Use AppleScript to quit Spotify."}}
    [need] = tool_needs.idea_card_needs(args, check=False)
    assert need["kind"] == tool_needs.OUTSIDE_SANDBOX and "quitting apps" in need["reason"]
    assert tool_needs.idea_card_needs(
        {"kwargs": {"description": "Move PDFs from Downloads into Documents/Invoices",
                    "expected_logic": "shutil.move each pdf"}}, check=False) == []
    assert [n.kind for n in tool_needs.idea_needs("Email my contacts a summary")] == [
        tool_needs.CONTACTS]


# ---- permission state -----------------------------------------------------------


def test_granted_comes_from_the_permissions_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permissions, "check_contacts", lambda: {"status": "not_asked"})
    [need] = tool_needs.code_needs("t", READ_CONTACTS)
    assert need == {"kind": "contacts", "reason": "it reads your contacts",
                    "granted": False, "permission_id": "contacts"}
    monkeypatch.setattr(permissions, "check_contacts", lambda: {"status": "granted"})
    assert tool_needs.code_needs("t", READ_CONTACTS)[0]["granted"] is True
    monkeypatch.setattr(permissions, "check_contacts", lambda: {"status": "unknown"})
    assert tool_needs.code_needs("t", READ_CONTACTS)[0]["granted"] is None
    # outside_sandbox is never "granted": the card only explains (7b).
    assert tool_needs.code_needs("t", QUIT_SPOTIFY)[0]["granted"] is None


# ---- dry run --------------------------------------------------------------------


def test_dry_run_minus_10004_is_a_need_not_a_drafter_problem() -> None:
    call = dr.DryRunCall(kwargs={})
    out = {"exception": "RuntimeError: osascript failed: execution error: "
                        "A privilege violation occurred. (-10004)",
           "thread_errors": [], "result": None, "home": "/tmp/fake"}
    one = dr.evaluate_call(call, out, set(), "dry run 1")
    assert one.hard == []
    assert [n.kind for n in one.needs] == [tool_needs.OUTSIDE_SANDBOX]
    assert one.needs[0].reason == tool_needs.REASON_DRY_RUN_APPLE_EVENT


def test_dry_run_other_refusals_stay_hard_problems() -> None:
    call = dr.DryRunCall(kwargs={})
    home = "/tmp/fake"
    path = {"exception": f"PermissionError: [Errno 1] Operation not permitted: '{home}/.bot-inator/a'",
            "thread_errors": [], "result": None, "home": home}
    plain = {"exception": "KeyError: 'x'", "thread_errors": [], "result": None, "home": home}
    for out in (path, plain):
        one = dr.evaluate_call(call, out, set(), "dry run 1")
        assert one.hard and one.needs == []


def test_dry_run_denied_exec_is_a_need() -> None:
    out = {"exception": "PermissionError: [Errno 1] Operation not permitted: '/usr/sbin/screencapture'",
           "thread_errors": [], "result": None, "home": "/tmp/fake"}
    one = dr.evaluate_call(dr.DryRunCall(kwargs={}), out, set(), "dry run 1")
    assert one.hard == [] and one.needs[0].reason == tool_needs.REASON_DRY_RUN_EXEC


def test_run_dry_run_records_needs_for_the_code_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr, "sandbox_available", lambda: True)
    out = {"exception": None, "thread_errors": [], "home": "/tmp/fake",
           "result": {"status": "error", "message": "execution error: (-10004)"}}
    monkeypatch.setattr(dr, "_execute", lambda *a, **k: (out, set()))
    plan = dr.DryRunPlan(files={}, calls=[dr.DryRunCall(kwargs={}, expect_status="ok")])
    result = dr.run_dry_run("t", DOCUMENTS_TOOL, plan)
    assert result.hard == []
    noted = "# REVIEW NOTES: x\n# - y\n\n" + DOCUMENTS_TOOL
    kinds = [n["kind"] for n in tool_needs.code_needs("t", noted, check=False)]
    assert kinds == [tool_needs.OUTSIDE_SANDBOX]
    # Another draft of the same name does not inherit them.
    assert [n["reason"] for n in tool_needs.code_needs("t", QUIT_SPOTIFY, check=False)] == [
        tool_needs.REASON_QUIT]
    assert tool_needs.dry_run_needs("t", DOCUMENTS_TOOL + "# changed\n") == []


# ---- payloads -------------------------------------------------------------------


def test_payloads_carry_needs_and_old_payloads_still_decode() -> None:
    base = dict(request_id="r", event_id="e", tool_name="t", description="d", code="c")
    assert CodeApprovalRequestPayload(**base).needs is None
    p = CodeApprovalRequestPayload(**base, needs=[{"kind": "contacts", "reason": "x", "granted": False}])
    assert p.model_dump()["needs"][0] == {"kind": "contacts", "reason": "x", "granted": False,
                                          "permission_id": None}
    assert ApprovalRequestPayload(request_id="r", event_id="e", tool_name="t",
                                  reasoning="x").needs is None


def test_code_card_carries_needs_and_the_checkbox_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> Any:
        orch = _approval_orch()
        orch._pending_code = {}
        orch.code_approval_timeout = 0.05
        await orch.request_code_approval(tool_name="quit_spotify", description="d", code=QUIT_SPOTIFY)
        return orch

    orch = asyncio.run(scenario())
    [msg] = [m for m in orch.sent if isinstance(m, CodeApprovalRequest)]
    assert [n.kind for n in msg.payload.needs] == [tool_needs.OUTSIDE_SANDBOX]
    assert "quits apps" in msg.payload.needs[0].reason
    # The default response (timeout) trusts nothing: the box is off by default.
    assert CodeApprovalResponsePayload(request_id="r", approved=True).outside_sandbox is False


def test_idea_card_carries_the_likely_needs() -> None:
    async def scenario(tool_name: str) -> Any:
        orch = _approval_orch()
        orch.approval_timeout = 0.05
        await orch._request_approval(
            "evt", tool_name,
            {"kwargs": {"tool_name": "quit_spotify", "description": "Quit Spotify",
                        "expected_logic": "AppleScript quit"}}, "r")
        return orch

    [card] = _cards(asyncio.run(scenario("generate_custom_tool")))
    assert [n.kind for n in card.needs] == [tool_needs.OUTSIDE_SANDBOX]
    [card] = _cards(asyncio.run(scenario("create_note")))
    assert card.needs is None


def test_code_card_without_needs_sends_none() -> None:
    async def scenario() -> Any:
        orch = _approval_orch()
        orch._pending_code = {}
        orch.code_approval_timeout = 0.05
        await orch.request_code_approval(tool_name="t", description="d", code=DOCUMENTS_TOOL)
        return orch

    [msg] = [m for m in asyncio.run(scenario()).sent if isinstance(m, CodeApprovalRequest)]
    assert msg.payload.needs is None


def test_tool_needs_never_touches_trust() -> None:
    src = Path(tool_needs.__file__).read_text()
    assert "tool_trust" not in src and "offer_from_review" not in src
