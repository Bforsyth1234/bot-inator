"""Read one email's headers and body from Mail.app's on-disk store.

Mail.app stores each message as ``<ROWID>.emlx`` (or ``<ROWID>.partial.emlx``
when attachments live in separate files) somewhere under
``~/Library/Mail/V<n>/<account>/<mailbox>.mbox/…/Messages/``. ``ROWID`` is
the ``messages.ROWID`` of the Envelope Index, i.e. the ``message_id`` that
``events.mail_watcher`` puts in ``mail_received`` events.

An ``.emlx`` file is: a line with the RFC 822 message's byte count, the
message itself, then an XML plist of Mail flags. Read-only; needs Full Disk
Access like the watcher.
"""

from __future__ import annotations

import email
import email.policy
import os
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

DEFAULT_MAIL_ROOT = Path.home() / "Library" / "Mail"
# Tests (and unusual setups) override this; read at call time.
MAIL_ROOT_ENV = "AGENT_MAIL_ROOT"
MAX_BODY_CHARS = 4000


def mail_root() -> Path:
    return Path(os.environ.get(MAIL_ROOT_ENV) or DEFAULT_MAIL_ROOT).expanduser()


def find_emlx(message_id: int, root: Path) -> Optional[Path]:
    """Locate ``<id>.emlx`` (preferred) or ``<id>.partial.emlx`` under root."""
    names = (f"{message_id}.emlx", f"{message_id}.partial.emlx")
    found: dict[str, Path] = {}
    try:
        versions = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name[1:].isdigit()
             and p.name.startswith("V")),
            key=lambda p: int(p.name[1:]), reverse=True,
        )
    except OSError:
        return None
    for version in versions:
        for dirpath, dirnames, filenames in os.walk(version):
            if dirpath == str(version):
                dirnames[:] = [d for d in dirnames if d != "MailData"]
            if os.path.basename(dirpath) != "Messages":
                continue
            for name in names:
                if name in filenames:
                    found.setdefault(name, Path(dirpath) / name)
            if names[0] in found:
                return found[names[0]]
        if found:
            return found.get(names[0]) or found[names[1]]
    return None


def parse_emlx(data: bytes) -> email.message.EmailMessage:
    """Strip the byte-count line and trailing plist, then parse."""
    first, _, rest = data.partition(b"\n")
    try:
        length = int(first.strip())
        raw = rest[:length]
    except ValueError:  # not a byte count: treat it all as the message
        raw = data
    return email.message_from_bytes(raw, policy=email.policy.default)


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed HTML: crude fallback
        text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def message_body(msg: email.message.EmailMessage) -> str:
    """Plain-text part if any, else the HTML part stripped of tags."""
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        if part is None:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        return content.strip() if kind == "plain" else html_to_text(content)
    return ""


def read_email_from(message_id: Any, root: Path) -> dict[str, Any]:
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"invalid message_id {message_id!r}"}
    path = find_emlx(mid, root)
    if path is None:
        return {"status": "error",
                "error": f"no .emlx for message {mid} under {root} "
                         "(missing, or no Full Disk Access)"}
    try:
        msg = parse_emlx(path.read_bytes())
        body = message_body(msg)
    except Exception as exc:
        return {"status": "error", "error": f"could not read {path.name}: {exc}"}
    truncated = len(body) > MAX_BODY_CHARS
    return {
        "status": "ok",
        "message_id": mid,
        "sender": str(msg.get("From", "") or ""),
        "subject": str(msg.get("Subject", "") or ""),
        "date": str(msg.get("Date", "") or ""),
        "body": body[:MAX_BODY_CHARS] + ("…" if truncated else ""),
        "truncated": truncated,
        "partial": path.name.endswith(".partial.emlx"),
    }


@tool
def read_email(message_id: int) -> dict[str, Any]:
    """Read one email from the Mac's Mail app: sender, subject, date and body.

    Use the ``message_id`` from a ``mail_received`` event. Read-only.

    Args:
        message_id: The Mail message id (``message_id`` in a mail_received event).

    Returns:
        A dict with ``status`` ("ok" or "error"), and on success ``sender``,
        ``subject``, ``date``, ``body`` (plain text, at most ~4,000
        characters) and ``truncated``; on failure ``error``.
    """
    return read_email_from(message_id, mail_root())
