"""Tests for ``read_active_tab_text`` and the browser order (todo #58).

``subprocess.run`` is always mocked: no AppleScript reaches a real browser.
"""

from __future__ import annotations

import importlib
import subprocess
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import

# Registration in tools/__init__.py shadows same-named modules.
rat = importlib.import_module("tools.read_active_tab")

CHROME_OFF = (
    "execution error: Google Chrome got an error: Executing JavaScript "
    "through AppleScript is turned off. To turn it on, from the menu bar, go "
    "to View > Developer > Allow JavaScript from Apple Events. (12)"
)
SAFARI_OFF = (
    "execution error: Safari got an error: You must enable 'Allow JavaScript "
    "from Apple Events' in the Developer section of Safari Settings to use "
    "'do JavaScript'. (8)"
)


class FakeMac:
    """Stands in for ``subprocess.run``; records every argv."""

    def __init__(self, *, visible: str = "", tabs: Optional[dict] = None,
                 page: str = "Hello world", js_error: Optional[dict] = None,
                 js_out: Optional[str] = None) -> None:
        self.visible = visible
        self.tabs = tabs or {}
        self.page = page
        self.js_error = js_error or {}
        self.js_out = js_out
        self.calls: list[list[str]] = []

    def js_calls(self) -> list[list[str]]:
        return [c for c in self.calls
                if c[0] == "osascript" and "--" in c]

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(list(argv))
        if argv[0] == "lsappinfo":
            return SimpleNamespace(returncode=0, stdout=self.visible, stderr="")
        assert argv[:2] == ["osascript", "-e"]
        script = argv[2]
        for browser, meta in rat._META_SCRIPTS.items():
            if script == meta:
                tab = self.tabs.get(browser)
                out = f"{tab[0]}\n{tab[1]}\n" if tab else "\n"
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
        for browser, js_script in rat._JS_SCRIPTS.items():
            if script == js_script:
                if browser in self.js_error:
                    return SimpleNamespace(returncode=1, stdout="",
                                           stderr=self.js_error[browser])
                out = self.page if self.js_out is None else self.js_out
                return SimpleNamespace(returncode=0, stdout=out + "\n",
                                       stderr="")
        raise AssertionError(f"unexpected script: {script!r}")


@pytest.fixture
def mac(monkeypatch: pytest.MonkeyPatch):
    def install(**kwargs: Any) -> FakeMac:
        fake = FakeMac(**kwargs)
        monkeypatch.setattr(rat.subprocess, "run", fake)
        return fake
    return install


@pytest.fixture(autouse=True)
def local_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: False)
    monkeypatch.delenv("AGENT_CLOUD_PERSONAL_TEXT", raising=False)


BOTH = {
    rat.CHROME: ("https://news.example.com/a", "Chrome page"),
    rat.SAFARI: ("https://blog.example.org/b", "Safari page"),
}
SAFARI_FRONT = ('ASN:0x0-0x1-"Claude": ASN:0x0-0x2-"Safari": '
                'ASN:0x0-0x3-"Google_Chrome": ')
CHROME_FRONT = 'ASN:0x0-0x3-"Google_Chrome": ASN:0x0-0x2-"Safari": '


# ---- the happy path, argv-only JavaScript ----------------------------------

def test_chrome_text_returned_with_shape(mac) -> None:
    fake = mac(visible=CHROME_FRONT, tabs=BOTH, page="Top story\n\nBody")
    out = rat.read_active_tab_text()
    assert out == {"url": "https://news.example.com/a", "title": "Chrome page",
                   "browser": rat.CHROME, "text": "Top story\nBody",
                   "truncated": False}
    (call,) = fake.js_calls()
    assert call[2] == rat._CHROME_JS_SCRIPT


def test_javascript_is_an_argv_item_not_script_source(mac) -> None:
    fake = mac(visible=CHROME_FRONT, tabs=BOTH)
    rat.read_active_tab_text()
    (call,) = fake.js_calls()
    script, dashdash, js, url = call[2], call[3], call[4], call[5]
    assert dashdash == "--"
    assert js == rat.PAGE_TEXT_JS and url == "https://news.example.com/a"
    # Non-vacuity: the script is the fixed constant and holds neither the
    # JavaScript nor the URL; an interpolating implementation fails here.
    assert script == rat._CHROME_JS_SCRIPT
    assert rat.PAGE_TEXT_JS not in script and "innerText" not in script
    assert url not in script
    assert "on run argv" in script


