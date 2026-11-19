"""Search the web and read a page, opt-in and in the privacy ledger (todo #124).

Two tools:

* ``web_search(query, limit=5)`` -- title, URL and snippet per result.
* ``fetch_page(url)`` -- the readable text of one public page.

Rules (#124 DECISIONS, binding):

* OFF BY DEFAULT (invariant 7c). ``AGENT_WEB_SEARCH`` ("1"/"0", read live)
  is the one switch. Off: both tools return :data:`OFF_MESSAGE` and send
  nothing, not even a DNS lookup.
* PROVIDERS. One interface, :class:`SearchProvider` (``search(query,
  limit)``). :class:`BraveProvider` is used when ``AGENT_BRAVE_API_KEY`` is
  saved. With no key the no-key path runs: :data:`NO_KEY_PROVIDER`, which
  is ``None`` in this build (no no-key endpoint's terms could be confirmed
  to allow automated queries from an app; see the item's Deviations), so
  the answer is :data:`UNAVAILABLE_MESSAGE`. A no-key provider that fails
  or comes back empty gives that same message, never an empty result.
* THE KEY IS NEVER LOGGED. It goes in one request header and nowhere else:
  not in a log line, a returned error, or a ledger row. Errors name the
  exception type or the HTTP status, never the exception's text.
* ``fetch_page``: http(s) only, no credentials in the URL, no local or
  private host. The name is resolved once, EVERY resolved address must be
  public, and the connection goes to the address that was checked (so a
  second DNS answer cannot move it). At most :data:`MAX_REDIRECTS`
  redirects, followed by hand with the same check on each hop. No cookies,
  no JavaScript, no login. Text only (script / style / nav dropped), capped
  at :data:`MAX_PAGE_CHARS`.
* LEDGER (#82): one ``web_search`` row per outbound request (each redirect
  hop is a request), with the host and bytes out and in. The query or URL
  is kept only while "keep request text" is on (the ledger decides).
* THE CARD IS THE ORCHESTRATOR'S. These functions never ask for approval
  and have no side effect other than the request and its ledger row.
  :func:`needs_card` is the rule the orchestrator applies (decisions 2-3):
  no card only in a turn the user started that has read no outside text.
  Both names are in ``taint.OUTSIDE_TEXT_TOOLS`` (their results mark the
  turn) and ``taint.WRITING_READ_ONLY_TOOLS`` (a marked turn cards them).

HTTP is the standard library's ``http.client``: ``httpx`` is installed but
is not in ``requirements.txt``, and nothing new is added.
"""

from __future__ import annotations

import html as _html
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import quote, urlencode, urljoin, urlsplit

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        """Fallback no-op decorator when smolagents is unavailable."""
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


logger = logging.getLogger(__name__)

# ---- names the runner registers ------------------------------------------------

TOOL_NAMES: frozenset[str] = frozenset({"web_search", "fetch_page"})
# Both belong in ``tools.READ_ONLY_TOOL_NAMES`` (decision 2); the card in a
# marked or background turn comes from :func:`needs_card`.
READ_ONLY: frozenset[str] = TOOL_NAMES

# ---- settings (read live) ------------------------------------------------------

ENABLED_KEY = "AGENT_WEB_SEARCH"          # "1" on, anything else off (default off)
BRAVE_KEY_SETTING = "AGENT_BRAVE_API_KEY"  # secret; stored like the cloud model key

OFF_MESSAGE = "web search is off: turn it on in Settings"
UNAVAILABLE_MESSAGE = "search provider unavailable: add a Brave key in Settings"

PROVIDER_BRAVE = "brave"
PROVIDER_NONE = "none"

# ---- limits --------------------------------------------------------------------

MAX_QUERY_CHARS = 200
DEFAULT_LIMIT = 5
MIN_LIMIT = 1
MAX_LIMIT = 10
MAX_PAGE_CHARS = 20_000
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2_000_000
MAX_URL_CHARS = 2_000
MAX_SNIPPET_CHARS = 500
TIMEOUT_S = 10.0

