"""Knowledge index source ``browser_history`` (#140): pages the user visited.

One :class:`~ai.knowledge_index.SourceItem` per URL (not per visit), merged
across every browser profile found: title, URL, last visit, visit count and
which browsers saw it. ``item_id`` = a hash of the normalised URL, ``link``
= that URL, ``date`` = the last visit (UTC ISO).

History files read (copied to a private temp dir first, with their
``-wal`` / ``-shm`` when present, because the browsers hold them open; the
copy is opened read-only and deleted afterwards):

* Safari    ``~/Library/Safari/History.db`` (``history_items`` +
            ``history_visits``; Mac absolute time, seconds since 2001).
            Needs Full Disk Access; without it the error says so.
* Chromium  ``<root>/{Default,Profile N}/History`` (``urls``; WebKit
            microseconds since 1601) for Chrome (``Google/Chrome``), Arc
            (``Arc/User Data``), Brave (``BraveSoftware/Brave-Browser``) and
            Edge (``Microsoft Edge``) under ``~/Library/Application Support``.
            Profile names come from the browser's ``Local State``.
* Firefox   ``~/Library/Application Support/Firefox/Profiles/*/places.sqlite``
            (``moz_places``; microseconds since 1970). Names from
            ``profiles.ini``.

Rules:

* LOOK-BACK. Only URLs whose last visit is newer than ``lookback_days``
  (7, 30, 90, 365, or 0 = everything) are listed. The listing is complete,
  so shortening the limit prunes the older rows (the newest
  :data:`MAX_ITEMS` URLs at most; anything older than those is pruned too).
* ONLY http/https. ``file:``, ``about:``, ``chrome:``, ``javascript:`` ... are
  skipped. EXCLUDED DOMAINS (and their subdomains) are never listed.
* CREDENTIALS AND TOKENS. A ``user:password@`` part is dropped; the
  fragment is always dropped; the WHOLE query string is dropped when any
  parameter looks secret (see :func:`clean_url`). Such a URL is never
  fetched for page text.
* ONE BROWSER FAILING DOES NOT HIDE THE OTHERS: what could be read is
  listed, the failures go in ``error`` and ``complete`` is False. The
  indexer prunes only on ``complete and not error``, so on such a pass
  nothing at all is pruned (the safe side: a single Listing cannot say
  "prune Safari's rows but not Chrome's").
* PAGE TEXT (``page_text`` option, off by default) is the only network use
  (invariant 7c). Off: no fetch callable is called and nothing here touches
  the network. On: ``load()`` of a changed item fetches the page through
  ``fetch`` (default :func:`fetch_public_page`, #124's checks, transport and
  ledger row). The item ``version`` is ``page:<window>``, where the window
  is a 30-day period staggered per URL, so an unchanged URL in the same
  window is skipped by the indexer without any call: at most one fetch per
  URL per 30 days. Consequence: in page-text mode the stored title / date /
  visit count refresh once per window, not per visit. Literal local and
  private hosts (``localhost``, ``*.local``, single-label names, private
  IPs) are refused here before ``fetch`` is called; names that RESOLVE to
  a private address are refused by ``fetch_public_page`` (DNS check, pinned
  connection). A failed fetch is not fatal: title + URL are indexed and the
  page is retried in the next window. Fetches are at least
  :data:`FETCH_GAP_SECONDS` apart.

No network import in this module: the default fetch lazily uses
``tools.web_search`` (``check_url``, ``_get`` -> ``_record``, ``extract_text``).
"""
from __future__ import annotations

import configparser
import contextlib
import hashlib
import importlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from ai.knowledge_index import Listing, SourceItem

NAME = "browser_history"
TITLE = "Browser history"
DESCRIPTION = ("Titles and addresses of pages you visited in Safari, Chrome, Arc, Brave, "
               "Edge and Firefox, read from their history files; they stay on this Mac.")
TAINT_LABEL = "indexed web page"

LOOKBACK_CHOICES: tuple[int, ...] = (7, 30, 90, 365, 0)
LOOKBACK_LABELS = {7: "7 days", 30: "30 days", 90: "90 days", 365: "1 year", 0: "Everything"}

