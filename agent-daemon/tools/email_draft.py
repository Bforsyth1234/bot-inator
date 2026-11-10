"""Create an unsent email draft in the native macOS Mail app (AppleScript).

DRAFTS ONLY (#63, the user's decision). This module opens a visible draft
window in Mail.app and stops there: the user reads it and presses the button
in Mail themselves. There is no tool here that delivers mail, and the
AppleScript constants below never contain Mail's delivery command (a test
asserts that).

User text never becomes part of the AppleScript source. The scripts are fixed
constants; recipients, subject and body reach them as ``osascript`` arguments
(``osascript -e <script> -- arg1 arg2 ...``) and are read with ``on run
argv``, so quotes or ``end tell`` in the text are plain data. Recipients are
also validated as email addresses in Python before anything runs.

Replies: ``reply_to_message_id`` is the ``message_id`` of a ``mail_received``
event / ``read_email`` result, i.e. ``messages.ROWID`` in Mail's Envelope
Index, which is what Mail's AppleScript exposes as ``id of message``. The
reply script looks that id up (inbox first, then every mailbox) and uses
Mail's own ``reply`` so threading headers and the quoted original are right.

No attachments in v1. The tool changes user data and is approval-gated: its
name does not belong in ``tools.READ_ONLY_TOOL_NAMES``.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


# Tool names in this module and which side of the approval gate they sit on.
GATED: frozenset[str] = frozenset({"draft_email"})
READ_ONLY: frozenset[str] = frozenset()

# The first-use Automation prompt blocks osascript until the user answers.
_TIMEOUT_S = 30
# A reply first has to find the message by id, which can scan many mailboxes.
_REPLY_TIMEOUT_S = 90

_NOTE = "Draft opened in Mail. It has NOT been delivered; the user reviews it and mails it themselves."

# local@domain.tld, ASCII, no spaces/commas/quotes/angle brackets.
_ADDRESS_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
_NAME_ADDR_RE = re.compile(r"[^<>]*<([^<>]+)>")

# Shared handler: "a@x.com,b@y.com" -> {"a@x.com", "b@y.com"}; "" -> {}.
_SPLIT_HANDLER = '''
on splitList(joined)
    if joined is "" then return {}
    set oldDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to ","
    set parts to text items of joined
    set AppleScript's text item delimiters to oldDelims
    return parts
end splitList
'''

# argv: 1 = to (comma-joined addresses), 2 = cc (same, may be ""),
#       3 = subject, 4 = body.
_NEW_SCRIPT = '''
on run argv
    set toText to item 1 of argv
    set ccText to item 2 of argv
    set subjectText to item 3 of argv
    set bodyText to item 4 of argv
    tell application "Mail"
        set theDraft to make new outgoing message with properties {subject:subjectText, content:bodyText, visible:true}
        tell theDraft
            repeat with addr in my splitList(toText)
                make new to recipient at end of to recipients with properties {address:(addr as string)}
            end repeat
            repeat with addr in my splitList(ccText)
                make new cc recipient at end of cc recipients with properties {address:(addr as string)}
            end repeat
        end tell
        activate
    end tell
    return "OK"
end run
''' + _SPLIT_HANDLER

# argv: 1 = message id (digits), 2 = extra to addresses, 3 = cc addresses,
#       4 = subject ("" keeps Mail's "Re: ..."), 5 = body.
# Mail fills in the original author as recipient; addresses in argv 2/3 are
# added only if not already on the draft. The quoted original is loaded by
# Mail shortly after the window opens, so wait briefly for it before putting
# the new text above it. Returns OK, OK_NO_QUOTE or NOT_FOUND.
_REPLY_SCRIPT = '''
on run argv
    set msgId to (item 1 of argv) as integer
    set toText to item 2 of argv
    set ccText to item 3 of argv
    set subjectText to item 4 of argv
    set bodyText to item 5 of argv
    tell application "Mail"
        set target to missing value
        try
            set hits to (messages of inbox whose id is msgId)
            if (count of hits) > 0 then set target to item 1 of hits
        end try
        if target is missing value then
            repeat with acct in accounts
                repeat with box in mailboxes of acct
                    try
                        set hits to (messages of box whose id is msgId)
                        if (count of hits) > 0 then
                            set target to item 1 of hits
                            exit repeat
                        end if
                    end try
                end repeat
                if target is not missing value then exit repeat
            end repeat
        end if
        if target is missing value then return "NOT_FOUND"

        set theDraft to reply target with opening window
        set quoted to ""
        repeat 15 times
            try
                set quoted to (content of theDraft) as string
            end try
            if quoted is not "" then exit repeat
            delay 0.2
        end repeat
        tell theDraft
            set visible to true
            if subjectText is not "" then set subject to subjectText
            if quoted is "" then
                set content to bodyText
            else
                set content to bodyText & linefeed & linefeed & quoted
            end if
            set have to {}
            try
                set have to (address of every to recipient) & (address of every cc recipient)
            end try
            repeat with addr in my splitList(toText)
                if have does not contain (addr as string) then
                    make new to recipient at end of to recipients with properties {address:(addr as string)}
                    set end of have to (addr as string)
                end if
            end repeat
            repeat with addr in my splitList(ccText)
                if have does not contain (addr as string) then
                    make new cc recipient at end of cc recipients with properties {address:(addr as string)}
                    set end of have to (addr as string)
                end if
            end repeat
        end tell
        activate
    end tell
    if quoted is "" then return "OK_NO_QUOTE"
    return "OK"
end run
''' + _SPLIT_HANDLER


def parse_addresses(value: Any) -> list[str]:
    """Split a comma/semicolon-separated recipient string into addresses.

    Accepts ``a@x.com`` and ``Name <a@x.com>`` entries (the name is dropped)
    and a list of such strings. Duplicates are removed case-insensitively.
    Raises ``ValueError`` naming the first entry that is not an address.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    if not isinstance(value, str):
        raise ValueError(f"not an email address: {value!r}")
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;]", value):
        entry = raw.strip()
        if not entry:
            continue
        named = _NAME_ADDR_RE.fullmatch(entry)
        addr = named.group(1).strip() if named else entry
        if not _ADDRESS_RE.fullmatch(addr):
            raise ValueError(f"not an email address: {entry!r}")
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