BRAVE_HOST = "api.search.brave.com"
BRAVE_URL = f"https://{BRAVE_HOST}/res/v1/web/search"
USER_AGENT = "Mozilla/5.0 (compatible; bot-inator)"
_TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def _setting(key: str) -> str:
    """A setting's live value: the environment first (Settings mirrors every
    save into it, and a shell export wins), else the saved file."""
    value = os.environ.get(key, "").strip()
    if value:
        return value
    try:
        import user_settings  # noqa: WPS433 (top-level daemon module)

        return str(user_settings.read().get(key, "") or "").strip()
    except Exception:
        return ""


def is_enabled() -> bool:
    """Live read of the switch (default off)."""
    return _setting(ENABLED_KEY) == "1"


def _brave_key() -> str:
    return _setting(BRAVE_KEY_SETTING)


def provider_in_use() -> str:
    """``"brave"`` when a key is saved, else the no-key provider's name
    (``"none"`` while there is none). For the Settings line."""
    if _brave_key():
        return PROVIDER_BRAVE
    provider = NO_KEY_PROVIDER
    return str(getattr(provider, "name", "") or PROVIDER_NONE) if provider else PROVIDER_NONE


def status() -> dict[str, Any]:
    """What Settings shows. Never holds the key, only whether one is saved."""
    return {
        "enabled": is_enabled(),
        "provider": provider_in_use(),
        "has_brave_key": bool(_brave_key()),
        "no_key_provider": NO_KEY_PROVIDER is not None,
    }


# ---- the card rule (the orchestrator applies it) ---------------------------------


def _user_turn_kinds() -> frozenset[str]:
    try:
        from tools.read_front_window import USER_TURN_KINDS

        return frozenset(USER_TURN_KINDS)
    except Exception:
        return frozenset({"chat"})


# Read kinds (``TurnTaint.read_kinds``) that are these tools' own results:
# public web text, nothing private. Any other kind (an email, the open tab,
# a file, the clipboard, a window, contacts, an MCP result, a marked event)
# means the turn holds text that must not ride out on a query or a URL.
WEB_RESULT_KINDS: frozenset[str] = frozenset({"web_result"})


def needs_card(
    tool_name: str,
    *,
    turn_kind: Any,
    read_kinds: Any = (),
    url: Any = "",
    known_urls: Any = (),
    user_typed: bool = False,
) -> bool:
    """True when ``tool_name`` is one of these tools and this call must go
    through an approval card. Pure; False for any other tool.

    * The turn was not started by the user (``turn_kind`` not in
      ``read_front_window.USER_TURN_KINDS``): card.
    * The turn has read anything other than these tools' own results
      (``read_kinds`` holds a kind outside :data:`WEB_RESULT_KINDS`): card.
      The query or URL could carry that text out.
    * ``web_search`` otherwise: no card. Its query goes to the search
      provider only.
    * ``fetch_page`` in a turn that read nothing: no card.
    * ``fetch_page`` after a search or a fetched page: no card only when the
      URL is one this turn's own ``web_search`` returned (``known_urls``) or
      one the user typed (``user_typed``); any other URL was written by the
      model after reading someone else's text, so it gets a card.
    """
    if tool_name not in TOOL_NAMES:
        return False
    if str(turn_kind or "") not in _user_turn_kinds():
        return True
    kinds = {str(k) for k in (read_kinds or ())}
    if kinds - WEB_RESULT_KINDS:
        return True
    if tool_name == "web_search" or not kinds:
        return False
    target = str(url or "").strip()
    if user_typed or (target and target in {str(u).strip() for u in (known_urls or ())}):
        return False
    return True


def result_urls(result: Any) -> list[str]:
    """The URLs a ``web_search`` result returned (for ``known_urls``)."""
    if not isinstance(result, Mapping):
        return []
    out: list[str] = []
    for item in result.get("results") or []:
        if isinstance(item, Mapping):
            u = str(item.get("url") or "").strip()
            if u:
                out.append(u)
    return out


def outbound_text(tool_name: str, arguments: Optional[Mapping[str, Any]]) -> str:
    """Exactly what would leave the Mac for this call (the card shows it):
    the query after the cap, or the URL."""
    args = arguments or {}
    if tool_name == "web_search":
        return clean_query(args.get("query"))
    if tool_name == "fetch_page":
        return str(args.get("url") or "").strip()
    return ""