# Suggested, user-editable. Sign-in pages (their titles and URLs can carry
# account names and one-time links), web mail (page titles are other
# people's subject lines) and a few large banks / payment sites. Health
# portals are usually per-provider hosts (e.g. mychart.<clinic>.org), so no
# safe generic entry exists: the user adds theirs.
SUGGESTED_EXCLUDED_DOMAINS: tuple[str, ...] = (
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "appleid.apple.com", "idmsa.apple.com", "okta.com", "auth0.com",
    "mail.google.com", "outlook.live.com", "outlook.office.com",
    "paypal.com", "venmo.com", "chase.com", "bankofamerica.com", "wellsfargo.com",
    "capitalone.com", "americanexpress.com",
)

DEFAULT_OPTIONS: dict[str, Any] = {
    "lookback_days": 90,
    "page_text": False,
    "excluded_domains": list(SUGGESTED_EXCLUDED_DOMAINS),
    "profiles": None,  # None = every profile found; else a list of profile keys
}

MAX_ITEMS = 50_000             # newest URLs listed; older ones are pruned
MAX_URL_CHARS = 2_000
MAX_EXCLUDED = 500
MAX_PROFILES = 100
MAX_PAGE_CHARS = 20_000
FETCH_EVERY = 30 * 86400.0     # at most one fetch per URL per this
FETCH_GAP_SECONDS = 1.0        # between two fetches in one pass
FETCH_TOOL_NAME = "knowledge_browser"  # the ledger row's tool name

MAC_EPOCH = 978307200.0                 # 2001-01-01 in Unix seconds
WEBKIT_EPOCH_OFFSET = 11644473600.0     # 1601-01-01 -> 1970-01-01, seconds

BROWSER_TITLES = {"safari": "Safari", "chrome": "Chrome", "arc": "Arc",
                  "brave": "Brave", "edge": "Edge", "firefox": "Firefox"}
_CHROMIUM_ROOTS = {
    "chrome": "Library/Application Support/Google/Chrome",
    "arc": "Library/Application Support/Arc/User Data",
    "brave": "Library/Application Support/BraveSoftware/Brave-Browser",
    "edge": "Library/Application Support/Microsoft Edge",
}
_SAFARI_ROOT = "Library/Safari"
_FIREFOX_ROOT = "Library/Application Support/Firefox"

FULL_DISK_ACCESS_ERROR = ("Safari's history can't be read: give the bot-inator daemon Full "
                          "Disk Access in System Settings > Privacy & Security.")

_PROFILE_KEY_RE = re.compile(r"^(safari|chrome|arc|brave|edge|firefox):[^/\\\x00]{1,200}$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def _clean_domain(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("Each excluded site must be text, like example.com.")
    text = raw.strip().lower()
    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            text = ""
    else:
        text = text.split("/", 1)[0].split(":", 1)[0]
    text = text.lstrip("*").strip(".")
    if text.startswith("www."):
        text = text[4:]
    try:
        text = text.encode("idna").decode("ascii") if text else ""
    except UnicodeError:
        text = ""
    if not text or not _DOMAIN_RE.match(text):
        raise ValueError(f"{str(raw)[:80]!r} isn't a site address (try example.com).")
    return text


def validate_options(options: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """A cleaned copy of ``options`` (missing keys get defaults, unknown keys
    are dropped). Raises ValueError with a message for the user."""
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ValueError("Browser history settings must be a set of options.")
    out = json.loads(json.dumps(DEFAULT_OPTIONS))

    days = options.get("lookback_days", out["lookback_days"])
    if isinstance(days, str) and days.strip().isdigit():
        days = int(days.strip())
    if isinstance(days, bool) or not isinstance(days, int) or days not in LOOKBACK_CHOICES:
        raise ValueError("Look back must be 7, 30, 90 or 365 days, or everything (0).")
    out["lookback_days"] = days

    page_text = options.get("page_text", False)
    if not isinstance(page_text, bool):
        raise ValueError("Page text must be on or off.")
    out["page_text"] = page_text

    domains = options.get("excluded_domains", out["excluded_domains"])
    if domains is None:
        domains = []
    if isinstance(domains, (str, bytes)) or not isinstance(domains, (list, tuple)):
        raise ValueError("Excluded sites must be a list, like [\"example.com\"].")
    if len(domains) > MAX_EXCLUDED:
        raise ValueError(f"At most {MAX_EXCLUDED} excluded sites.")
    cleaned: list[str] = []
    for d in domains:
        c = _clean_domain(d)
        if c not in cleaned:
            cleaned.append(c)
    out["excluded_domains"] = cleaned

    profiles = options.get("profiles", None)
    if profiles is not None:
        if isinstance(profiles, (str, bytes)) or not isinstance(profiles, (list, tuple)):
            raise ValueError("Browser profiles must be a list (or left out for all of them).")
        if len(profiles) > MAX_PROFILES:
            raise ValueError(f"At most {MAX_PROFILES} browser profiles.")
        keys: list[str] = []
        for p in profiles:
            if not isinstance(p, str) or not _PROFILE_KEY_RE.match(p):
                raise ValueError(f"{str(p)[:80]!r} isn't a browser profile from the list.")
            if p not in keys:
                keys.append(p)
        profiles = keys
    out["profiles"] = profiles
    return out


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_SECRET_KEY_PARTS = ("token", "secret", "passw", "pwd", "signature", "session", "sessid",
                     "apikey", "api_key", "api-key", "credential", "saml", "jwt", "nonce",
                     "ticket", "auth")
_SECRET_KEYS = frozenset({"code", "state", "key", "sig", "sid", "otp", "pin", "hmac", "hash"})
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_\-.~+/=%]{32,}$")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")


