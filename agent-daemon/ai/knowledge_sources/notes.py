"""Apple Notes as a knowledge-index source (#139).

One item per note: title, folder, modified date and the body with its HTML
stripped. Read-only: the two scripts below only read properties; nothing
here creates, changes or deletes a note (``tools/notes.py`` does that, and
only behind an approval card).

How it reads (decision 1, approach (a)): JavaScript for Automation through
``osascript``, the same Automation permission ``tools/notes.py`` uses.

* LISTING: one script call walks every account's folders and bulk-fetches
  ``id``, ``name``, ``modificationDate`` and ``passwordProtected`` of each
  folder's notes (four Apple events per folder, not per note). That is the
  whole listing; ``version`` is the modification date, so an unchanged
  note costs nothing more. Measured 2026-11-20 on the user's library:
  815 notes in 0.3 s (one osascript call, 6 folders).
* BODIES: fetched lazily in ``load()``, only for notes the indexer found
  changed. One body costs about 30 ms inside Notes plus about 0.15 s to start
  ``osascript``, so a load fetches a BATCH (up to :data:`BATCH_SIZE` ids
  per call) when the caller tells :func:`listing` which versions it already
  holds (``known_versions``); without that, each load is its own call.
  Measured: 25 bodies in one call 0.94 s, versus 0.17 s per single call.

Locked (password-protected) notes are never listed and never loaded; the
body script checks the flag again before reading, in case a note was
locked after the listing. "Recently Deleted" is skipped (by folder name,
see :data:`DELETED_FOLDER_NAMES`). Inline images (``<img src="data:...">``)
are cut inside the script so they never cross the pipe.

Link: empty. The scripting interface gives only an ``x-coredata://`` id,
which is not an openable URL; the ``notes://showNote?identifier=<UUID>``
form needs the note's store UUID, which scripting does not expose.

When approach (b) (``NoteStore.sqlite``) would be needed: only if a full
pass over 1,000 notes took over 5 minutes (decision 1). At the measured
rates a full first pass over 1,000 notes is about 1 minute batched and
about 3.5 minutes unbatched, and later passes load only edited notes. (b)
would need Full Disk Access and parsing of undocumented gzipped protobuf
bodies; it is not implemented.

No network calls. Errors (no Automation permission, Notes not scriptable,
a timeout, garbled output) come back as ``Listing([], complete=False,
error=...)``, never as an exception.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Mapping, Optional

from ai.knowledge_index import Listing, SourceItem

NAME = "apple_notes"
TITLE = "Notes"
DESCRIPTION = ("Your notes from the Notes app, read through Automation; the "
               "index stays on this Mac and locked notes are never read.")
TAINT_LABEL = "indexed note"
DEFAULT_OPTIONS: dict[str, Any] = {"exclude_folders": []}

LIST_TIMEOUT_S = 60.0
BODY_TIMEOUT_S = 60.0
BATCH_SIZE = 25
MAX_NOTES = 20_000
MAX_EXCLUDES = 200
MAX_FOLDER_CHARS = 300
FOLDER_SEP = " / "

# Notes' own name for its trash folder, in the languages most likely here.
DELETED_FOLDER_NAMES: tuple[str, ...] = (
    "Recently Deleted", "Zuletzt gelöscht", "Suppressions récentes",
    "Eliminado recientemente", "Eliminati di recente", "Recentemente apagadas",
    "Onlangs verwijderd", "最近删除", "最近削除した項目",
)

# argv[0] = JSON list of folder names to skip. Returns JSON
# {"folders": [{"path": [account, folder, ...], "ids": [...], "names": [...],
#   "modified": [...], "locked": [...]}]}.
LIST_SCRIPT = r'''
function run(argv) {
  const app = Application("Notes");
  const skip = new Set(JSON.parse(argv[0] || "[]"));
  const seen = new Set();
  const folders = [];
  function walk(f, path) {
    const fid = f.id();
    if (seen.has(fid)) return;
    seen.add(fid);
    const name = f.name();
    if (skip.has(name)) return;
    const p = path.concat([name]);
    const ns = f.notes;
    folders.push({path: p, ids: ns.id(), names: ns.name(),
                  modified: ns.modificationDate(), locked: ns.passwordProtected()});
    for (const sub of f.folders()) walk(sub, p);
  }
  for (const a of app.accounts()) {
    const an = a.name();
    for (const f of a.folders()) {
      let top = true;
      try { top = f.container().properties().pcls === "account"; } catch (e) {}
      if (top) walk(f, [an]);
    }
  }
  return JSON.stringify({folders: folders});
}
'''

# argv = note ids. Returns JSON {id: {"body": html} | {"locked": true} | {"missing": true}}.
BODY_SCRIPT = r'''
function run(argv) {
  const app = Application("Notes");
  const out = {};
  for (const id of argv) {
    try {
      const n = app.notes.byId(id);
      if (n.passwordProtected()) { out[id] = {locked: true}; continue; }
      out[id] = {body: String(n.body() || "").replace(/<img\b[^>]*>/gi, " ")};
    } catch (e) {
      out[id] = {missing: true};
    }
  }
  return JSON.stringify(out);
}
'''

# runner(script, args, timeout) -> stdout; raises like subprocess.run(check=True).
Runner = Callable[[str, list[str], float], str]


class NotesError(Exception):
    """A user-readable reason Notes could not be read."""


def _osascript(script: str, args: list[str], timeout: float) -> str:
    proc = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script, "--", *args],
        check=True, capture_output=True, text=True, timeout=timeout,
    )
    return proc.stdout or ""


def _explain(exc: BaseException, timeout: float) -> str:
    """A clear message; never echoes script output (it could hold note text)."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return (f"Notes didn't answer within {int(timeout)} seconds (it may be waiting on "
                "a permission prompt, or busy syncing). Try again later.")
    if isinstance(exc, FileNotFoundError):
        return "osascript is missing, so Notes can't be read on this Mac."
    if isinstance(exc, subprocess.CalledProcessError):
        err = str(exc.stderr or "")
        code = re.search(r"\((-\d+)\)", err)
        code_s = code.group(1) if code else ""
        if code_s == "-1743" or "Not authorized" in err or "not allowed" in err.lower():
            return ("bot-inator isn't allowed to control Notes. Turn it on in System Settings "
                    "› Privacy & Security › Automation, then index again.")
        if code_s in ("-1728", "-1708", "-10814", "-600", "-2700") or "Can't get application" in err:
            return "Notes isn't available to scripts on this Mac (is the Notes app installed?)."
        return f"Notes could not be read (osascript error {code_s or exc.returncode})."
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "Notes returned something unexpected; nothing was indexed."
    return f"Notes could not be read ({type(exc).__name__})."