# ---- small helpers ---------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]{0,200}>")


def clean_query(query: Any) -> str:
    """One line, at most :data:`MAX_QUERY_CHARS` characters."""
    return _WS_RE.sub(" ", str(query or "")).strip()[:MAX_QUERY_CHARS].strip()


def clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, n))


def _plain(value: Any, cap: int = MAX_SNIPPET_CHARS) -> str:
    """Text without markup (Brave marks matches with ``<strong>``)."""
    text = _html.unescape(_TAG_RE.sub("", str(value or "")))
    return _WS_RE.sub(" ", text).strip()[:cap]


def _scrub(text: str) -> str:
    """Belt and braces: the key never leaves in a returned string."""
    key = _brave_key()
    return text.replace(key, "[key]") if key and key in text else text


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": _scrub(message), **extra}


# ---- transport -------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased names
    body: bytes = b""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to the address that was checked, not to a fresh lookup."""

    def __init__(self, host: str, port: Optional[int], ip: Optional[str], timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip or self.host, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: Optional[int], ip: Optional[str], timeout: float) -> None:
        self._ctx = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._ctx)
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip or self.host, self.port), self.timeout)
        # The certificate is checked against the name, not the address.
        self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)


def _http_get(
    url: str, *, headers: Optional[Mapping[str, str]] = None,
    connect_ip: Optional[str] = None, timeout: float = TIMEOUT_S,
    max_bytes: int = MAX_BODY_BYTES,
) -> HttpResponse:
    """One GET. No redirects, no cookies, no proxy, no decompression.

    Tests replace this function; nothing else in the module opens a socket.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    cls = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
    conn = cls(host, parts.port, connect_ip, timeout)
    try:
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
        if parts.query:
            path += "?" + quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
        sent = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})}
        conn.request("GET", path, headers=sent)
        reply = conn.getresponse()
        body = reply.read(max_bytes + 1)[:max_bytes]
        return HttpResponse(
            int(reply.status), {k.lower(): v for k, v in reply.getheaders()}, body,
        )
    finally:
        conn.close()


def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. Tests replace this function."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


def _record(tool_name: str, url: str, *, text: str, reply: Optional[HttpResponse],
            error: Optional[BaseException] = None) -> None:
    """One ledger row for one request. ``url`` gives the host and the size
    out; headers (where the key is) are never passed in."""
    try:
        from ai import egress_log

        note = None
        if error is not None:
            note = f"failed: {type(error).__name__}"
        elif reply is not None and reply.status >= 400:
            note = f"HTTP {reply.status}"
        egress_log.record_web_search(
            url, tool=tool_name, bytes_out=len(url.encode("utf-8")),
            bytes_in=len(reply.body) if reply is not None else 0,
            text=text, note=note,
        )
    except Exception:  # recording never fails a request
        logger.warning("web_search: ledger row not written")


def _get(tool_name: str, url: str, *, text: str,
         headers: Optional[Mapping[str, str]] = None,
         connect_ip: Optional[str] = None) -> HttpResponse:
    """:func:`_http_get` plus its ledger row. Raises what the transport raises."""
    try:
        reply = _http_get(url, headers=headers, connect_ip=connect_ip)
    except Exception as exc:
        _record(tool_name, url, text=text, reply=None, error=exc)
        raise
    _record(tool_name, url, text=text, reply=reply)
    return reply


# ---- providers -------------------------------------------------------------------


class SearchError(Exception):
    """A provider failure with a message that is safe to show (no key)."""


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        """Up to ``limit`` results, each ``{"title", "url", "snippet"}``.
        Raises :class:`SearchError` on failure."""