def _secret_param(key: str, value: str) -> bool:
    k = key.strip().lower()
    if k in _SECRET_KEYS or any(part in k for part in _SECRET_KEY_PARTS):
        return True
    if _JWT_RE.search(value):
        return True
    return bool(_OPAQUE_RE.match(value) and re.search(r"\d", value) and re.search(r"[A-Za-z]", value))


def _secretish(query: str) -> bool:
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return True
    if not pairs and query:
        return bool(_OPAQUE_RE.match(query) or _JWT_RE.search(query))
    return any(_secret_param(k, v) for k, v in pairs)


def clean_url(raw: Any) -> tuple[Optional[str], bool]:
    """``(normalised URL, sanitised)`` or ``(None, False)`` to skip.

    Only http/https with a host. Normalised: scheme and host lower-cased,
    default port and fragment dropped, empty path -> ``/``. ``sanitised``
    is True when something secret-looking was removed: a ``user:pass@``
    part, or the whole query string because one parameter's NAME looks
    secret (token, auth, session, password, signature, key, code, state,
    sig, sid, otp ...) or its VALUE looks like a token (a JWT, or 32+ URL
    characters mixing letters and digits). A fragment holding a token marks
    it sanitised too (OAuth puts access tokens there)."""
    if not isinstance(raw, str):
        return None, False
    text = raw.strip()
    if not text or len(text) > 4 * MAX_URL_CHARS:
        return None, False
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None, False
    if scheme not in ("http", "https") or not host:
        return None, False
    sanitised = parts.username is not None or parts.password is not None
    query = parts.query
    if query and _secretish(query):
        query, sanitised = "", True
    if parts.fragment and _secretish(parts.fragment):
        sanitised = True
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc += f":{port}"
    url = urlunsplit((scheme, netloc, parts.path or "/", query, ""))
    if len(url) > MAX_URL_CHARS:
        url = urlunsplit((scheme, netloc, parts.path or "/", "", ""))
        sanitised = True
        if len(url) > MAX_URL_CHARS:
            return None, False
    return url, sanitised


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_excluded(url: str, excluded: Any) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in excluded)


def _never_fetch_host(host: str) -> bool:
    """Local or private by its NAME alone (no DNS here)."""
    bare = host.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None
    if ip is not None:
        mapped = getattr(ip, "ipv4_mapped", None)
        ip = mapped or ip
        return not ip.is_global or ip.is_multicast
    if "." not in bare:
        return True  # localhost, intranet single-label names
    return bare.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa",
                          ".intranet", ".corp", ".test", ".invalid", ".example"))