def validate_options(options: Any) -> dict[str, Any]:
    """A cleaned copy of ``options``; ValueError with a readable message."""
    if options is None:
        return json.loads(json.dumps(DEFAULT_OPTIONS))
    if not isinstance(options, Mapping):
        raise ValueError("Notes options must be an object.")
    unknown = sorted(str(k) for k in options if k not in DEFAULT_OPTIONS)
    if unknown:
        raise ValueError(f"Unknown Notes option: {', '.join(unknown)}.")
    raw = options.get("exclude_folders", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("Excluded folders must be a list of folder names.")
    if len(raw) > MAX_EXCLUDES:
        raise ValueError(f"At most {MAX_EXCLUDES} folders can be excluded.")
    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("Each excluded folder must be a folder name.")
        name = entry.strip()
        if not name:
            raise ValueError("An excluded folder name is empty.")
        if len(name) > MAX_FOLDER_CHARS:
            raise ValueError("An excluded folder name is too long.")
        if any(ord(c) < 32 for c in name):
            raise ValueError("An excluded folder name has control characters.")
        if name not in cleaned:
            cleaned.append(name)
    return {"exclude_folders": cleaned}


def _excluded(path: str, excludes: list[str]) -> bool:
    return any(path == ex or path.startswith(ex + FOLDER_SEP) for ex in excludes)


def _list_notes(runner: Runner, timeout: float) -> list[dict[str, Any]]:
    """Every listable note as ``{id, name, folder, modified}``, locked and
    Recently Deleted ones left out. Raises on any failure."""
    out = runner(LIST_SCRIPT, [json.dumps(list(DELETED_FOLDER_NAMES))], timeout)
    data = json.loads(out)
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for folder in data["folders"]:
        path_parts = [str(p) for p in folder["path"]]
        if any(p in DELETED_FOLDER_NAMES for p in path_parts[1:]):
            continue
        path = FOLDER_SEP.join(path_parts)
        ids, names = folder["ids"], folder["names"]
        modified, locked = folder["modified"], folder["locked"]
        if not (len(ids) == len(names) == len(modified) == len(locked)):
            raise ValueError("ragged folder listing")
        for nid, name, mod, lock in zip(ids, names, modified, locked):
            nid = str(nid or "")
            if not nid or nid in seen or lock is not False:
                continue  # a locked (or unknown-lock) note is never listed
            seen.add(nid)
            notes.append({"id": nid, "name": str(name or ""), "folder": path,
                          "modified": str(mod or "")})
    return notes


class _BodyLoader:
    """Fetches bodies on demand; with ``wanted`` (ids the indexer will load),
    the first load fetches the next batch of wanted ids in one call."""

    def __init__(self, runner: Runner, timeout: float, batch_size: int,
                 order: list[str], wanted: Optional[set[str]]) -> None:
        self.runner, self.timeout = runner, timeout
        self.batch_size = max(1, int(batch_size))
        self.order, self.wanted = order, wanted
        self.cache: dict[str, dict[str, Any]] = {}

    def _batch_for(self, nid: str) -> list[str]:
        if not self.wanted:
            return [nid]
        batch = [nid]
        start = self.order.index(nid) + 1 if nid in self.order else len(self.order)
        for other in self.order[start:]:
            if len(batch) >= self.batch_size:
                break
            if other in self.wanted and other not in self.cache:
                batch.append(other)
        return batch

    def body(self, nid: str) -> str:
        got = self.cache.pop(nid, None)
        if got is None:
            batch = self._batch_for(nid)
            try:
                result = json.loads(self.runner(BODY_SCRIPT, batch, self.timeout))
            except Exception as exc:
                raise NotesError(_explain(exc, self.timeout)) from None
            if not isinstance(result, dict):
                raise NotesError("Notes returned something unexpected.")
            for other in batch[1:]:
                if isinstance(result.get(other), dict):
                    self.cache[other] = result[other]
            got = result.get(nid)
        if not isinstance(got, dict) or got.get("locked"):
            raise NotesError("note is locked; not read")
        if "body" not in got:
            raise NotesError("note is gone")
        return str(got["body"] or "")


def _load(loader: _BodyLoader, note: dict[str, Any]) -> dict[str, str]:
    from tools.read_email import html_to_text

    text = html_to_text(loader.body(note["id"]))
    return {"text": f"Folder: {note['folder']}\n\n{text}"}


def listing(options: Optional[dict] = None, *, runner: Optional[Runner] = None,
            known_versions: Optional[Mapping[str, str]] = None,
            list_timeout: float = LIST_TIMEOUT_S, body_timeout: float = BODY_TIMEOUT_S,
            batch_size: int = BATCH_SIZE) -> Listing:
    """Every unlocked note outside Recently Deleted and the excluded folders.

    ``runner`` replaces osascript (tests). ``known_versions`` (``{item_id:
    version}`` the index already holds, optional) lets a load batch the
    other changed notes into the same call; it changes nothing else."""
    try:
        opts = validate_options(options)
    except ValueError as exc:
        return Listing([], complete=False, error=str(exc))
    run = runner or _osascript
    try:
        notes = _list_notes(run, list_timeout)
    except Exception as exc:
        return Listing([], complete=False, error=_explain(exc, list_timeout))
    notes = [n for n in notes if not _excluded(n["folder"], opts["exclude_folders"])]
    complete = True
    if len(notes) > MAX_NOTES:
        notes, complete = notes[:MAX_NOTES], False
    wanted = None
    if known_versions is not None:
        wanted = {n["id"] for n in notes if known_versions.get(n["id"]) != n["modified"]}
    loader = _BodyLoader(run, body_timeout, batch_size, [n["id"] for n in notes], wanted)
    items = [SourceItem(
        item_id=n["id"], version=n["modified"], title=n["name"] or "(untitled note)",
        link="", date=n["modified"],
        load=(lambda note=n: _load(loader, note)),
    ) for n in notes]
    return Listing(items, complete=complete)


def choices(options: Optional[dict] = None, *, runner: Optional[Runner] = None,
            list_timeout: float = LIST_TIMEOUT_S) -> dict[str, Any]:
    """Folders for the optional exclude list: ``{"folders": [{"path",
    "notes"}], "error"}`` (locked notes and Recently Deleted not counted)."""
    try:
        notes = _list_notes(runner or _osascript, list_timeout)
    except Exception as exc:
        return {"folders": [], "error": _explain(exc, list_timeout)}
    counts: dict[str, int] = {}
    for n in notes:
        counts[n["folder"]] = counts.get(n["folder"], 0) + 1
    return {"folders": [{"path": p, "notes": c} for p, c in sorted(counts.items())], "error": ""}