def _run(script: str, *args: str, timeout: int = _TIMEOUT_S) -> str:
    """Run a fixed AppleScript with ``args`` as argv; return trimmed stdout."""
    proc = subprocess.run(
        ["osascript", "-e", script, "--", *args],
        check=True, capture_output=True, text=True, timeout=timeout,
    )
    return (proc.stdout or "").strip()


def _error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or str(exc)).strip()
    return str(exc)


@tool
def draft_email(to: str, subject: str, body: str, cc: str = "",
                reply_to_message_id: int = 0) -> dict[str, Any]:
    """Create an email DRAFT in the Mac's Mail app. It is never delivered.

    This only opens an unsent draft window in Mail for the user to review;
    the user mails it themselves. There is no tool that delivers email, so
    never tell the user an email "was sent": say a draft is ready in Mail.

    Args:
        to: Recipient email address, or several separated by commas. For a
            reply, the address of the person being replied to.
        subject: Subject line. For a reply, leave empty to keep "Re: ...".
        body: Plain-text body of the email; line breaks are kept.
        cc: Optional Cc address(es), comma-separated. Empty for none.
        reply_to_message_id: To reply to an existing email, the
            ``message_id`` from a mail_received event or ``read_email``; the
            draft is then a threaded reply quoting that email. 0 (default)
            starts a new email.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``delivered`` (always
        False), ``to``, ``cc``, ``subject``, ``reply_to_message_id``, a
        ``note``, and, on failure, an ``error`` string.
    """
    subject = "" if subject is None else str(subject)
    body = "" if body is None else str(body)
    base: dict[str, Any] = {"delivered": False, "subject": subject}

    def fail(message: str) -> dict[str, Any]:
        return {"status": "error", **base, "error": message}

    try:
        reply_id = int(reply_to_message_id or 0)
    except (TypeError, ValueError):
        return fail(f"invalid reply_to_message_id {reply_to_message_id!r}")
    if reply_id < 0:
        return fail(f"invalid reply_to_message_id {reply_to_message_id!r}")
    base["reply_to_message_id"] = reply_id
    try:
        to_list = parse_addresses(to)
        cc_list = parse_addresses(cc)
    except ValueError as exc:
        return fail(str(exc))
    base.update(to=to_list, cc=cc_list)
    if not to_list and not reply_id:
        return fail("no recipient: 'to' needs at least one email address")
    if not body.strip():
        return fail("empty body: nothing to draft")
    if "\n" in subject or "\r" in subject:
        return fail("subject must be a single line")

    to_arg, cc_arg = ",".join(to_list), ",".join(cc_list)
    try:
        if reply_id:
            out = _run(_REPLY_SCRIPT, str(reply_id), to_arg, cc_arg, subject, body,
                       timeout=_REPLY_TIMEOUT_S)
        else:
            out = _run(_NEW_SCRIPT, to_arg, cc_arg, subject, body)
    except Exception as exc:
        return fail(_error(exc))
    if out == "NOT_FOUND":
        return fail(f"No message with id {reply_id} in Mail; nothing was drafted.")
    result = {"status": "ok", **base, "note": _NOTE}
    if out == "OK_NO_QUOTE":
        result["note"] = _NOTE + " The original message could not be quoted in the draft."
    return result