def test_url_with_quotes_stays_data(mac) -> None:
    url = 'https://evil.example.com/"end tell'
    fake = mac(tabs={rat.CHROME: (url, "x")})
    rat.read_active_tab_text()
    (call,) = fake.js_calls()
    assert call[5] == url and call[2] == rat._CHROME_JS_SCRIPT


# ---- frontmost browser first ------------------------------------------------

def test_safari_used_when_it_is_most_recently_active(mac) -> None:
    fake = mac(visible=SAFARI_FRONT, tabs=BOTH, page="safari text")
    out = rat.read_active_tab_text()
    assert out["browser"] == rat.SAFARI and out["text"] == "safari text"
    (call,) = fake.js_calls()
    assert call[2] == rat._SAFARI_JS_SCRIPT


def test_browser_order_parses_lsappinfo(mac) -> None:
    mac(visible=SAFARI_FRONT)
    assert rat.browser_order() == [rat.SAFARI, rat.CHROME]
    mac(visible=CHROME_FRONT)
    assert rat.browser_order() == [rat.CHROME, rat.SAFARI]


def test_browser_order_defaults_without_lsappinfo(monkeypatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no lsappinfo")
    monkeypatch.setattr(rat.subprocess, "run", boom)
    assert rat.browser_order() == [rat.CHROME, rat.SAFARI]


def test_falls_back_when_front_browser_has_no_window(mac) -> None:
    mac(visible=SAFARI_FRONT, tabs={rat.CHROME: BOTH[rat.CHROME]})
    assert rat.read_active_tab_text()["browser"] == rat.CHROME


def test_read_active_tab_follows_order_and_keeps_shape(mac) -> None:
    mac(visible=SAFARI_FRONT, tabs=BOTH)
    assert rat.read_active_tab() == {"url": "https://blog.example.org/b",
                                     "title": "Safari page",
                                     "browser": rat.SAFARI}
    mac(visible=CHROME_FRONT, tabs=BOTH)
    assert rat.read_active_tab()["browser"] == rat.CHROME
    mac(visible="", tabs={})
    assert rat.read_active_tab() == {
        "url": "", "title": "", "browser": "",
        "error": "no supported browser with an open window"}


def test_read_active_tab_never_runs_javascript(mac) -> None:
    fake = mac(visible=CHROME_FRONT, tabs=BOTH)
    rat.read_active_tab()
    assert fake.js_calls() == []


# ---- "Allow JavaScript from Apple Events" off -------------------------------

@pytest.mark.parametrize("browser,visible,stderr,menu", [
    (rat.CHROME, CHROME_FRONT, CHROME_OFF,
     "View > Developer > Allow JavaScript from Apple Events"),
    (rat.SAFARI, SAFARI_FRONT, SAFARI_OFF,
     "Develop > Allow JavaScript from Apple Events"),
])
def test_js_setting_off_names_menu_item(mac, browser, visible, stderr, menu
                                        ) -> None:
    mac(visible=visible, tabs=BOTH, js_error={browser: stderr})
    out = rat.read_active_tab_text()
    assert out["browser"] == browser and out["text"] == ""
    assert menu in out["error"] and browser in out["error"]


def test_other_js_error_is_reported_plainly(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH,
        js_error={rat.CHROME: "execution error: something else (-1)"})
    out = rat.read_active_tab_text()
    assert "Apple Events" not in out["error"]
    assert "something else" in out["error"]


def test_tab_changed_between_steps(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH, js_out=rat._CHANGED)
    out = rat.read_active_tab_text()
    assert out["text"] == "" and "changed" in out["error"]


# ---- truncation, cap, whitespace --------------------------------------------

def test_truncates_to_max_chars(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH, page="x" * 500)
    out = rat.read_active_tab_text(max_chars=100)
    assert out["text"] == "x" * 100 and out["truncated"] is True
    out = rat.read_active_tab_text(max_chars=500)
    assert len(out["text"]) == 500 and out["truncated"] is False


def test_default_and_cap(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH, page="y" * 30_000)
    assert len(rat.read_active_tab_text()["text"]) == 8000
    out = rat.read_active_tab_text(max_chars=1_000_000)
    assert len(out["text"]) == 20_000 and out["truncated"] is True
    assert len(rat.read_active_tab_text(max_chars=0)["text"]) == 1


def test_whitespace_collapsed(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH,
        page="  Title \t\t here\r\n\n\n   para   two  \n\n")
    assert rat.read_active_tab_text()["text"] == "Title here\npara two"


def test_truncation_counts_collapsed_text(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH, page="a" + " " * 50 + "b")
    out = rat.read_active_tab_text(max_chars=3)
    assert out["text"] == "a b" and out["truncated"] is False


# ---- sensitive sites ---------------------------------------------------------

BLOCKED = [
    "https://my.1password.com/vaults",
    "https://vault.bitwarden.com/#/vault",
    "https://accounts.google.com/signin",
    "https://acme.okta.com/app",
    "https://www.bankofamerica.com/",
    "https://secure.chase.com/web/auth",
    "https://onlinebanking.usbank.com/",
    "https://www.paypal.com/myaccount",
    "https://github.com/settings/tokens",
    "chrome://settings/passwords",
    "about:blank",
    "file:///Users/me/statement.pdf",
    "chrome-extension://abc/popup.html",
    "view-source:https://example.com/",
    "",
]
ALLOWED = [
    "https://news.example.com/a",
    "https://www.google.com/search?q=bank",   # "bank" only in the query
    "https://github.com/anthropics",
    "http://localhost:3000/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls(url: str) -> None:
    assert rat.is_sensitive_url(url)


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url: str) -> None:
    assert not rat.is_sensitive_url(url)


@pytest.mark.parametrize("url", ["https://vault.bitwarden.com/#/vault",
                                 "https://www.wellsfargo.com/",
                                 "chrome://settings", "about:blank",
                                 "file:///etc/hosts"])
def test_blocklist_refuses_without_running_js(mac, url: str) -> None:
    fake = mac(visible=CHROME_FRONT, tabs={rat.CHROME: (url, "Secret")})
    out = rat.read_active_tab_text()
    assert out["error"] == rat.SENSITIVE_ERROR and out["text"] == ""
    assert out["url"] == url
    assert fake.js_calls() == []
    # Non-vacuity: a normal page in the same setup does run JavaScript, so
    # the empty call list above comes from the blocklist, not the fake.
    fake = mac(visible=CHROME_FRONT, tabs=BOTH)
    rat.read_active_tab_text()
    assert len(fake.js_calls()) == 1


def test_blocklist_flip_runs_js(mac, monkeypatch) -> None:
    """Non-vacuity: with the check disabled the blocked page would be read."""
    monkeypatch.setattr(rat, "is_sensitive_url", lambda url: False)
    fake = mac(tabs={rat.CHROME: ("https://vault.bitwarden.com/", "V")})
    rat.read_active_tab_text()
    assert len(fake.js_calls()) == 1


# ---- privacy placeholder (#31) ----------------------------------------------

def test_cloud_main_without_opt_in_gets_placeholder(mac, monkeypatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    mac(visible=CHROME_FRONT, tabs=BOTH, page="private page body")
    out = rat.read_active_tab_text()
    assert out["text"] == privacy.PLACEHOLDER
    assert out["url"] == "https://news.example.com/a"
    assert out["title"] == "Chrome page"
    assert "note" in out


def test_cloud_main_with_opt_in_gets_text(mac, monkeypatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: role == "main")
    monkeypatch.setenv("AGENT_CLOUD_PERSONAL_TEXT", "1")
    mac(visible=CHROME_FRONT, tabs=BOTH, page="private page body")
    out = rat.read_active_tab_text()
    assert out["text"] == "private page body" and "note" not in out


def test_local_main_gets_text(mac) -> None:
    mac(visible=CHROME_FRONT, tabs=BOTH, page="private page body")
    out = rat.read_active_tab_text()
    assert out["text"] == "private page body" and "note" not in out


# ---- gate ----------------------------------------------------------------

def test_read_only_export() -> None:
    assert rat.READ_ONLY == frozenset({"read_active_tab_text"})


def test_timeout_is_reported(monkeypatch) -> None:
    def fake(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "lsappinfo":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[2] == rat._CHROME_SCRIPT:
            return SimpleNamespace(returncode=0, stdout="https://a.example/\nA",
                                   stderr="")
        if argv[2] == rat._CHROME_JS_SCRIPT:
            raise subprocess.TimeoutExpired(argv, 20)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(rat.subprocess, "run", fake)
    out = rat.read_active_tab_text()
    assert "timed out" in out["error"]
