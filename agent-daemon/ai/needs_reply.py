"""Deterministic "this email asks the user something" test (todo #70).

Pure functions over plain fields: no I/O, no model, no settings. #70 uses
:func:`needs_reply` on received mail before one judge call; #73 reuses
:func:`asks_something` (and the helpers) on the user's sent mail.

The checks, in order (the first failure is the ``reason``):

1. ``bulk``: mailing-list / newsletter mail (List-Id or List-Unsubscribe).
2. ``automated``: a no-reply / notification / bounce sender.
3. ``no_user_address``: no address of the user is known, so "the user is
   in To" cannot be checked. In doubt: no (invariant 7d).
4. ``from_user``: the user wrote it.
5. ``cc_only`` / ``not_in_to``: the user is only in Cc, or not in To or
   Cc at all (a list or alias delivered it).
6. ``no_question``: no question mark and no request phrase in the subject
   or the first lines of the body (quoted history is ignored).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Iterable, Sequence, Union

Addresses = Union[str, Sequence[str], None]

# How many non-quoted body lines count as "the first lines".
FIRST_LINES = 6

_AUTOMATED_LOCAL_RE = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|notify|"
    r"mailer[-_.]?daemon|postmaster|bounces?|alerts?|newsletters?|news|"
    r"updates|marketing|automated|calendar[-_.]?notification)(?:$|[-_.+])",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
# A line that starts the quoted original of a reply or forward.
_HISTORY_RE = re.compile(
    r"^(?:on .{0,200}wrote:?$|-{2,}\s*(?:original|forwarded) message\s*-{2,}"
    r"|begin forwarded message:?|from:\s.+@)",
    re.IGNORECASE,
)
_REQUEST_RE = re.compile(
    r"\b(?:can|could|would|will) you\b"
    r"|\bplease\b(?!\s+(?:find|see|note|do not|don['’]t|ignore|disregard)\b)"
    r"|\blet me know\b|\bget back to me\b|\bplease advise\b"
    r"|\bwhat do you think\b|\byour (?:thoughts|input|feedback|opinion)\b"
    r"|\bany (?:thoughts|update|updates|news|feedback|idea)\b"
    r"|\bare you (?:free|available|around|able|ok|okay|coming|going|in)\b"
    r"|\bdo you (?:have|know|want|think|mind|need)\b"
    r"|\bis it possible\b|\bi was wondering\b|\bwdyt\b|\brsvp\b"
    r"|\bneed (?:your|you to)\b",
    re.IGNORECASE,
)
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw|wg)\s*:\s*)+", re.IGNORECASE)


@dataclass(frozen=True)
class ReplyCheck:
    """Outcome of :func:`needs_reply`; truthy when a reply looks needed."""

    needed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.needed


def extract_addresses(value: Addresses) -> list[str]:
    """Lower-cased addresses from a header value or a list of them.

    ``"Sam <Sam@X.com>, b@y.org"`` -> ``["sam@x.com", "b@y.org"]``.
    """
    if not value:
        return []
    items = [value] if isinstance(value, str) else [str(v) for v in value if v]
    out: list[str] = []
    for _, addr in getaddresses(items):
        addr = addr.strip().lower()
        if "@" in addr and addr not in out:
            out.append(addr)
    return out


def is_automated_sender(sender: str) -> bool:
    """True for no-reply, notification, bounce and similar senders."""
    addrs = extract_addresses(sender)
    if not addrs:
        return False
    local = addrs[0].split("@", 1)[0]
    return bool(_AUTOMATED_LOCAL_RE.match(local))


def first_lines(body: str, n: int = FIRST_LINES) -> list[str]:
    """The first ``n`` non-empty lines the sender wrote (no quoted history)."""
    lines: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            continue
        if _HISTORY_RE.match(line):
            break
        lines.append(line)
        if len(lines) >= n:
            break
    return lines


def strip_subject_prefix(subject: str) -> str:
    """``"Re: Fwd: Report"`` -> ``"Report"``."""
    return _SUBJECT_PREFIX_RE.sub("", " ".join((subject or "").split()))


def asks_something(subject: str, body: str = "", n: int = FIRST_LINES) -> bool:
    """A question mark or a request phrase in the subject or first lines."""
    texts = [strip_subject_prefix(subject), *first_lines(body, n)]
    for text in texts:
        text = _URL_RE.sub(" ", text)
        if "?" in text or _REQUEST_RE.search(text):
            return True
    return False


def needs_reply(
    *,
    sender: str,
    to: Addresses,
    cc: Addresses = None,
    subject: str = "",
    body: str = "",
    user_addresses: Iterable[str],
    bulk: bool = False,
) -> ReplyCheck:
    """Whether a received email directly asks the user something.

    ``to``/``cc`` are header values or lists; ``user_addresses`` are the
    user's own addresses (any case). See the module docstring for the
    checks and their ``reason`` codes; ``"ok"`` when every check passes.
    """
    if bulk:
        return ReplyCheck(False, "bulk")
    if is_automated_sender(sender):
        return ReplyCheck(False, "automated")
    mine = {a.strip().lower() for a in user_addresses if a and "@" in a}
    if not mine:
        return ReplyCheck(False, "no_user_address")
    if set(extract_addresses(sender)) & mine:
        return ReplyCheck(False, "from_user")
    if not set(extract_addresses(to)) & mine:
        cc_only = bool(set(extract_addresses(cc)) & mine)
        return ReplyCheck(False, "cc_only" if cc_only else "not_in_to")
    if not asks_something(subject, body):
        return ReplyCheck(False, "no_question")
    return ReplyCheck(True, "ok")


__all__ = [
    "FIRST_LINES",
    "ReplyCheck",
    "asks_something",
    "extract_addresses",
    "first_lines",
    "is_automated_sender",
    "needs_reply",
    "strip_subject_prefix",
]
