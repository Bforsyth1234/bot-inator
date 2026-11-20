"""Imported AI-chat exports as a knowledge source (#141).

The user downloads their data export from ChatGPT or Claude and picks the
file in Settings (decision 1). This module reads that file IN PLACE, read
only: nothing is copied into ``~/.bot-inator`` and a zip is never unpacked
to disk (decision 5). NOTHING HERE MAKES A NETWORK CALL; nothing is sent to
OpenAI or Anthropic (decision 7).

Formats (decision 2), detected from the JSON structure, never the file name:

* ChatGPT: a JSON array of conversations, each with a ``mapping`` graph of
  nodes (``parent`` / ``children`` / ``message``) and ``current_node``. Only
  the active branch (``current_node`` back to the root) is read, so
  regenerated and abandoned replies are not indexed. Text is
  ``content.parts`` strings of ``text`` / ``multimodal_text`` messages from
  ``user`` / ``assistant``; images, tool calls and output, system, hidden
  and custom-instruction messages are skipped. The export zip holds
  ``conversations.json`` or, in newer exports, ``conversations-<n>.json``.
* Claude: a JSON array of conversations with ``uuid``, ``name``,
  ``created_at``, ``updated_at`` and ``chat_messages`` (``sender``,
  ``text`` and/or ``content`` blocks). Only ``text`` blocks are read;
  thinking, tool use / results and attachments are skipped.

Both formats are undocumented and drift: the parser is defensive, skips a
conversation it can't read, and fails a file only when the file itself
can't be read (then nothing from that file is listed).

One item per conversation (decision 3). ``item_id`` is ``<provider>:<id>``
and does not depend on the import, so a newer export of the same account
updates conversations instead of duplicating them (decision 4); when two
imports hold the same conversation, the later update time wins.

Large files: the top-level array is decoded one conversation at a time
(:func:`_iter_json_array`), so memory holds one conversation while
listing, plus the rendered text of at most LOAD_WINDOW conversations of
one export while loading.
"""
from __future__ import annotations

import bisect
import hashlib
import io
import json
import os
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ai.knowledge_index import Listing, SourceItem

NAME = "ai_chats"
TITLE = "Imported AI chats"
DESCRIPTION = (
    "Indexes ChatGPT and Claude data-export files you pick; the files are read "
    "in place on this Mac and nothing is sent to OpenAI or Anthropic."
)
TAINT_LABEL = "imported AI chat"
DEFAULT_OPTIONS: dict = {"imports": []}

PROVIDERS = {"chatgpt": "ChatGPT", "claude": "Claude"}
MAX_IMPORTS = 50
# Uncompressed size cap per conversations file (plain or zip member). The
# reader streams, so this bounds time, not memory.
MAX_EXPORT_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_MEMBERS = 200          # conversations-<n>.json members read from one zip
READ_CHUNK = 1 << 20           # characters per read while streaming
LOAD_WINDOW = 500              # conversation texts held while loading (= the index's PASS_BUDGET)
_MEMBER_RE = re.compile(r"^conversations(-\d+)?\.json$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ExportError(Exception):
    """The file as a whole can't be read; ``str()`` is shown to the user."""


# ---------------------------------------------------------------------------
# Streaming a top-level JSON array
# ---------------------------------------------------------------------------


def _iter_json_array(fh: io.TextIOBase, chunk: int = READ_CHUNK) -> Iterator[Any]:
    """Yield the elements of a top-level JSON array one at a time.

    A failed decode at the end of the buffer reads more (doubling, so a big
    element costs O(n) overall); a failure at end of file is malformed."""
    dec = json.JSONDecoder()
    buf, pos, eof = "", 0, False

    def fill(n: int) -> None:
        nonlocal buf, pos, eof
        data = fh.read(n)
        if not data:
            eof = True
            return
        buf, pos = buf[pos:] + data, 0

    def skip_ws() -> None:
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf) or eof:
                return
            fill(chunk)

    skip_ws()
    if pos >= len(buf):
        raise ExportError("is empty")
    if buf[pos] != "[":
        raise ExportError("is not a ChatGPT or Claude export (expected a list of conversations)")
    pos += 1
    first = True
    while True:
        skip_ws()
        if pos >= len(buf):
            raise ExportError("is cut off or not valid JSON")
        if buf[pos] == "]":
            return
        if not first:
            if buf[pos] != ",":
                raise ExportError("is not valid JSON")
            pos += 1
            skip_ws()
        first = False
        want = chunk
        while True:
            try:
                obj, end = dec.raw_decode(buf, pos)
                if end == len(buf) and not eof and not isinstance(obj, (dict, list)):
                    raise ValueError("maybe truncated scalar")
                break
            except ValueError:
                if eof:
                    raise ExportError("is cut off or not valid JSON") from None
                fill(want)
                want *= 2
        pos = end
        yield obj


