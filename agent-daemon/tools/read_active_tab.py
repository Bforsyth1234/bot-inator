"""Query the currently active browser tab via AppleScript.

Two read-only tools (both belong in ``tools.READ_ONLY_TOOL_NAMES``):

* ``read_active_tab`` -- URL, title and browser of the active tab.
* ``read_active_tab_text`` -- the same plus the page's visible text
  (``document.body.innerText``), read from the user's own tab (todo #58).

Browser order (both tools): the supported browser that was active most
recently comes first, then Google Chrome, then Safari. "Most recently
active" comes from ``lsappinfo visibleProcessList``, which lists apps
front-to-back and needs no permission; when the bot-inator UI itself is in
front, the browser the user was looking at just before is still ahead of
the other one. If ``lsappinfo`` fails, the order is Chrome, Safari.

Safety of ``read_active_tab_text``:

* The JavaScript and the expected URL reach ``osascript`` as argv items
  (``osascript -e <script> -- <js> <url>``), never as script source.
* The URL is checked against the sensitive-site list *before* any
  JavaScript runs, and the JavaScript step only runs if the tab still shows
  that URL (so a tab switch between the two steps cannot read a blocked
  page).
* Only ``http`` / ``https`` pages are read. Everything else -- ``chrome://``,
  ``about:``, extension pages, ``view-source:``, ``data:``, and ``file://``
  -- is refused. ``file://`` is refused because a local file can be
  anything on disk (statements, exports, keys) and is not "a web page the
  user is browsing"; the agent has file tools for files it is asked about.
* Nothing is read while an app on the never-watch list (#87,
  ``ai.delivery.is_never_watched``; password managers by default) is in
  front, or when the browser itself is on the list. URL and title are
  blanked too.
* With a cloud main engine, ``text`` is replaced by the #31 placeholder
  unless ``AGENT_CLOUD_PERSONAL_TEXT=1`` (invariant 7c). URL and title are
  unchanged.

Requires macOS Automation permission for each browser, and the browser
setting "Allow JavaScript from Apple Events" for the text tool.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Optional
from urllib.parse import urlsplit

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        """Fallback no-op decorator when smolagents is unavailable."""
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


READ_ONLY: frozenset[str] = frozenset({"read_active_tab_text"})

CHROME = "Google Chrome"
SAFARI = "Safari"
DEFAULT_ORDER: tuple[str, ...] = (CHROME, SAFARI)
# ``lsappinfo`` shows app names with spaces as underscores.
_LSAPPINFO_NAMES: dict[str, str] = {"Google_Chrome": CHROME, "Safari": SAFARI}

DEFAULT_MAX_CHARS = 8000
MAX_CHARS_CAP = 20_000

SENSITIVE_ERROR = "page is on the sensitive-site list"
NEVER_WATCH_ERROR = "an app on the never-watch list is in front, or is this browser"

# #87: bundle ids, for the never-watch list (``ai.delivery``).
BROWSER_BUNDLE_IDS: dict[str, str] = {CHROME: "com.google.Chrome", SAFARI: "com.apple.Safari"}

# Menu path to the one-time browser setting (#58 decision 3).
JS_SETTING_MENU: dict[str, str] = {
    CHROME: "View > Developer > Allow JavaScript from Apple Events",
    SAFARI: (
        "Develop > Allow JavaScript from Apple Events (if there is no "
        "Develop menu: Safari > Settings > Advanced > Show features for "
        "web developers)"
    ),
}
# Fragments of the error each browser raises when that setting is off.
_JS_OFF_MARKERS: tuple[str, ...] = (
    "javascript from apple events",
    "javascript through applescript is turned off",
)

# ---- sensitive-site list (#58 decision 7) ----------------------------------
#
# Conservative on purpose: a false positive only means the agent cannot read
# that page; a false negative sends a bank or vault page to a model.

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Host equals the entry or ends with "." + entry.
SENSITIVE_HOST_SUFFIXES: tuple[str, ...] = (
    # password managers
    "1password.com", "1password.ca", "1password.eu",
    "bitwarden.com", "bitwarden.eu", "lastpass.com", "dashlane.com",
    "keepersecurity.com", "keepersecurity.eu", "nordpass.com",
    "roboform.com", "pass.proton.me", "account.proton.me",
    # identity providers / account pages
    "accounts.google.com", "myaccount.google.com", "passwords.google.com",
    "okta.com", "oktapreview.com", "okta-emea.com", "auth0.com",
    "onelogin.com", "duosecurity.com", "pingone.com", "login.gov", "id.me",
    "login.microsoftonline.com", "login.live.com", "account.live.com",
    "account.microsoft.com", "appleid.apple.com", "account.apple.com",
    "idmsa.apple.com", "iforgot.apple.com", "github.com/settings",
    # banks, cards, brokers, payments without "bank" in the host
    "chase.com", "wellsfargo.com", "citi.com", "capitalone.com",
    "americanexpress.com", "discover.com", "ally.com", "pnc.com",
    "truist.com", "schwab.com", "fidelity.com", "vanguard.com",
    "etrade.com", "robinhood.com", "merrilledge.com", "sofi.com",
    "chime.com", "paypal.com", "venmo.com", "wise.com", "coinbase.com",
    "kraken.com", "binance.com", "irs.gov", "ssa.gov",
)
# Host contains any of these (e.g. bankofamerica.com, usbank.com).
SENSITIVE_HOST_KEYWORDS: tuple[str, ...] = (
    "bank", "creditunion", "fcu.", "password", "vault",
)


def is_sensitive_url(url: str) -> bool:
    """True when ``url`` must never be read (non-web scheme or listed host)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return True
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return True
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return True
    path = parts.path.lower()
    for entry in SENSITIVE_HOST_SUFFIXES:
        entry_host, _, entry_path = entry.partition("/")
        if host == entry_host or host.endswith("." + entry_host):
            if not entry_path or path.lstrip("/").startswith(entry_path):
                return True
    return any(word in host for word in SENSITIVE_HOST_KEYWORDS)


