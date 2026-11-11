"""Prepared offers: show the finished result, not a proposal (todo #76).

A next-step idea (:mod:`ai.idea_sources`) used to arrive as a question
("add this to your calendar?") and the agent worked out the arguments only
after the user said yes. Here a fixed, read-only preparation runs *before*
the card is shown, so the card arrives filled in with one decision left:

* ``document_downloaded``: an invoice / receipt / statement / ticket lands
  in Downloads. The text is read locally (``summarize_file`` for text files,
  PDFKit for PDFs), vendor / amount / due date are extracted (regexes, then
  at most one model call for what is still missing), a file name and a
  destination folder are proposed (the #75 activity history when it shows a
  usual destination, else the folder where similar files already are, via
  ``find_files``) and the offer bundles ``move_file`` + ``create_reminder``.
* ``mail_invite`` / ``mail_deadline``: the email is read with ``read_email``
  and one model call fills ``create_event`` (title, start, end, location,
  attendees in notes) or ``create_reminder`` (title, due).

Rules (LIVING_SPEC §1, todo #76):

* Only ``tools.READ_ONLY_TOOL_NAMES`` run during preparation
  (:class:`ReadOnlyTools` refuses anything else). Nothing gated runs before
  the user approves the card.
* Budget: one model call and :data:`BUDGET_SECONDS`. Any failure, timeout or
  empty result returns the idea unchanged (today's plain offer); the card is
  never blocked.
* Privacy (#31/#52): file and mail content only reaches the model when the
  detection engine may see personal text (``privacy.should_redact``). Under
  a cloud engine the regex extraction still runs (it is local); the model
  call is skipped. Mail offers need the model, so they fall back to the
  plain offer.

The bundle IS the approval card: :meth:`PreparedOffer.to_card` gives the
``approval_request`` fields, including the optional ``actions`` list (each
``{tool, args, enabled, label}``). The menu-bar card ticks each action; the
response's ``edited_args`` carries ``{"actions": [...]}`` back (or plain
``{"args", "kwargs"}`` edits for a one-action card), and
:func:`run_approved_actions` runs only the ticked actions through the
normal tool functions, after approval, never adding a tool the offer did
not list.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePath
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from ai import privacy
from events.event_bus import ContextEvent

logger = logging.getLogger(__name__)

Evaluate = Callable[[str], Awaitable[str]]

# Card tool name for a bundle of two or more actions.
PREPARED_TOOL_NAME = "prepared_offer"
# Idea-metadata key the prepared offer is attached under.
PREPARED_KEY = "prepared_offer"
BUDGET_SECONDS = 10.0
MAX_MODEL_CALLS = 1
# The only tools an offer may bundle (all gated; they run after approval).
# Runner (wave 15, #88): a watch's one action may also be any of its other
# gated built-ins, so every watch card carries its exact arguments and no
# model runs. ``run_approved_actions`` calls each the same way (the tool's
# own function with the card's kwargs, after the card was approved).
BUNDLE_TOOLS: frozenset[str] = frozenset({
    "move_file", "create_reminder", "create_event",
    "draft_email", "create_note", "open_app", "open_url", "run_shortcut",
})

# New trigger kind for idea_sources (runner wiring): a document in Downloads.
DOCUMENT_TRIGGER = "document_downloaded"
DOCUMENT_IDEA_NAME = "file_downloaded_document"
DOCUMENT_FOLLOW_UP = (
    "Read the downloaded document, file it under a clear name in the folder "
    "where similar documents go (built-in move_file), and add a reminder for "
    "its due date (built-in create_reminder); it asks the user for approval."
)
MAIL_KINDS: frozenset[str] = frozenset({"mail_invite", "mail_deadline"})

_DOC_SUFFIXES = frozenset({".pdf", ".txt", ".html", ".htm"})
_DOC_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("invoice", re.compile(r"invoice|rechnung|factura|facture|\bbill\b", re.I)),
    ("receipt", re.compile(r"receipt|quittung|recibo", re.I)),
    ("statement", re.compile(r"statement", re.I)),
    ("ticket", re.compile(r"ticket|boarding[ _-]?pass|e-?ticket", re.I)),
)
_TEMP_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")
_MAX_TEXT = 4000


# ---- data ----------------------------------------------------------------


@dataclass
class OfferAction:
    tool: str
    args: dict[str, Any]
    enabled: bool = True
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": dict(self.args), "enabled": self.enabled,
                "label": self.label}


@dataclass
class PreparedOffer:
    kind: str
    headline: str
    actions: list[OfferAction]
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "headline": self.headline, "facts": dict(self.facts),
                "actions": [a.to_dict() for a in self.actions]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PreparedOffer":
        actions = [OfferAction(str(a["tool"]), dict(a.get("args") or {}),
                               bool(a.get("enabled", True)), str(a.get("label") or ""))
                   for a in data.get("actions") or []]
        return cls(str(data.get("kind") or ""), str(data.get("headline") or ""),
                   actions, dict(data.get("facts") or {}))

    def to_card(self) -> dict[str, Any]:
        """``approval_request`` payload fields (minus request/event ids).

        One action: the card is that tool's own editable card (#18) and
        ``actions`` holds just it. Two or more: ``tool_name`` is
        :data:`PREPARED_TOOL_NAME` and the card lists each with a tick box.
        """
        single = len(self.actions) == 1
        first = self.actions[0]
        return {
            "tool_name": first.tool if single else PREPARED_TOOL_NAME,
            "tool_args": {"args": [], "kwargs": dict(first.args) if single else {}},
            "reasoning": self.headline,
            "summary": self.headline,
            "actions": [a.to_dict() for a in self.actions],
        }


# ---- read-only tools -----------------------------------------------------


class GatedToolError(RuntimeError):
    """A preparation step asked for a tool outside READ_ONLY_TOOL_NAMES."""


def _callable(obj: Any) -> Callable[..., Any]:
    return getattr(obj, "_ungated_forward", None) or getattr(obj, "forward", None) or obj


def default_tools() -> dict[str, Any]:
    import tools as tool_pkg  # deferred: tools imports lead back into ai

    return {getattr(t, "name", getattr(t, "__name__", "")): t for t in tool_pkg.AVAILABLE_TOOLS}


def _read_only_names() -> frozenset[str]:
    from tools import READ_ONLY_TOOL_NAMES

    return READ_ONLY_TOOL_NAMES


class ReadOnlyTools:
    """Calls tools by name, refusing every one not in READ_ONLY_TOOL_NAMES."""

    def __init__(self, tools: Optional[Mapping[str, Any]] = None,
                 read_only: Optional[Iterable[str]] = None) -> None:
        self._tools = tools
        self._read_only = frozenset(read_only) if read_only is not None else None

    def accepts(self, name: str, param: str) -> bool:
        """Whether tool ``name`` takes ``param`` (signature only, never called)."""
        try:
            if self._tools is None:
                self._tools = default_tools()
            fn = _callable(self._tools[name])
            return param in inspect.signature(fn).parameters
        except Exception:
            return False

    def call(self, name: str, **kwargs: Any) -> Any:
        allowed = self._read_only if self._read_only is not None else _read_only_names()
        if name not in allowed:
            raise GatedToolError(f"{name} is not read-only; preparation skips it")
        if self._tools is None:
            self._tools = default_tools()
        tool = self._tools.get(name)
        if tool is None:
            raise LookupError(f"no tool {name!r}")
        return _callable(tool)(**kwargs)


# ---- extraction (local, deterministic) -------------------------------------


def _is_downloads(path: PurePath) -> bool:
    return any(part.lower() == "downloads" for part in path.parent.parts)


def document_kind(path: str) -> Optional[str]:
    """``invoice``/``receipt``/``statement``/``ticket`` for a document that
    landed in a Downloads folder (by file name), else ``None``."""
    p = PurePath(path or "")
    name = p.name
    if not name or name.startswith(".") or name.lower().endswith(_TEMP_SUFFIXES):
        return None
    if p.suffix.lower() not in _DOC_SUFFIXES or not _is_downloads(p):
        return None
    for kind, pattern in _DOC_TYPES:
        if pattern.search(name):
            return kind
    return None


def classify_document_event(event: ContextEvent) -> Optional[str]:
    """``DOCUMENT_TRIGGER`` for a created/renamed document in Downloads."""
    meta = event.metadata or {}
    if event.event_type != "file" or meta.get("action") not in {"created", "renamed"}:
        return None
    return DOCUMENT_TRIGGER if document_kind(str(meta.get("path") or "")) else None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<y>20\d\d)-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),
    re.compile(_MONTH_RE.replace("(jan", "(?P<mon>jan") + r"\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>20\d\d)", re.I),
    re.compile(r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RE.replace("(jan", "(?P<mon>jan") + r",?\s+(?P<y>20\d\d)", re.I),
    re.compile(r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>20\d\d)"),
)
_DUE_RE = re.compile(r"\b(?:due(?:\s+date)?|pay(?:ment)?\s+by|payable\s+by|date\s+of\s+travel|departure)\b", re.I)
_AMOUNT_RE = re.compile(
    r"(?P<cur>[$€£]|USD|EUR|GBP|CAD|AUD)\s?(?P<num>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
    r"|(?P<num2>\d{1,3}(?:,\d{3})*\.\d{2})\s?(?P<cur2>USD|EUR|GBP|CAD|AUD|€)", re.I)
_TOTAL_RE = re.compile(r"\b(?:amount due|balance due|total due|total|amount)\b", re.I)


def _date_from_match(m: "re.Match[str]") -> Optional[date]:
    g = m.groupdict()
    try:
        month = _MONTHS[g["mon"][:3].lower()] if g.get("mon") else int(g["m"])
        return date(int(g["y"]), month, int(g["d"]))
    except (KeyError, TypeError, ValueError):
        return None


def find_dates(text: str) -> list[tuple[int, date]]:
    found: list[tuple[int, date]] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            d = _date_from_match(m)
            if d is not None:
                found.append((m.start(), d))
    return sorted(found)


def find_due_date(text: str) -> Optional[str]:
    """ISO date of the first date within 60 chars after a 'due' word."""
    dates = find_dates(text)
    for cue in _DUE_RE.finditer(text):
        for pos, d in dates:
            if cue.end() <= pos <= cue.end() + 60:
                return d.isoformat()
    return None


def find_amount(text: str) -> Optional[str]:
    """The amount after a 'total'/'amount due' word, else the first amount."""
    amounts = [(m.start(), (m.group("cur") or m.group("cur2") or "").upper(),
                m.group("num") or m.group("num2")) for m in _AMOUNT_RE.finditer(text)]
    if not amounts:
        return None

    def fmt(cur: str, num: str) -> str:
        num = num.replace(" ", "")
        return f"{cur}{num}" if cur in {"$", "€", "£"} else f"{num} {cur}"

    for cue in _TOTAL_RE.finditer(text):
        for pos, cur, num in amounts:
            if cue.end() <= pos <= cue.end() + 40:
                return fmt(cur, num)
    return fmt(amounts[0][1], amounts[0][2])


def _clean_name(text: str, limit: int = 40) -> str:
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", text or "")
    return " ".join(text.split())[:limit].strip(" .-_")


def vendor_from_name(path: str) -> str:
    """A vendor guess from the file name: words left after removing the
    document word, dates, numbers and separators."""
    stem = PurePath(path).stem
    stem = re.sub(r"[_\-.]+", " ", stem)
    for _, pattern in _DOC_TYPES:
        stem = pattern.sub(" ", stem)
    stem = re.sub(r"\b(?:no|nr|num|number|copy|final|scan)\b", " ", stem, flags=re.I)
    stem = re.sub(r"\b[\w]*\d[\w]*\b", " ", stem)
    return _clean_name(stem.title())


def propose_file_name(path: str, doc_type: str, vendor: str, when: Optional[str]) -> str:
    """``"<YYYY-MM-DD> <Vendor> <type><ext>"``; vendor/date left out when unknown."""
    parts = [when or "", _clean_name(vendor), doc_type]
    stem = " ".join(p for p in parts if p)
    return f"{stem}{PurePath(path).suffix.lower()}"


def read_pdf_text(path: str, limit: int = _MAX_TEXT) -> str:
    """Local PDF text via PDFKit (read-only, nothing leaves the Mac)."""
    try:
        from Foundation import NSURL  # type: ignore
        from Quartz import PDFDocument  # type: ignore
    except Exception:
        return ""
    doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
    if doc is None:
        return ""
    chunks: list[str] = []
    total = 0
    for i in range(min(doc.pageCount(), 5)):
        page = doc.pageAtIndex_(i)
        text = str(page.string() or "") if page is not None else ""
        chunks.append(text)
        total += len(text)
        if total >= limit:
            break
    return "\n".join(chunks)[:limit]


def _parse_json_object(raw: str) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _iso_date(value: Any) -> Optional[str]:
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        return None


def _iso_datetime(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


DOCUMENT_PROMPT = (
    "Extract facts from this document. Reply with one JSON object and "
    "nothing else: {\"vendor\": \"<company or issuer>\", \"amount\": \"<total "
    "with currency, or empty>\", \"due_date\": \"<YYYY-MM-DD the payment is "
    "due or the ticket's date, or empty>\"}. Use empty strings for anything "
    "the text does not state.\n\nFile: {name}\nText:\n{text}"
)
MAIL_INVITE_PROMPT = (
    "This email is an invitation. Reply with one JSON object and nothing "
    "else: {\"title\": \"...\", \"start\": \"YYYY-MM-DDTHH:MM\", \"end\": "
    "\"YYYY-MM-DDTHH:MM or empty\", \"location\": \"address or meeting link, "
    "or empty\", \"attendees\": [\"name or address\", ...]}. Local time. "
    "If the email gives no date and time, reply {}.\n\n{mail}"
)
MAIL_DEADLINE_PROMPT = (
    "This email asks for something by a deadline. Reply with one JSON object "
    "and nothing else: {\"title\": \"<what to do, imperative, short>\", "
    "\"due\": \"YYYY-MM-DD or YYYY-MM-DDTHH:MM\"}. If the email gives no "
    "deadline, reply {}.\n\n{mail}"
)


# ---- the preparer ----------------------------------------------------------


class Preparer:
    """Runs the fixed read-only preparation for one idea; never raises."""

    def __init__(
        self,
        *,
        evaluate: Optional[Evaluate] = None,
        tools: Optional[Mapping[str, Any]] = None,
        read_only: Optional[Iterable[str]] = None,
        read_pdf: Callable[[str], str] = read_pdf_text,
        history: Optional[Callable[[], list[dict[str, Any]]]] = None,
        redact: Callable[[str], bool] = privacy.should_redact,
        role: str = "detection",
        budget_seconds: float = BUDGET_SECONDS,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._evaluate = evaluate
        self.tools = ReadOnlyTools(tools, read_only)
        self._read_pdf = read_pdf
        self._history = history if history is not None else _default_history
        self._redact = redact
        self._role = role
        self.budget_seconds = budget_seconds
        self._today = today
        self.model_calls = 0

    # -- entry point --

    async def prepare(self, idea: dict[str, Any], event: ContextEvent) -> dict[str, Any]:
        """``idea`` with ``idea[PREPARED_KEY]`` set when preparation worked,
        else ``idea`` unchanged (the plain offer)."""
        self.model_calls = 0
        trigger = str(idea.get("trigger") or "")
        try:
            offer = await asyncio.wait_for(self._prepare(trigger, event), self.budget_seconds)
        except asyncio.TimeoutError:
            logger.info("prepared offer %s: over the %.0fs budget, plain offer", trigger, self.budget_seconds)
            return idea
        except Exception:
            logger.exception("prepared offer %s failed, plain offer", trigger)
            return idea
        if offer is None or not offer.actions:
            return idea
        prepared = dict(idea)
        prepared[PREPARED_KEY] = offer.to_dict()
        prepared["card_text"] = offer.headline
        return prepared

    async def _prepare(self, trigger: str, event: ContextEvent) -> Optional[PreparedOffer]:
        meta = event.metadata or {}
        if trigger == DOCUMENT_TRIGGER and event.event_type == "file":
            return await self.prepare_document(str(meta.get("path") or ""))
        if trigger in MAIL_KINDS and event.event_type == "mail_received":
            return await self.prepare_mail(trigger, meta)
        return None

    async def _model(self, prompt: str) -> Optional[dict[str, Any]]:
        if self._evaluate is None or self.model_calls >= MAX_MODEL_CALLS:
            return None
        self.model_calls += 1
        return _parse_json_object(await self._evaluate(prompt))

    def _may_send_content(self) -> bool:
        try:
            return not self._redact(self._role)
        except Exception:
            logger.exception("prepared offer: privacy lookup failed")
            return False

    async def _tool(self, name: str, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.tools.call, name, **kwargs)

    # -- (a) a document in Downloads --

    async def _document_text(self, path: str) -> str:
        if PurePath(path).suffix.lower() == ".pdf":
            return await asyncio.to_thread(self._read_pdf, path) or ""
        result = await self._tool("summarize_file", path=path, max_chars=_MAX_TEXT)
        return str((result or {}).get("preview") or "") if isinstance(result, dict) else ""

    async def prepare_document(self, path: str) -> Optional[PreparedOffer]:
        doc_type = document_kind(path)
        if doc_type is None:
            return None
        text = await self._document_text(path)
        vendor = ""
        amount = find_amount(text) if text else None
        due = find_due_date(text) if text else None
        if text and self._may_send_content():
            got = await self._model(
                DOCUMENT_PROMPT.replace("{name}", PurePath(path).name).replace("{text}", text[:3000]))
            if got:
                vendor = _clean_name(str(got.get("vendor") or ""))
                amount = amount or (str(got.get("amount") or "").strip() or None)
                due = due or _iso_date(got.get("due_date"))
        vendor = vendor or vendor_from_name(path)
        folder = await self.destination_folder(path, doc_type)
        new_name = propose_file_name(path, doc_type, vendor, due or self._today().isoformat())
        actions: list[OfferAction] = []
        # move_file keeps the file's name unless it takes a ``new_name``.
        renames = self.tools.accepts("move_file", "new_name")
        shown_name = new_name if renames else PurePath(path).name
        if folder:
            move_args = {"src": path, "dst_dir": folder}
            if renames:
                move_args["new_name"] = new_name
            actions.append(OfferAction(
                "move_file", move_args, label=f"File as {shown_name} in {_short(folder)}"))
        if due and doc_type in {"invoice", "statement", "ticket"}:
            what = vendor or doc_type
            title = f"{what} ticket" if doc_type == "ticket" else f"Pay {what} {doc_type}"
            notes = ", ".join(x for x in (amount or "", new_name) if x)
            actions.append(OfferAction(
                "create_reminder", {"title": title, "due": due, "notes": notes},
                label=f"Remind you on {due}"))
        if not actions:
            return None
        bits = []
        if folder:
            bits.append(f"File as {shown_name} in {_short(folder)}")
        if len(actions) > 1 or not folder:
            bits.append(f"remind you on {due}")
        headline = " and ".join(bits) + "?"
        headline = headline[0].upper() + headline[1:]
        if not renames and folder:
            headline += f" (suggested name: {new_name})"
        facts = {"doc_type": doc_type, "vendor": vendor, "amount": amount or "", "due_date": due or "",
                 "file_name": new_name, "folder": folder or ""}
        return PreparedOffer(DOCUMENT_TRIGGER, headline, actions, facts)

    async def destination_folder(self, path: str, doc_type: str) -> Optional[str]:
        """The usual destination from #75 history, else where similar files are."""
        src = Path(path).expanduser()
        try:
            folder = self._history_folder(src)
        except Exception:
            logger.exception("prepared offer: history lookup failed")
            folder = None
        if folder:
            return folder
        try:
            result = await self._tool(
                "find_files", query=doc_type,
                kind="pdf" if src.suffix.lower() == ".pdf" else "", limit=30)
        except GatedToolError:
            raise
        except Exception:
            logger.exception("prepared offer: find_files failed")
            return None
        counts: Counter[str] = Counter()
        for item in (result or {}).get("results") or []:
            parent = Path(str(item.get("path") or "")).parent
            if not str(parent) or parent == src.parent or _skip_folder(parent):
                continue
            counts[str(parent)] += 1
        for folder, _ in counts.most_common():
            if os.path.isdir(folder):
                return folder
        return None

    def _history_folder(self, src: Path) -> Optional[str]:
        rows = self._history() or []
        ext = src.suffix.lower()
        here = _tilde(src.parent)
        counts: Counter[str] = Counter(
            str(r.get("folder")) for r in rows
            if r.get("event_type") == "file" and r.get("from_folder") == here
            and r.get("folder") and r.get("folder") != here and (r.get("ext") or "") == ext
        )
        for folder, n in counts.most_common():
            full = str(Path(folder).expanduser())
            if n >= 2 and os.path.isdir(full) and not _skip_folder(Path(full)):
                return full
        return None

    # -- (b) a mail with a date or an invitation --

    async def prepare_mail(self, trigger: str, meta: Mapping[str, Any]) -> Optional[PreparedOffer]:
        if not self._may_send_content():
            return None  # the model is the extractor; content stays local
        try:
            mid = int(meta.get("message_id") or 0)
        except (TypeError, ValueError):
            return None
        if mid <= 0:
            return None
        mail = await self._tool("read_email", message_id=mid)
        if not isinstance(mail, dict) or mail.get("status") != "ok":
            return None
        text = (f"From: {mail.get('sender', '')}\nSubject: {mail.get('subject', '')}\n"
                f"Date: {mail.get('date', '')}\n\n{str(mail.get('body') or '')[:3000]}")
        if trigger == "mail_invite":
            got = await self._model(MAIL_INVITE_PROMPT.replace("{mail}", text))
            return _invite_offer(got or {}, str(mail.get("subject") or ""))
        got = await self._model(MAIL_DEADLINE_PROMPT.replace("{mail}", text))
        return _deadline_offer(got or {}, str(mail.get("subject") or ""))