def item_id_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def fetch_window(url: str, now: float) -> int:
    """The 30-day period ``now`` is in for ``url``, staggered per URL so all
    URLs don't come due on the same day."""
    offset = int(hashlib.sha256(b"w:" + url.encode("utf-8")).hexdigest()[:8], 16) % int(FETCH_EVERY)
    return int((now - offset) // FETCH_EVERY)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    key: str          # "chrome:Profile 1"
    browser: str      # "chrome"
    profile: str      # folder name ("Default", "Profile 1", "abcd.default-release")
    name: str         # what the browser calls it
    path: Path        # the history file
    family: str       # "safari" | "chromium" | "firefox"

    @property
    def label(self) -> str:
        title = BROWSER_TITLES[self.browser]
        return title if self.browser == "safari" else f"{title} ({self.name})"


def _roots(home: Path, roots: Optional[Mapping[str, Any]]) -> dict[str, Path]:
    out = {"safari": home / _SAFARI_ROOT, "firefox": home / _FIREFOX_ROOT}
    out.update({b: home / r for b, r in _CHROMIUM_ROOTS.items()})
    for key, value in (roots or {}).items():
        if key not in out:
            raise TypeError(f"unknown browser root {key!r}")
        out[key] = Path(value)
    return out


def _present(path: Path) -> bool:
    """True if the file exists or can't be checked (no permission): an
    unreadable Safari file must be listed so its error shows."""
    try:
        os.stat(path)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _chromium_names(root: Path) -> dict[str, str]:
    try:
        data = json.loads((root / "Local State").read_text("utf-8"))
        cache = data["profile"]["info_cache"]
        return {str(k): str(v.get("name") or k) for k, v in cache.items() if isinstance(v, dict)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _firefox_names(root: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(root / "profiles.ini", encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError):
        return {}
    names: dict[str, str] = {}
    for section in parser.sections():
        rel = parser.get(section, "Path", fallback="")
        name = parser.get(section, "Name", fallback="")
        if rel and name:
            names[Path(rel).name] = name
    return names


def find_profiles(home: Path, roots: Optional[Mapping[str, Any]] = None) -> list[Profile]:
    """Every browser profile with a history file, in a stable order."""
    r = _roots(Path(home), roots)
    found: list[Profile] = []
    safari = r["safari"] / "History.db"
    if _present(safari):
        found.append(Profile("safari:default", "safari", "default", "Safari", safari, "safari"))
    for browser in _CHROMIUM_ROOTS:
        root = r[browser]
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        names = _chromium_names(root)
        for d in dirs:
            if d.name != "Default" and not re.fullmatch(r"Profile \d+", d.name):
                continue
            if (d / "History").is_file():
                found.append(Profile(f"{browser}:{d.name}", browser, d.name,
                                     names.get(d.name, d.name), d / "History", "chromium"))
    ff = r["firefox"]
    try:
        ff_dirs = sorted(p for p in (ff / "Profiles").iterdir() if p.is_dir())
    except OSError:
        ff_dirs = []
    names = _firefox_names(ff) if ff_dirs else {}
    for d in ff_dirs:
        if (d / "places.sqlite").is_file():
            found.append(Profile(f"firefox:{d.name}", "firefox", d.name,
                                 names.get(d.name, d.name), d / "places.sqlite", "firefox"))
    return found


# ---------------------------------------------------------------------------
# Reading one history file
# ---------------------------------------------------------------------------


class ReadError(Exception):
    """A history file that could not be read; the message is for the user."""


@contextlib.contextmanager
def _copied(path: Path, family: str) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a private copy of ``path`` (+ -wal/-shm),
    deleted on exit."""
    tmp = tempfile.mkdtemp(prefix="bot-inator-history-")
    conn: Optional[sqlite3.Connection] = None
    try:
        dest = Path(tmp) / path.name
        try:
            shutil.copyfile(path, dest)
            for suffix in ("-wal", "-shm"):
                side = path.with_name(path.name + suffix)
                if side.is_file():
                    shutil.copyfile(side, dest.with_name(dest.name + suffix))
        except PermissionError:
            if family == "safari":
                raise ReadError(FULL_DISK_ACCESS_ERROR) from None
            raise ReadError("no permission to read its history file") from None
        except FileNotFoundError:
            raise ReadError("its history file is missing") from None
        except OSError as exc:
            raise ReadError(f"could not copy its history file ({type(exc).__name__})") from None
        try:
            conn = sqlite3.connect(f"{dest.as_uri()}?mode=ro", uri=True, timeout=2)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            raise ReadError(f"its history file could not be opened ({exc})") from None
        yield conn
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _need(conn: sqlite3.Connection, layout: Mapping[str, set[str]]) -> None:
    for table, cols in layout.items():
        if not cols <= _columns(conn, table):
            raise ReadError("its history file has an unexpected layout")


Row = tuple[str, str, float, int]   # url, title, last visit (unix), visit count


def _read_safari(conn: sqlite3.Connection, cutoff: Optional[float]) -> list[Row]:
    _need(conn, {"history_items": {"id", "url", "visit_count"},
                 "history_visits": {"history_item", "visit_time", "title"}})
    mac_cutoff = -1e18 if cutoff is None else cutoff - MAC_EPOCH
    rows = conn.execute(
        "SELECT i.url, "
        " (SELECT v2.title FROM history_visits v2 WHERE v2.history_item = i.id "
        "   AND v2.title IS NOT NULL AND v2.title != '' ORDER BY v2.visit_time DESC LIMIT 1), "
        " MAX(v.visit_time), i.visit_count "
        "FROM history_items i JOIN history_visits v ON v.history_item = i.id "
        "WHERE v.visit_time >= ? GROUP BY i.id", (mac_cutoff,)).fetchall()
    return [(u, t or "", float(last) + MAC_EPOCH, int(n or 0)) for u, t, last, n in rows
            if last is not None]


def _read_chromium(conn: sqlite3.Connection, cutoff: Optional[float]) -> list[Row]:
    _need(conn, {"urls": {"url", "title", "visit_count", "last_visit_time", "hidden"}})
    wk_cutoff = 1 if cutoff is None else int((cutoff + WEBKIT_EPOCH_OFFSET) * 1_000_000)
    rows = conn.execute(
        "SELECT url, title, last_visit_time, visit_count FROM urls "
        "WHERE last_visit_time >= ? AND last_visit_time > 0 AND hidden = 0",
        (wk_cutoff,)).fetchall()
    return [(u, t or "", last / 1_000_000 - WEBKIT_EPOCH_OFFSET, int(n or 0))
            for u, t, last, n in rows]


def _read_firefox(conn: sqlite3.Connection, cutoff: Optional[float]) -> list[Row]:
    _need(conn, {"moz_places": {"url", "title", "visit_count", "last_visit_date", "hidden"}})
    us_cutoff = 1 if cutoff is None else int(cutoff * 1_000_000)
    rows = conn.execute(
        "SELECT url, title, last_visit_date, visit_count FROM moz_places "
        "WHERE last_visit_date IS NOT NULL AND last_visit_date >= ? AND hidden = 0",
        (us_cutoff,)).fetchall()
    return [(u, t or "", last / 1_000_000, int(n or 0)) for u, t, last, n in rows]


_READERS: dict[str, Callable[[sqlite3.Connection, Optional[float]], list[Row]]] = {
    "safari": _read_safari, "chromium": _read_chromium, "firefox": _read_firefox,
}


def read_profile(profile: Profile, cutoff: Optional[float]) -> list[Row]:
    """Rows of one profile visited at/after ``cutoff`` (unix; None = all)."""
    with _copied(profile.path, profile.family) as conn:
        try:
            return _READERS[profile.family](conn, cutoff)
        except sqlite3.Error as exc:
            raise ReadError(f"its history file could not be read ({exc})") from None


# ---------------------------------------------------------------------------
# Page text
# ---------------------------------------------------------------------------

Fetch = Callable[[str], Mapping[str, Any]]


def fetch_public_page(url: str) -> dict[str, Any]:
    """#124's fetch path without its web-search switch (page text is its own
    opt-in): ``check_url`` on every hop (http/https, no credentials, every
    resolved address public, connection pinned to it), at most
    ``MAX_REDIRECTS``, no cookies, ``MAX_BODY_BYTES`` / ``TIMEOUT_S``, text
    extracted. Each request is one privacy-ledger row through
    ``web_search._get`` -> ``_record`` (tool ``knowledge_browser``)."""
    ws = importlib.import_module("tools.web_search")
    current = url
    for hop in range(ws.MAX_REDIRECTS + 1):
        address, refusal = ws.check_url(current)
        if address is None:
            return {"status": "error", "error": ("redirect refused: " if hop else "") + refusal}
        try:
            reply = ws._get(FETCH_TOOL_NAME, current, text=current, connect_ip=address)
        except Exception as exc:
            return {"status": "error", "error": f"could not fetch the page: {type(exc).__name__}"}
        if reply.status in ws._REDIRECTS:
            target = str(reply.headers.get("location") or "").strip()
            if not target:
                return {"status": "error", "error": "the page redirected nowhere"}
            current = urljoin(current, target)
            continue
        if reply.status != 200:
            return {"status": "error", "error": f"the page answered HTTP {reply.status}"}
        content_type = str(reply.headers.get("content-type") or "text/html").lower()
        kind = content_type.split(";", 1)[0].strip()
        if kind not in ws._TEXT_TYPES:
            return {"status": "error", "error": "not a text page"}
        raw = ws._decode(reply.body, content_type)
        title, text = ("", ws._tidy(raw)) if kind == "text/plain" else ws.extract_text(raw)
        if not text:
            return {"status": "error", "error": "the page has no readable text"}
        return {"status": "ok", "url": current, "title": title, "text": text[:MAX_PAGE_CHARS]}
    return {"status": "error", "error": "too many redirects"}


class _FetchPacer:
    def __init__(self, fetch: Fetch, sleep: Callable[[float], None]) -> None:
        self.fetch = fetch
        self.sleep = sleep
        self.last: Optional[float] = None

    def __call__(self, url: str) -> Optional[Mapping[str, Any]]:
        if self.last is not None:
            wait = FETCH_GAP_SECONDS - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
        try:
            got = self.fetch(url)
        except Exception:
            got = None
        finally:
            self.last = time.monotonic()
        if isinstance(got, Mapping) and got.get("status") == "ok" and got.get("text"):
            return got
        return None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    url: str
    title: str
    last: float
    count: int
    browsers: set
    sanitised: bool


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _meta_text(e: _Entry) -> str:
    browsers = ", ".join(sorted(BROWSER_TITLES[b] for b in e.browsers))
    visits = "1 visit" if e.count == 1 else f"{e.count} visits"
    return f"{e.title}\n{e.url}\nLast visited {_iso(e.last)[:10]} in {browsers}; {visits}."


def _make_item(e: _Entry, page_text: bool, now: float, pacer: Optional[_FetchPacer]) -> SourceItem:
    meta = _meta_text(e)
    title = e.title or _host(e.url)
    fetchable = page_text and pacer is not None and not e.sanitised \
        and not _never_fetch_host(_host(e.url))
    if not fetchable:
        version = "meta:" + hashlib.sha256(meta.encode("utf-8")).hexdigest()[:20]
        return SourceItem(item_id=item_id_for(e.url), version=version, title=title,
                          link=e.url, date=_iso(e.last), text=meta)

    def load(url: str = e.url, meta: str = meta) -> dict[str, str]:
        got = pacer(url)
        if got is None:  # not fatal: title + URL now, retried next window
            return {"text": meta}
        out = {"text": f"{meta}\n\n{str(got.get('text') or '')[:MAX_PAGE_CHARS]}"}
        if not e.title and got.get("title"):
            out["title"] = str(got["title"])[:300]
        return out

    return SourceItem(item_id=item_id_for(e.url), version=f"page:{fetch_window(e.url, now)}",
                      title=title, link=e.url, date=_iso(e.last), load=load)


_OVERRIDES = frozenset({"home", "roots", "clock", "fetch", "sleep"})


def _check_overrides(overrides: Mapping[str, Any]) -> None:
    unknown = set(overrides) - _OVERRIDES
    if unknown:
        raise TypeError(f"unknown override(s): {sorted(unknown)}")


def listing(options: Optional[Mapping[str, Any]] = None, **overrides: Any) -> Listing:
    """Every visited http(s) URL inside the look-back limit, newest first.

    Overrides (tests): ``home`` (Path; default ``Path.home()``), ``roots``
    (``{browser: Path}`` replacing one browser's folder), ``clock`` (unix
    seconds), ``fetch`` (``url -> {"status", "text", "title"}``; default
    :func:`fetch_public_page`), ``sleep`` (between fetches)."""
    _check_overrides(overrides)
    try:
        opts = validate_options(options)
    except ValueError as exc:
        return Listing([], complete=False, error=str(exc))
    clock: Callable[[], float] = overrides.get("clock") or time.time
    now = float(clock())
    days = opts["lookback_days"]
    cutoff = None if days == 0 else now - days * 86400.0
    home = Path(overrides.get("home") or Path.home())
    try:
        profiles = find_profiles(home, overrides.get("roots"))
    except TypeError:
        raise
    except Exception as exc:
        return Listing([], complete=False, error=f"could not look for browsers ({type(exc).__name__})")

    errors: list[str] = []
    selected = profiles
    if opts["profiles"] is not None:
        wanted = opts["profiles"]
        selected = [p for p in profiles if p.key in wanted]
        have = {p.key for p in profiles}
        for key in wanted:
            if key not in have:
                browser, _, folder = key.partition(":")
                errors.append(f"{BROWSER_TITLES[browser]} ({folder}): profile not found")

    merged: dict[str, _Entry] = {}
    excluded = opts["excluded_domains"]
    for profile in selected:
        try:
            rows = read_profile(profile, cutoff)
        except ReadError as exc:
            errors.append(f"{profile.label}: {exc}")
            continue
        except Exception as exc:  # one browser never hides the others
            errors.append(f"{profile.label}: could not read history ({type(exc).__name__})")
            continue
        for raw_url, title, last, count in rows:
            if cutoff is not None and last < cutoff:
                continue
            url, sanitised = clean_url(raw_url)
            if url is None or is_excluded(url, excluded):
                continue
            title = re.sub(r"\s+", " ", str(title or "")).strip()[:300]
            e = merged.get(url)
            if e is None:
                merged[url] = _Entry(url, title, last, max(count, 0), {profile.browser}, sanitised)
                continue
            if last > e.last:
                e.last = last
                e.title = title or e.title
            elif not e.title:
                e.title = title
            e.count += max(count, 0)
            e.browsers.add(profile.browser)
            e.sanitised = e.sanitised or sanitised

    pacer = None
    if opts["page_text"]:
        pacer = _FetchPacer(overrides.get("fetch") or fetch_public_page,
                            overrides.get("sleep") or time.sleep)
    entries = sorted(merged.values(), key=lambda e: e.last, reverse=True)[:MAX_ITEMS]
    items = [_make_item(e, opts["page_text"], now, pacer) for e in entries]
    return Listing(items, complete=not errors, error="; ".join(errors))


def choices(options: Optional[Mapping[str, Any]] = None, **overrides: Any) -> dict[str, Any]:
    """What Settings shows: the profiles found (``readable`` False with an
    ``error`` when the file can't be opened, e.g. Safari without Full Disk
    Access), profiles chosen but gone, the look-back choices and the
    suggested excluded sites. Reads no history rows."""
    _check_overrides(overrides)
    try:
        opts = validate_options(options)
    except ValueError:
        opts = validate_options(None)
    profiles = find_profiles(Path(overrides.get("home") or Path.home()), overrides.get("roots"))
    rows = []
    for p in profiles:
        readable, error = True, ""
        try:
            with open(p.path, "rb") as fh:
                fh.read(16)
        except OSError:
            readable = False
            error = FULL_DISK_ACCESS_ERROR if p.browser == "safari" else "can't read its history file"
        rows.append({"key": p.key, "browser": p.browser, "browser_title": BROWSER_TITLES[p.browser],
                     "profile": p.profile, "name": p.name, "path": str(p.path),
                     "enabled": opts["profiles"] is None or p.key in opts["profiles"],
                     "readable": readable, "error": error})
    have = {p.key for p in profiles}
    return {
        "profiles": rows,
        "missing_profiles": [k for k in (opts["profiles"] or []) if k not in have],
        "lookback_days": [{"value": d, "label": LOOKBACK_LABELS[d]} for d in LOOKBACK_CHOICES],
        "suggested_excluded_domains": list(SUGGESTED_EXCLUDED_DOMAINS),
    }


__all__ = [
    "DEFAULT_OPTIONS", "DESCRIPTION", "FETCH_TOOL_NAME", "LOOKBACK_CHOICES", "NAME",
    "SUGGESTED_EXCLUDED_DOMAINS", "TAINT_LABEL", "TITLE", "choices", "clean_url",
    "fetch_public_page", "fetch_window", "find_profiles", "is_excluded", "listing",
    "validate_options",
]