def _open_texts(path: Path, max_bytes: int) -> Iterator[tuple[str, io.TextIOBase]]:
    """Yield ``(name, text stream)`` for each conversations file in ``path``
    (the file itself, or the matching members of a zip), one open at a time."""
    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError as exc:
        raise ExportError(f"can't be read ({exc.strerror or exc})") from None
    if not is_zip:
        try:
            size = path.stat().st_size
            if size > max_bytes:
                raise ExportError(f"is larger than {max_bytes // (1024 * 1024)} MB")
            fh = open(path, "r", encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise ExportError(f"can't be read ({exc.strerror or exc})") from None
        with fh:
            yield path.name, fh
        return
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExportError(f"is not a readable zip ({exc})") from None
    with zf:
        members = _zip_members(zf)
        if not members:
            raise ExportError("has no conversations.json in the zip (is it a ChatGPT or Claude data export?)")
        for info in members:
            if info.file_size > max_bytes:
                raise ExportError(f"{info.filename} in the zip is larger than {max_bytes // (1024 * 1024)} MB")
            try:
                raw = zf.open(info)
            except (RuntimeError, NotImplementedError, zipfile.BadZipFile, OSError) as exc:
                raise ExportError(f"can't read {info.filename} in the zip ({exc})") from None
            try:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace") as fh:
                    yield info.filename, fh
            except (zipfile.BadZipFile, OSError, EOFError) as exc:
                raise ExportError(f"{info.filename} in the zip is damaged ({exc})") from None


def _zip_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Members named ``conversations.json`` / ``conversations-<n>.json`` at
    the zip's root, or inside ONE top folder when nothing is at the root
    (a re-zipped export). Paths with ``..``, absolute paths and macOS
    ``__MACOSX`` entries are never read. The name is only looked up, never
    used as a path on disk."""
    root: list[zipfile.ZipInfo] = []
    nested: dict[str, list[zipfile.ZipInfo]] = {}
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or name.startswith("/") or "\\" in name:
            continue
        parts = name.split("/")
        if any(p in ("", ".", "..") for p in parts) or parts[0] == "__MACOSX":
            continue
        if not _MEMBER_RE.match(parts[-1]):
            continue
        if len(parts) == 1:
            root.append(info)
        elif len(parts) == 2:
            nested.setdefault(parts[0], []).append(info)
    found = root or (next(iter(nested.values())) if len(nested) == 1 else [])

    def order(info: zipfile.ZipInfo) -> int:
        m = re.search(r"-(\d+)\.json$", info.filename)
        return int(m.group(1)) if m else -1

    return sorted(found, key=order)[:MAX_ZIP_MEMBERS]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def _detect(conv: Any) -> str:
    if not isinstance(conv, dict):
        return ""
    if isinstance(conv.get("mapping"), dict):
        return "chatgpt"
    if isinstance(conv.get("chat_messages"), list) and conv.get("uuid"):
        return "claude"
    return ""


def _epoch(value: Any) -> Optional[float]:
    """Unix seconds from a number (ChatGPT) or an ISO 8601 string (Claude)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    return None


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _chatgpt_message_text(msg: Any) -> tuple[str, str]:
    """``(role, text)`` of one visible user/assistant message, else ``("", "")``."""
    if not isinstance(msg, dict):
        return "", ""
    author = msg.get("author") if isinstance(msg.get("author"), dict) else {}
    role = author.get("role")
    if role not in ("user", "assistant"):
        return "", ""
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    if meta.get("is_visually_hidden_from_conversation") or meta.get("is_user_system_message"):
        return "", ""
    if msg.get("recipient") not in (None, "", "all"):   # a call to a tool
        return "", ""
    weight = msg.get("weight")
    if isinstance(weight, (int, float)) and not isinstance(weight, bool) and weight == 0:
        return "", ""
    content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
    if content.get("content_type") not in ("text", "multimodal_text"):
        return "", ""
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    text = "\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return role, text


def _chatgpt_turns(conv: dict) -> list[tuple[str, str]]:
    mapping = conv.get("mapping") or {}
    node_id = conv.get("current_node")
    if node_id not in mapping:
        # No usable pointer: follow the last child from the root.
        roots = [k for k, n in mapping.items()
                 if isinstance(n, dict) and n.get("parent") not in mapping]
        node_id = roots[0] if roots else None
        seen_down: set = set()
        while node_id in mapping and node_id not in seen_down:
            seen_down.add(node_id)
            kids = [c for c in (mapping[node_id].get("children") or []) if c in mapping]
            if not kids:
                break
            node_id = kids[-1]
    chain: list[tuple[str, str]] = []
    seen: set = set()
    while node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if not isinstance(node, dict):
            break
        role, text = _chatgpt_message_text(node.get("message"))
        if text:
            chain.append((role, text))
        node_id = node.get("parent")
    chain.reverse()
    return chain


def _claude_message_text(msg: Any) -> tuple[str, str]:
    if not isinstance(msg, dict):
        return "", ""
    sender = msg.get("sender")
    role = {"human": "user", "user": "user", "assistant": "assistant"}.get(sender)
    if role is None:
        return "", ""
    blocks = msg.get("content")
    texts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    texts.append(block["text"].strip())
        # A message made only of tool results / thinking is not a turn, even
        # if a flattened ``text`` field repeats it.
        if blocks and not texts:
            return "", ""
    if not texts and isinstance(msg.get("text"), str) and msg["text"].strip():
        texts.append(msg["text"].strip())
    return role, "\n".join(texts)


def _claude_turns(conv: dict) -> list[tuple[str, str]]:
    turns = []
    for msg in conv.get("chat_messages") or []:
        role, text = _claude_message_text(msg)
        if text:
            turns.append((role, text))
    return turns


def _render(turns: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{'You' if r == 'user' else 'Assistant'}: {t}" for r, t in turns)


def _summarise(conv: Any) -> Optional[dict]:
    """``{provider, conv_id, title, updated, date, text}`` or None when the
    conversation can't be read or has no text."""
    provider = _detect(conv)
    if provider == "chatgpt":
        conv_id = conv.get("conversation_id") or conv.get("id")
        title = conv.get("title")
        updated = _epoch(conv.get("update_time"))
        created = _epoch(conv.get("create_time"))
        turns = _chatgpt_turns(conv)
    elif provider == "claude":
        conv_id = conv.get("uuid")
        title = conv.get("name")
        updated = _epoch(conv.get("updated_at"))
        created = _epoch(conv.get("created_at"))
        turns = _claude_turns(conv)
    else:
        return None
    if not isinstance(conv_id, str) or not conv_id.strip() or not turns:
        return None
    conv_id = conv_id.strip()
    if not isinstance(title, str) or not title.strip():
        first = turns[0][1] if turns[0][0] == "user" else ""
        title = (first[:60] + ("…" if len(first) > 60 else "")) if first else "(untitled)"
    return {"provider": provider, "conv_id": conv_id, "title": " ".join(title.split()),
            "updated": updated, "date": _iso(updated or created), "text": _render(turns)}


def _link(provider: str, conv_id: str) -> str:
    """The conversation's web page, only for an id shaped like the ones
    those URLs take (a UUID); else empty."""
    if not _UUID_RE.match(conv_id):
        return ""
    if provider == "chatgpt":
        return f"https://chatgpt.com/c/{conv_id}"
    if provider == "claude":
        return f"https://claude.ai/chat/{conv_id}"
    return ""


def _version(summary: dict) -> str:
    if summary["updated"] is not None:
        return f"{summary['conv_id']}:{summary['updated']!r}"
    digest = hashlib.sha256(summary["text"].encode("utf-8")).hexdigest()[:16]
    return f"{summary['conv_id']}:h{digest}"


def _scan(path: Path, max_bytes: int, keep_text: Optional[set] = None
          ) -> tuple[list[dict], dict[str, int]]:
    """Every readable conversation of one export, without text unless its
    item id is in ``keep_text``. Raises :class:`ExportError`; on any error
    nothing from the file is returned."""
    out: list[dict] = []
    counts: dict[str, int] = {}
    seen_any = False
    for _name, fh in _open_texts(path, max_bytes):
        try:
            for conv in _iter_json_array(fh):
                seen_any = True
                summary = _summarise(conv)
                if summary is None:
                    continue
                summary["item_id"] = f"{summary['provider']}:{summary['conv_id']}"
                summary["version"] = _version(summary)
                if keep_text is None or summary["item_id"] not in keep_text:
                    summary.pop("text")
                counts[summary["provider"]] = counts.get(summary["provider"], 0) + 1
                out.append(summary)
        except UnicodeError as exc:
            raise ExportError(f"is not UTF-8 text ({exc})") from None
        except (zipfile.BadZipFile, EOFError, OSError) as exc:
            raise ExportError(f"is damaged ({exc})") from None
    if seen_any and not out:
        raise ExportError("has no ChatGPT or Claude conversations in it (is it the right file?)")
    return out, counts


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def inspect_export(path: str, *, max_bytes: int = MAX_EXPORT_BYTES) -> dict:
    """Validate a picked file: ``{"provider", "conversations", "error"}``.
    Reads the file once; indexes nothing."""
    p = Path(str(path)).expanduser()
    if not p.is_file():
        return {"provider": "", "conversations": 0, "error": f"{p.name or path} is missing"}
    try:
        _found, counts = _scan(p, max_bytes)
    except ExportError as exc:
        return {"provider": "", "conversations": 0, "error": f"{p.name} {exc}"}
    if not counts:
        return {"provider": "", "conversations": 0, "error": f"{p.name} has no conversations"}
    provider = max(counts, key=lambda k: counts[k])
    return {"provider": provider, "conversations": sum(counts.values()), "error": ""}


def make_import(path: str, clock: Callable[[], float] = time.time,
                new_id: Optional[Callable[[], str]] = None, *,
                max_bytes: int = MAX_EXPORT_BYTES) -> dict:
    """The options entry for a picked file (after :func:`inspect_export`).
    Raises ValueError with a user-readable message if the file won't do."""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        raise ValueError("Pick the export file again (the path must be absolute).")
    got = inspect_export(str(p), max_bytes=max_bytes)
    if got["error"]:
        raise ValueError(got["error"])
    return {
        "id": (new_id or (lambda: uuid.uuid4().hex[:12]))(),
        "path": str(p),
        "provider": got["provider"],
        "added_at": _iso(clock()),
        "conversations": got["conversations"],
    }


def validate_options(options: Any) -> dict:
    """A cleaned copy of ``options``; ValueError with a readable message."""
    if options is None:
        return {"imports": []}
    if not isinstance(options, dict):
        raise ValueError("Imported AI chats: settings must be an object.")
    imports = options.get("imports", [])
    if not isinstance(imports, list):
        raise ValueError("Imported AI chats: 'imports' must be a list.")
    if len(imports) > MAX_IMPORTS:
        raise ValueError(f"Imported AI chats: at most {MAX_IMPORTS} imports.")
    cleaned, ids = [], set()
    for entry in imports:
        if not isinstance(entry, dict):
            raise ValueError("Imported AI chats: each import must be an object.")
        imp_id = entry.get("id")
        if not isinstance(imp_id, str) or not _ID_RE.match(imp_id):
            raise ValueError("Imported AI chats: an import has no valid id.")
        if imp_id in ids:
            raise ValueError(f"Imported AI chats: import id {imp_id} appears twice.")
        ids.add(imp_id)
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValueError("Imported AI chats: an import has no file path.")
        p = Path(path.strip()).expanduser()
        if not p.is_absolute():
            raise ValueError(f"Imported AI chats: {path} is not a full path.")
        if p.suffix.lower() not in (".json", ".zip"):
            raise ValueError(f"Imported AI chats: {p.name} is not a .json or .zip export.")
        provider = entry.get("provider")
        if provider not in PROVIDERS:
            raise ValueError(f"Imported AI chats: unknown provider {provider!r}.")
        added = entry.get("added_at", "")
        if not isinstance(added, str):
            raise ValueError("Imported AI chats: 'added_at' must be text.")
        count = entry.get("conversations", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Imported AI chats: 'conversations' must be a whole number.")
        cleaned.append({"id": imp_id, "path": str(p), "provider": provider,
                        "added_at": added, "conversations": count})
    return {"imports": cleaned}


class _Loader:
    """Renders text for winning conversations. On a miss it re-reads that
    one export and keeps the text of the next :data:`LOAD_WINDOW` wanted
    conversations (items are listed sorted by file then id, and the index
    loads them in that order), so memory holds at most a window of one
    export's text, not the whole export."""

    def __init__(self, wanted: dict[str, dict[str, str]], max_bytes: int) -> None:
        self.wanted = wanted          # path -> {item id: winning version}
        self.order = {p: sorted(ids) for p, ids in wanted.items()}
        self.max_bytes = max_bytes
        self.path: Optional[str] = None
        self.texts: dict[str, str] = {}

    def load(self, path: str, item_id: str) -> dict:
        if self.path != path or item_id not in self.texts:
            self.path, self.texts = None, {}
            order = self.order.get(path, [])
            start = bisect.bisect_left(order, item_id)
            keep = set(order[start:start + LOAD_WINDOW])
            want = self.wanted.get(path, {})
            found, _counts = _scan(Path(path), self.max_bytes, keep_text=keep)
            for s in found:
                if "text" in s and want.get(s["item_id"]) == s["version"]:
                    self.texts.setdefault(s["item_id"], s["text"])
            self.path = path
        text = self.texts.pop(item_id, None)
        if text is None:
            raise ExportError(f"{Path(path).name} no longer holds this conversation")
        return {"text": text}


def listing(options: dict, **overrides: Any) -> Listing:
    """One item per conversation across every import (decision 3/4).

    A missing or unreadable import gives ``error`` and ``complete=False`` so
    the index never prunes its conversations. A removed import is simply
    not listed, so its conversations go (unless another import has them).
    ``overrides``: ``max_bytes`` (size cap, for tests)."""
    max_bytes = int(overrides.get("max_bytes", MAX_EXPORT_BYTES))
    try:
        opts = validate_options(options)
    except ValueError as exc:
        return Listing([], complete=False, error=str(exc))
    best: dict[str, tuple[tuple, dict, str]] = {}
    errors: list[str] = []
    scanned: dict[str, list[dict]] = {}
    for order, imp in enumerate(opts["imports"]):
        path = imp["path"]
        name = Path(path).name
        if path not in scanned:
            if not Path(path).is_file():
                errors.append(f"{name} is missing (its chats are kept until you remove the import)")
                continue
            try:
                scanned[path], _counts = _scan(Path(path), max_bytes)
            except ExportError as exc:
                errors.append(f"{name} {exc}")
                continue
        for s in scanned[path]:
            rank = (s["updated"] if s["updated"] is not None else float("-inf"), order)
            held = best.get(s["item_id"])
            if held is None or rank > held[0]:
                best[s["item_id"]] = (rank, s, path)
    wanted: dict[str, dict[str, str]] = {}
    for item_id, (_rank, s, path) in best.items():
        wanted.setdefault(path, {})[item_id] = s["version"]
    loader = _Loader(wanted, max_bytes)
    items = []
    # Grouped by file so each export is re-read at most once while loading.
    for item_id, (_rank, s, path) in sorted(best.items(), key=lambda kv: (kv[1][2], kv[0])):
        label = PROVIDERS[s["provider"]]
        items.append(SourceItem(
            item_id=item_id, version=s["version"], title=f"{label}: {s['title']}",
            link=_link(s["provider"], s["conv_id"]), date=s["date"],
            load=(lambda p=path, i=item_id: loader.load(p, i)),
        ))
    return Listing(items, complete=not errors, error="; ".join(errors))


def choices(options: dict, **overrides: Any) -> dict:
    """What Settings lists: each import's file name, provider, date,
    conversation count (as imported) and whether the file is still there."""
    try:
        opts = validate_options(options)
    except ValueError as exc:
        return {"imports": [], "error": str(exc)}
    rows = []
    for imp in opts["imports"]:
        p = Path(imp["path"])
        rows.append({
            "id": imp["id"], "file_name": p.name, "path": imp["path"],
            "provider": imp["provider"], "provider_label": PROVIDERS[imp["provider"]],
            "added_at": imp["added_at"], "conversations": imp["conversations"],
            "present": os.path.isfile(p),
        })
    return {"imports": rows, "error": ""}