# ---- AppleScript -----------------------------------------------------------

_CHROME_SCRIPT = '''
if application "Google Chrome" is running then
    tell application "Google Chrome"
        if (count windows) > 0 then
            set theTab to active tab of front window
            return (URL of theTab) & linefeed & (title of theTab)
        end if
    end tell
end if
return ""
'''

_SAFARI_SCRIPT = '''
if application "Safari" is running then
    tell application "Safari"
        if (count windows) > 0 then
            set theTab to current tab of front window
            return (URL of theTab) & linefeed & (name of theTab)
        end if
    end tell
end if
return ""
'''

# argv: 1 = JavaScript to run, 2 = URL the tab must still show.
_CHROME_JS_SCRIPT = '''
on run argv
    set js to item 1 of argv
    set expectedURL to item 2 of argv
    tell application "Google Chrome"
        if (count windows) is 0 then return "__BOTINATOR_CHANGED__"
        set theTab to active tab of front window
        if (URL of theTab) is not expectedURL then return "__BOTINATOR_CHANGED__"
        set pageText to execute theTab javascript js
    end tell
    if pageText is missing value then return ""
    return pageText as text
end run
'''

_SAFARI_JS_SCRIPT = '''
on run argv
    set js to item 1 of argv
    set expectedURL to item 2 of argv
    tell application "Safari"
        if (count windows) is 0 then return "__BOTINATOR_CHANGED__"
        set theTab to current tab of front window
        if (URL of theTab) is not expectedURL then return "__BOTINATOR_CHANGED__"
        set pageText to do JavaScript js in theTab
    end tell
    if pageText is missing value then return ""
    return pageText as text
end run
'''

_CHANGED = "__BOTINATOR_CHANGED__"
PAGE_TEXT_JS = "(document.body && document.body.innerText) || ''"

_META_SCRIPTS: dict[str, str] = {CHROME: _CHROME_SCRIPT, SAFARI: _SAFARI_SCRIPT}
_JS_SCRIPTS: dict[str, str] = {CHROME: _CHROME_JS_SCRIPT, SAFARI: _SAFARI_JS_SCRIPT}

_META_TIMEOUT_S = 5
# Big pages take a moment; the first-use Automation prompt blocks too.
_JS_TIMEOUT_S = 20


def _osascript(script: str, *args: str, timeout: float = _META_TIMEOUT_S
               ) -> tuple[bool, str, str]:
    """Run ``script`` with ``args`` as argv. Returns (ok, stdout, stderr)."""
    argv = ["osascript", "-e", script]
    if args:
        argv += ["--", *args]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timed out waiting for the browser"
    except Exception as exc:  # noqa: BLE001 - osascript missing, etc.
        return False, "", str(exc)
    if result.returncode != 0:
        return False, result.stdout or "", result.stderr or ""
    return True, result.stdout or "", ""


def _run_osascript(script: str) -> str:
    ok, out, _ = _osascript(script)
    return out.strip() if ok else ""