def _invite_offer(got: Mapping[str, Any], subject: str) -> Optional[PreparedOffer]:
    start = _iso_datetime(got.get("start"))
    if start is None:
        return None
    title = _clean_name(str(got.get("title") or subject), 120) or "Meeting"
    args: dict[str, Any] = {"title": title, "start": start}
    end = _iso_datetime(got.get("end"))
    if end:
        args["end"] = end
    location = str(got.get("location") or "").strip()
    if location:
        args["location"] = location[:300]
    attendees = got.get("attendees") or []
    if isinstance(attendees, list) and attendees:
        args["notes"] = "Attendees: " + ", ".join(str(a) for a in attendees[:20])
    where = f" at {location}" if location else ""
    headline = f"Add \"{title}\" on {start.replace('T', ' ')}{where} to your calendar?"
    return PreparedOffer("mail_invite", headline,
                         [OfferAction("create_event", args, label=headline.rstrip("?"))],
                         {"title": title, "start": start})


def _deadline_offer(got: Mapping[str, Any], subject: str) -> Optional[PreparedOffer]:
    due = _iso_date(got.get("due")) if "T" not in str(got.get("due") or "") else _iso_datetime(got.get("due"))
    if due is None:
        return None
    title = _clean_name(str(got.get("title") or subject), 120)
    if not title:
        return None
    headline = f"Remind you to {title[0].lower() + title[1:]} by {due.replace('T', ' ')}?"
    return PreparedOffer("mail_deadline", headline,
                         [OfferAction("create_reminder", {"title": title, "due": due},
                                      label=headline.rstrip("?"))],
                         {"title": title, "due": due})


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    if text == home:
        return "~"
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _short(folder: str) -> str:
    return _tilde(Path(folder))