class BraveProvider:
    """Brave Search API with the user's own key (USER DECISION A)."""

    name = PROVIDER_BRAVE

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def __repr__(self) -> str:  # the key never reaches a repr
        return "BraveProvider()"

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        url = BRAVE_URL + "?" + urlencode({"q": query, "count": limit})
        headers = {"Accept": "application/json", "X-Subscription-Token": self._api_key}
        try:
            reply = _get("web_search", url, text=query, headers=headers)
        except Exception as exc:
            logger.warning("web_search: Brave request failed (%s)", type(exc).__name__)
            raise SearchError(f"search failed: {type(exc).__name__}") from None
        if reply.status in (401, 403):
            raise SearchError("Brave did not accept the key: check it in Settings")
        if reply.status == 429:
            raise SearchError("Brave's rate limit or monthly credit was reached")
        if reply.status != 200:
            raise SearchError(f"search failed: HTTP {reply.status}")
        try:
            data = json.loads(reply.body.decode("utf-8", "replace"))
            rows = (data.get("web") or {}).get("results") or []
        except Exception:
            raise SearchError("search failed: unreadable reply") from None
        out: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            link = str(row.get("url") or "").strip()
            if not link.startswith(("http://", "https://")):
                continue
            out.append({
                "title": _plain(row.get("title"), 200),
                "url": link[:MAX_URL_CHARS],
                "snippet": _plain(row.get("description")),
            })
            if len(out) >= limit:
                break
        return out


# The no-key provider (USER DECISION B). ``None``: not shipped, because no
# no-key endpoint's terms could be confirmed to allow this use (#124
# Deviations). Anything with ``name`` and ``search(query, limit)`` fits here.
NO_KEY_PROVIDER: Optional[SearchProvider] = None


# ---- web_search ------------------------------------------------------------------


@tool
def web_search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the web and return the top results (title, URL, snippet).

    Only works while web search is turned on in Settings. Use it for
    questions that need current information from the internet.

    Args:
        query: What to search for (at most 200 characters).
        limit: How many results to return, 1 to 10 (default 5).

    Returns:
        A dict with ``status`` ("ok" or "error"), ``query``, ``provider``
        and ``results`` (a list of ``title``, ``url``, ``snippet``).
    """
    if not is_enabled():
        return _error(OFF_MESSAGE)
    q = clean_query(query)
    if not q:
        return _error("query is empty")
    n = clamp_limit(limit)

    key = _brave_key()
    if key:
        try:
            results = BraveProvider(key).search(q, n)
        except SearchError as exc:
            return _error(str(exc), query=q, provider=PROVIDER_BRAVE)
        return {"status": "ok", "query": q, "provider": PROVIDER_BRAVE,
                "results": results[:n]}

    provider = NO_KEY_PROVIDER
    if provider is None:
        return _error(UNAVAILABLE_MESSAGE, query=q, provider=PROVIDER_NONE)
    name = str(getattr(provider, "name", "") or "no-key")
    try:
        results = list(provider.search(q, n) or [])
    except Exception as exc:
        logger.warning("web_search: no-key provider failed (%s)", type(exc).__name__)
        results = []
    if not results:  # failed or empty: say what to do, never an empty result
        return _error(UNAVAILABLE_MESSAGE, query=q, provider=name)
    return {"status": "ok", "query": q, "provider": name, "results": results[:n]}


# ---- fetch_page: which hosts ----------------------------------------------------


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global) and not ip.is_multicast


def check_url(url: str) -> tuple[Optional[str], str]:
    """``(address to connect to, "")`` for a public http(s) URL, else
    ``(None, why not)``. Resolves the name; every address must be public."""
    from ai import egress_log

    if len(url) > MAX_URL_CHARS:
        return None, "url is too long"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None, "url is not valid"
    if parts.scheme not in ("http", "https"):
        return None, "url must start with http:// or https://"
    if not host:
        return None, "url has no host"
    if parts.username is not None or parts.password is not None:
        return None, "a url with a user name or password is not fetched"
    refusal = "local and private addresses are not fetched"
    if egress_log._is_local_host(host):
        return None, refusal
    if egress_log._is_ip(host):
        bare = host.strip("[]")
        return (bare, "") if _is_public_ip(bare) else (None, refusal)
    try:
        addresses = _resolve(host, port or (443 if parts.scheme == "https" else 80))
    except Exception:
        return None, "could not find that host"
    if not addresses:
        return None, "could not find that host"
    if not all(_is_public_ip(a) for a in addresses):
        return None, refusal
    return addresses[0], ""


# ---- fetch_page: readable text ---------------------------------------------------

_SKIP_TAGS = frozenset({
    "script", "style", "nav", "noscript", "template", "svg", "iframe",
    "select", "button", "form",
})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "main", "aside", "blockquote", "pre", "hr", "dd", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "title",
})
_BLANK_RE = re.compile(r"[ \t\r\f\v]+")
_LINES_RE = re.compile(r"\n\s*\n+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip.append(tag)
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in _BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if tag in self._skip:
                while self._skip and self._skip.pop() != tag:
                    pass
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip:
            self.parts.append(data)


def _tidy(text: str) -> str:
    text = _BLANK_RE.sub(" ", text.replace("\xa0", " "))
    lines = [line.strip() for line in text.split("\n")]
    return _LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def extract_text(markup: str) -> tuple[str, str]:
    """``(title, readable text)`` of an HTML page: no script, style or nav."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # a broken page keeps what was read so far
        pass
    title = _WS_RE.sub(" ", "".join(parser.title_parts)).strip()[:200]
    return title, _tidy("".join(parser.parts))


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([\w.:-]+)", content_type, re.I)
    return match.group(1) if match else "utf-8"


