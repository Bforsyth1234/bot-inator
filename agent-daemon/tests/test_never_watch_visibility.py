"""#111: a never-watch window refuses a whole-screen capture only when some
part of it is actually visible.

Window lists are fixtures (the shape of ``look_at_screen._window_list``);
the real screen is never read or captured.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from events import remote_channel as rc
from tests.test_look_at_screen import model, screen  # noqa: F401  - fixtures

las = importlib.import_module("tools.look_at_screen")
ts = importlib.import_module("tools.take_screenshot")

BUNDLES = {100: "com.botinator.MenuBarUI", 200: "com.apple.mail", 210: "com.apple.MobileSMS",
           300: "com.apple.controlcenter", 600: "com.apple.Passwords", 700: "com.apple.dock",
           800: "com.example.Overlay"}


def _w(wid: int, pid: int, owner: str, x: float, y: float, width: float, height: float,
       layer: int = 0, alpha: float = 1.0, title: str = "") -> dict[str, Any]:
    return {"id": wid, "pid": pid, "owner": owner, "title": title, "layer": layer,
            "alpha": alpha, "x": x, "y": y, "width": width, "height": height}


# The 2026-11-11 window list on the user's Mac, trimmed (front to back).
MENUBAR = _w(1, 0, "Window Server", 0, 0, 1728, 33, layer=24)
DOCK = _w(2, 700, "Dock", 0, 0, 1728, 1117, layer=20)
STATUS = _w(3, 300, "Control Center", 1110, 0, 32, 33, layer=25)
MAIL = _w(10, 200, "Mail", 0, 33, 1728, 1084)
MESSAGES = _w(11, 210, "Messages", 0, 33, 864, 1084)
PASSWORDS = _w(20, 600, "Passwords", 510, 297, 900, 530)


@pytest.fixture()
def bundles(screen, monkeypatch):
    monkeypatch.setattr(las, "_bundle_id_for_pid", lambda pid: BUNDLES.get(pid, ""))
    screen.front = ("com.apple.mail", "Mail")
    return screen


def _screen_ok() -> bool:
    try:
        las.resolve("screen")
    except las.Refused:
        return False
    return True


def _refusal() -> str:
    with pytest.raises(las.Refused) as exc:
        las.resolve("screen")
    return str(exc.value)


# ---- the decision-1 cases ----------------------------------------------------------

def test_fully_covered_never_watch_window_is_allowed(bundles) -> None:
    bundles.windows = [STATUS, MENUBAR, DOCK, MAIL, MESSAGES, PASSWORDS]
    assert las.resolve("screen") == (None, "the whole screen")


def test_covered_by_two_windows_together_is_allowed(bundles) -> None:
    left = _w(12, 210, "Messages", 0, 33, 960, 1084)
    right = _w(13, 200, "Mail", 960, 33, 768, 1084)
    bundles.windows = [MENUBAR, left, right, PASSWORDS]
    assert _screen_ok()


def test_one_pixel_showing_is_refused(bundles) -> None:
    bundles.windows = [MENUBAR, MAIL, PASSWORDS]
    assert _screen_ok()  # baseline: covered
    for sliver in (_w(10, 200, "Mail", 0, 33, 1409, 1084),   # 1 px at the right edge
                   _w(10, 200, "Mail", 511, 33, 1217, 1084),  # 1 px at the left edge
                   _w(10, 200, "Mail", 0, 298, 1728, 819),    # 1 px at the top
                   _w(10, 200, "Mail", 0, 33, 1728, 793)):    # 1 px at the bottom
        bundles.windows = [MENUBAR, sliver, PASSWORDS]
        assert "Passwords" in _refusal() and "never-watch" in _refusal()


def test_a_hole_between_two_covers_is_refused(bundles) -> None:
    left = _w(12, 210, "Messages", 0, 33, 960, 1084)
    right = _w(13, 200, "Mail", 961, 33, 767, 1084)  # a 1 px gap at x = 960
    bundles.windows = [MENUBAR, left, right, PASSWORDS]
    assert "Passwords" in _refusal()


def test_covered_only_by_a_translucent_window_is_refused(bundles) -> None:
    glass = {**MAIL, "alpha": 0.95}
    bundles.windows = [MENUBAR, glass, PASSWORDS]
    assert "Passwords" in _refusal()


def test_the_dock_and_menu_bar_never_cover(bundles) -> None:
    under_menu = _w(20, 600, "Passwords", 100, 0, 900, 33 + 40)
    dock_only = [MENUBAR, DOCK, under_menu]
    bundles.windows = dock_only
    assert "Passwords" in _refusal()


def test_a_window_behind_does_not_cover(bundles) -> None:
    bundles.windows = [MENUBAR, PASSWORDS, MAIL]  # Passwords is in front of Mail
    assert "Passwords" in _refusal()


def test_minimized_never_watch_window_is_allowed(bundles) -> None:
    # Quartz's on-screen list leaves minimized (and other-Space) windows out.
    bundles.windows = [MENUBAR, DOCK, MESSAGES]
    assert _screen_ok()


def test_unreadable_window_list_is_refused(bundles, monkeypatch) -> None:
    monkeypatch.setattr(las, "_window_list", lambda: None)
    assert "could not be read" in _refusal()
    with pytest.raises(las.Refused):
        las.resolve("front_window")


def test_real_window_list_failure_reads_as_unreadable(monkeypatch) -> None:
    import Quartz  # type: ignore

    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", lambda *a: None)
    assert las._window_list() is None

    def boom(*a: Any) -> Any:
        raise RuntimeError("no window server")

    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", boom)
    assert las._window_list() is None


def test_front_app_check_unchanged(bundles) -> None:
    bundles.windows = [MENUBAR, MAIL, MESSAGES, PASSWORDS]
    bundles.front = ("com.apple.Passwords", "Passwords")
    assert "Passwords is on the never-watch list" in _refusal()


def test_front_window_target_skips_a_covered_never_watch_window(bundles) -> None:
    bundles.windows = [MENUBAR, MAIL, PASSWORDS]
    window, _ = las.resolve("front_window")
    assert window is not None and window.app == "Mail"


# ---- the same rule through both tools ----------------------------------------------

def test_look_at_screen_captures_when_passwords_is_covered(bundles, model) -> None:
    bundles.windows = [STATUS, MENUBAR, DOCK, MAIL, MESSAGES, PASSWORDS]
    result = las.look_at_screen_impl("what?", "screen")
    assert result["status"] == "ok"
    assert bundles.captures and "-m" in bundles.captures[0]


def test_look_at_screen_refuses_when_passwords_peeks_out(bundles, model) -> None:
    bundles.windows = [MENUBAR, MESSAGES, {**PASSWORDS, "x": 800}]
    result = las.look_at_screen_impl("what?", "screen")
    assert result["status"] == "error" and "Passwords" in result["error"]
    assert bundles.captures == []


def test_take_screenshot_for_the_phone_follows_the_same_rule(bundles) -> None:
    bundles.windows = [STATUS, MENUBAR, DOCK, MAIL, MESSAGES, PASSWORDS]
    assert ts._sensitive_refusal() == ""
    rc.begin_phone_turn("remote_111")
    try:
        assert "refused" not in ts.card_note()
        bundles.windows = [MENUBAR, MESSAGES, PASSWORDS]
        assert "Passwords" in ts._sensitive_refusal()
        assert "refused" in ts.card_note() and "Passwords" in ts.card_note()
    finally:
        rc.end_phone_turn()


# ---- the geometry ---------------------------------------------------------------------

def test_visible_parts_geometry() -> None:
    a = _w(1, 1, "A", 0, 0, 10, 10)
    b = _w(2, 2, "B", 0, 0, 10, 10)
    assert las.visible_parts([a, b], 1) == []
    assert las.visible_parts([{**a, "x": 5}, b], 1) == [(0.0, 0.0, 5.0, 10.0)]
    centre = _w(3, 3, "C", 3, 3, 4, 4)
    parts = las.visible_parts([centre, b], 1)
    assert sum((p[2] - p[0]) * (p[3] - p[1]) for p in parts) == 100 - 16
    floating = _w(4, 4, "Panel", 0, 0, 10, 10, layer=3)
    assert las.visible_parts([floating, b], 1) == []      # a floating panel covers
    assert las.visible_parts([b, floating], 1) != []      # a normal window can't cover it


# ---- 3. cards hide empty arguments ------------------------------------------------------

def test_phone_card_hides_null_and_empty_arguments() -> None:
    text = rc.card_text("take_screenshot", {"args": [], "kwargs": {"path": None}}, code="a")
    assert "path" not in text and "null" not in text and "None" not in text
    text = rc.card_text("demo", {"args": [None, "x", ""], "kwargs": {
        "a": "", "b": "  ", "c": [], "d": {}, "e": 0, "f": False, "g": "kept"}}, code="b")
    assert "  2: x" in text and "  1:" not in text and "  3:" not in text
    for key in "abcd":
        assert f"  {key}:" not in text
    assert "  e: 0" in text and "  f: false" in text and "  g: kept" in text