def _skip_folder(folder: Path) -> bool:
    parts = {p.lower() for p in folder.parts}
    return bool(parts & {"downloads", ".trash", "library"})


def _default_history() -> list[dict[str, Any]]:
    from ai import activity_store

    if not activity_store.history_enabled():
        return []
    return activity_store.default_store().rows()


# ---- after approval: run the ticked subset ---------------------------------


def approved_actions(offer: Mapping[str, Any] | PreparedOffer,
                     edited_args: Optional[Mapping[str, Any]],
                     tools: Optional[Mapping[str, Any]] = None) -> list[OfferAction]:
    """The actions to run after an approved card: the offer's own actions,
    with the user's ticks and field edits applied. A returned action may only
    switch ``enabled`` and change args the tool accepts; its tool can never
    differ from the offer's, and nothing can be added."""
    base = offer if isinstance(offer, PreparedOffer) else PreparedOffer.from_dict(offer)
    actions = [OfferAction(a.tool, dict(a.args), a.enabled, a.label) for a in base.actions]
    edited = dict(edited_args or {})
    returned = edited.get("actions")
    if isinstance(returned, list):
        for i, item in enumerate(returned[: len(actions)]):
            if not isinstance(item, Mapping) or item.get("tool") != actions[i].tool:
                continue
            actions[i].enabled = bool(item.get("enabled", True))
            args = item.get("args")
            if isinstance(args, Mapping):
                actions[i].args.update(_allowed_args(actions[i], args, tools))
    elif len(actions) == 1 and isinstance(edited.get("kwargs"), Mapping):
        actions[0].args.update(_allowed_args(actions[0], edited["kwargs"], tools))
    return [a for a in actions if a.enabled and a.tool in BUNDLE_TOOLS]