def _decode(body: bytes, content_type: str) -> str:
    try:
        return body.decode(_charset(content_type), "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


# ---- fetch_page ------------------------------------------------------------------


@tool
def fetch_page(url: str) -> dict[str, Any]:
    """Fetch a public web page and return its readable text.

    Only works while web search is turned on in Settings. Reads http and
    https pages on the public internet; no login, cookies or JavaScript.

    Args:
        url: The absolute URL of the page (http:// or https://).

    Returns:
        A dict with ``status`` ("ok" or "error"), ``url`` (after redirects),
        ``title``, ``text`` (at most 20,000 characters) and ``truncated``.
    """
    asked = str(url or "").strip()
    if not is_enabled():
        return _error(OFF_MESSAGE, url=asked)
    current = asked
    for hop in range(MAX_REDIRECTS + 1):
        address, refusal = check_url(current)
        if address is None:
            if hop:
                refusal = f"redirect refused: {refusal}"
            return _error(refusal, url=asked)
        try:
            reply = _get("fetch_page", current, text=current, connect_ip=address)
        except Exception as exc:
            logger.warning("fetch_page: request failed (%s)", type(exc).__name__)
            return _error(f"could not fetch the page: {type(exc).__name__}", url=asked)
        if reply.status in _REDIRECTS:
            target = str(reply.headers.get("location") or "").strip()
            if not target:
                return _error("the page redirected nowhere", url=asked)
            current = urljoin(current, target)
            continue
        if reply.status != 200:
            return _error(f"the page answered HTTP {reply.status}", url=asked)
        content_type = str(reply.headers.get("content-type") or "text/html").lower()
        kind = content_type.split(";", 1)[0].strip()
        if kind not in _TEXT_TYPES:
            return _error(f"not a text page ({kind[:60]})", url=asked)
        raw = _decode(reply.body, content_type)
        if kind == "text/plain":
            title, text = "", _tidy(raw)
        else:
            title, text = extract_text(raw)
        if not text:
            return _error("the page has no readable text (it may need JavaScript)",
                          url=asked)
        return {
            "status": "ok",
            "url": current,
            "title": title,
            "text": text[:MAX_PAGE_CHARS],
            "truncated": len(text) > MAX_PAGE_CHARS or len(reply.body) >= MAX_BODY_BYTES,
        }
    return _error(f"too many redirects (more than {MAX_REDIRECTS})", url=asked)


__all__ = [
    "BRAVE_KEY_SETTING", "BraveProvider", "ENABLED_KEY", "MAX_PAGE_CHARS",
    "MAX_QUERY_CHARS", "MAX_REDIRECTS", "NO_KEY_PROVIDER", "OFF_MESSAGE",
    "READ_ONLY", "SearchError", "SearchProvider", "TOOL_NAMES",
    "UNAVAILABLE_MESSAGE", "check_url", "clamp_limit", "clean_query",
    "extract_text", "fetch_page", "is_enabled", "needs_card", "outbound_text",
    "provider_in_use", "status", "web_search",
]