def browser_order() -> list[str]:
    """Supported browsers, most recently active first, then Chrome, Safari."""
    try:
        out = subprocess.run(
            ["lsappinfo", "visibleProcessList"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    order: list[str] = []
    # Entries look like: ASN:0x0-0x3e63e6-"Google_Chrome":
    for name in re.findall(r'"([^"]+)"', out or ""):
        browser = _LSAPPINFO_NAMES.get(name)
        if browser and browser not in order:
            order.append(browser)
    order += [b for b in DEFAULT_ORDER if b not in order]
    return order


def _query(browser: str, script: str) -> Optional[dict[str, Any]]:
    output = _run_osascript(script)
    if not output:
        return None
    parts = output.split("\n", 1)
    if len(parts) < 2:
        return None
    url, title = parts[0].strip(), parts[1].strip()
    if not url:
        return None
    return {"url": url, "title": title, "browser": browser}


def _find_active_tab() -> Optional[dict[str, Any]]:
    for browser in browser_order():
        found = _query(browser, _META_SCRIPTS[browser])
        if found is not None:
            return found
    return None


_NO_BROWSER = "no supported browser with an open window"


@tool
def read_active_tab() -> dict[str, Any]:
    """Return metadata about the frontmost browser's active tab.

    Asks the browser the user was looking at most recently first, then
    Google Chrome, then Safari. Returns an empty payload with an ``error``
    field when no supported browser has an open window.

    Returns:
        A dict with the active tab's ``url``, ``title`` and ``browser``,
        or ``{"url": "", "title": "", "browser": "", "error": "..."}``.
    """
    found = _find_active_tab()
    if found is not None:
        return found
    return {"url": "", "title": "", "browser": "", "error": _NO_BROWSER}


# ---- never-watch apps (#87) -------------------------------------------------

def _front_app() -> tuple[Optional[str], Optional[str]]:
    """(bundle id, name) of the frontmost app via ``lsappinfo``; Nones on failure."""
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=2.0,
        ).stdout.strip()
        if not asn.startswith("ASN:"):
            return None, None
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "bundleid", "-only", "name", asn],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    values = dict(re.findall(r'"([^"]+)"="([^"]*)"', out or ""))
    return values.get("CFBundleIdentifier") or None, values.get("LSDisplayName") or None


def never_watched(browser: str) -> bool:
    """True when the browser or the frontmost app is on the never-watch list."""
    from ai.delivery import is_never_watched  # lazy: ai pulls in the AI stack

    if is_never_watched(BROWSER_BUNDLE_IDS.get(browser), browser):
        return True
    bundle, name = _front_app()
    return is_never_watched(bundle, name)


# ---- page text -------------------------------------------------------------

_HSPACE_RE = re.compile(r"[^\S\n]+")
_NEWLINES_RE = re.compile(r"\s*\n\s*")


def collapse_whitespace(text: str) -> str:
    """Runs of spaces/tabs -> one space; line breaks (and blank lines) -> one."""
    text = _HSPACE_RE.sub(" ", text.replace("\r", "\n"))
    return _NEWLINES_RE.sub("\n", text).strip()


def _clamp_max_chars(max_chars: Any) -> int:
    try:
        value = int(max_chars)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(1, min(value, MAX_CHARS_CAP))


def _js_error(browser: str, stderr: str) -> str:
    lowered = (stderr or "").lower()
    if any(marker in lowered for marker in _JS_OFF_MARKERS):
        return (
            f"{browser} does not allow JavaScript from Apple Events. Turn it "
            f"on once in {browser}: {JS_SETTING_MENU[browser]}."
        )
    detail = (stderr or "").strip().splitlines()
    return f"could not read the page in {browser}: " + (
        detail[-1] if detail else "unknown error"
    )


def _redact(text: str) -> tuple[str, bool]:
    from ai import privacy

    redacted = privacy.redact_text(text, "main")
    return redacted, redacted != text


@tool
def read_active_tab_text(max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    """Return the visible text of the active browser tab, with URL and title.

    Reads the tab the user is looking at (most recently active browser
    first, then Google Chrome, then Safari). Never reads banks, password
    managers, sign-in pages, or non-web pages (``chrome://``, ``about:``,
    ``file://``). Whitespace is collapsed and the text is cut to
    ``max_chars`` (at most 20,000).

    Args:
        max_chars: Maximum number of characters of page text to return
            (default 8000, capped at 20000).

    Returns:
        ``{"url", "title", "browser", "text", "truncated"}``, plus an
        ``error`` string when the page could not or must not be read (for
        example when the browser's "Allow JavaScript from Apple Events"
        setting is off; the error names the menu item to turn it on).
    """
    limit = _clamp_max_chars(max_chars)
    tab = _find_active_tab()
    if tab is None:
        return {"url": "", "title": "", "browser": "", "text": "",
                "truncated": False, "error": _NO_BROWSER}
    result: dict[str, Any] = {**tab, "text": "", "truncated": False}
    if is_sensitive_url(tab["url"]):
        result["error"] = SENSITIVE_ERROR
        return result
    browser = tab["browser"]
    if never_watched(browser):
        result.update(url="", title="", error=NEVER_WATCH_ERROR)
        return result
    ok, out, err = _osascript(
        _JS_SCRIPTS[browser], PAGE_TEXT_JS, tab["url"], timeout=_JS_TIMEOUT_S,
    )
    if not ok:
        result["error"] = _js_error(browser, err)
        return result
    raw = out[:-1] if out.endswith("\n") else out
    if raw == _CHANGED:
        result["error"] = "the tab changed while reading; ask again"
        return result
    text = collapse_whitespace(raw)
    result["truncated"] = len(text) > limit
    text = text[:limit]
    text, redacted = _redact(text)
    result["text"] = text
    if redacted:
        result["note"] = (
            "Page text was kept on this Mac because the main engine runs in "
            "the cloud (the user can allow sending it in Settings, or with "
            "AGENT_CLOUD_PERSONAL_TEXT=1)."
        )
    return result