def _allowed_args(action: OfferAction, args: Mapping[str, Any],
                  tools: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    allowed = set(action.args)
    try:
        fn = _callable((tools or default_tools())[action.tool])
        allowed |= {p for p in inspect.signature(fn).parameters if p != "self"}
    except Exception:
        pass
    return {k: v for k, v in args.items() if k in allowed}


def run_approved_actions(offer: Mapping[str, Any] | PreparedOffer,
                         edited_args: Optional[Mapping[str, Any]],
                         tools: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """Run the ticked actions of an APPROVED card through the normal tool
    functions (call it only after the user approved; a denied or timed-out
    card runs nothing). Returns one ``{tool, args, result}`` per action run.
    One failing action does not stop the next."""
    table = tools if tools is not None else default_tools()
    out: list[dict[str, Any]] = []
    for action in approved_actions(offer, edited_args, table):
        try:
            result = _callable(table[action.tool])(**action.args)
        except Exception as exc:
            logger.exception("prepared offer action %s failed", action.tool)
            result = {"status": "error", "error": str(exc)}
        out.append({"tool": action.tool, "args": dict(action.args), "result": result})
    return out


__all__ = [
    "BUDGET_SECONDS", "BUNDLE_TOOLS", "DOCUMENT_FOLLOW_UP", "DOCUMENT_IDEA_NAME",
    "DOCUMENT_TRIGGER", "GatedToolError", "MAIL_KINDS", "OfferAction", "PREPARED_KEY",
    "PREPARED_TOOL_NAME", "PreparedOffer", "Preparer", "ReadOnlyTools", "approved_actions",
    "classify_document_event", "document_kind", "find_amount", "find_due_date",
    "propose_file_name", "run_approved_actions", "vendor_from_name",
]
