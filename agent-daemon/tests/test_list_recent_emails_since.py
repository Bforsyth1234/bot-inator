"""#144: mail dates are local time with the offset, and list_recent_emails
counts a time range (``since`` / ``until``) instead of the model counting
a capped list of UTC times."""

from __future__ import annotations

import importlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from events import mail_watcher as mw
from tests.test_list_recent_emails import SCHEMA, _add

lre = importlib.import_module("tools.list_recent_emails")
re_mod = importlib.import_module("tools.read_email")

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mail"
NOW = datetime(2026, 11, 20, 14, 0, tzinfo=NY)


def _ts(*parts: int, tz=NY) -> int:
    return int(datetime(*parts, tzinfo=tz).timestamp())


def _root(tmp_path: Path, mails: list[tuple[str, int]]) -> Path:
    root = tmp_path / "Mail"
    path = root / "V10" / "MailData" / "Envelope Index"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for subject, stamp in mails:
        _add(conn, subject, stamp, 1)
    conn.commit()
    conn.close()
    return root


@pytest.fixture()
def noon_root(tmp_path: Path) -> Path:
    return _root(tmp_path, [
        ("late last night", _ts(2026, 11, 19, 23, 30)),   # 04:30 UTC on the 20th
        ("before noon", _ts(2026, 11, 20, 11, 55)),
        ("Fandango", _ts(2026, 11, 20, 12, 16, 17)),
        ("sent copy", _ts(2026, 11, 20, 12, 30)),
    ])


@pytest.fixture()
def system_zone(monkeypatch):
    """Set the process zone through TZ (never the machine's real zone)."""
    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield use
    monkeypatch.undo()
    time.tzset()


def _subjects(result: dict) -> list[str]:
    assert result["status"] == "ok", result
    return [e["subject"] for e in result["emails"]]


def _move_to_sent(root: Path, subject: str) -> None:
    conn = sqlite3.connect(root / "V10" / "MailData" / "Envelope Index")
    conn.execute("UPDATE messages SET mailbox = 2 WHERE subject = "
                 "(SELECT ROWID FROM subjects WHERE subject = ?)", (subject,))
    conn.commit()
    conn.close()


# ---- Verify 1: local offset ---------------------------------------------------


def test_dates_carry_the_local_offset(noon_root) -> None:
    result = lre.list_recent_emails_from(noon_root, 10, tz=NY)
    dates = {e["subject"]: e["date"] for e in result["emails"]}
    assert dates["Fandango"] == "2026-11-20T12:16:17-05:00"
    assert dates["before noon"] == "2026-11-20T11:55:00-05:00"
    assert dates["late last night"] == "2026-11-19T23:30:00-05:00"


def test_dates_follow_the_system_zone_at_call_time(noon_root, system_zone) -> None:
    system_zone("America/New_York")
    first = lre.list_recent_emails_from(noon_root, 1)["emails"][0]["date"]
    system_zone("Europe/London")
    again = lre.list_recent_emails_from(noon_root, 1)["emails"][0]["date"]
    assert (first, again) == ("2026-11-20T12:30:00-05:00", "2026-11-20T17:30:00+00:00")
    assert mw._iso_date(_ts(2026, 11, 20, 12, 16, 17)) == "2026-11-20T17:16:17+00:00"


def test_iso_date_local_and_utc_are_the_same_instant() -> None:
    stamp = _ts(2026, 11, 20, 12, 16, 17)
    local = mw._iso_date(stamp, NY)
    utc = mw._iso_date(stamp, timezone.utc)
    assert local != utc  # different strings...
    assert datetime.fromisoformat(local) == datetime.fromisoformat(utc)  # ...same time
    assert mw._iso_date(None) == mw._iso_date("x") == ""


# ---- Verify 2: since / count / truncation -------------------------------------


def test_since_noon_returns_only_the_later_mail(noon_root) -> None:
    _move_to_sent(noon_root, "sent copy")
    result = lre.list_recent_emails_from(noon_root, 10, since="2026-11-20T12:00", tz=NY)
    assert _subjects(result) == ["Fandango"]
    assert result["count"] == 1 and result["listed"] == 1 and result["truncated"] is False
    assert result["since"] == "2026-11-20T12:00:00-05:00" and result["until"] == ""
    assert "note" not in result


def test_sixty_matches_count_sixty_list_capped_and_marked(tmp_path) -> None:
    base = _ts(2026, 11, 20, 12, 1)
    root = _root(tmp_path, [("morning", _ts(2026, 11, 20, 9, 0))]
                 + [(f"m{i}", base + 60 * i) for i in range(60)])
    result = lre.list_recent_emails_from(root, 500, since="2026-11-20T12:00", tz=NY)
    assert result["count"] == 60
    assert result["listed"] == len(result["emails"]) == lre.MAX_COUNT == 50
    assert result["truncated"] is True
    assert "60 emails match" in result["note"] and "count (60)" in result["note"]
    assert result["emails"][0]["subject"] == "m59"
    # The default list size caps the list, never the count.
    small = lre.list_recent_emails_from(root, since="2026-11-20T12:00", tz=NY)
    assert (small["count"], small["listed"], small["truncated"]) == (60, 10, True)
    # No range: unchanged shape, count is what is listed.
    plain = lre.list_recent_emails_from(root, 500, tz=NY)
    assert plain["count"] == 50 and "truncated" not in plain and "listed" not in plain


def test_range_count_skips_sent_duplicates_and_deleted(tmp_path) -> None:
    root = _root(tmp_path, [])
    conn = sqlite3.connect(root / "V10" / "MailData" / "Envelope Index")
    at = _ts(2026, 11, 20, 12, 30)
    _add(conn, "real", at, 1)
    _add(conn, "label copy", at, 5, global_id=9)
    _add(conn, "label copy", at, 1, global_id=9)
    _add(conn, "mine", at, 2, sender=2)
    _add(conn, "junk", at, 3)
    _add(conn, "gone", at, 1, deleted=1)
    conn.commit()
    conn.close()
    result = lre.list_recent_emails_from(root, 10, since="2026-11-20T12:00", tz=NY)
    assert result["count"] == 2 and sorted(_subjects(result)) == ["label copy", "real"]
    assert lre.list_recent_emails_from(root, 10, "work", since="2026-11-20", tz=NY)["count"] == 1


# ---- since / until forms -------------------------------------------------------


def test_since_as_a_bare_date_is_local_midnight(noon_root) -> None:
    result = lre.list_recent_emails_from(noon_root, 10, since="2026-11-20", tz=NY)
    # 23:30 on the 19th is 04:30 UTC on the 20th: a UTC midnight would let it in.
    assert _subjects(result) == ["sent copy", "Fandango", "before noon"]
    assert result["since"] == "2026-11-20T00:00:00-05:00"
    day_before = lre.list_recent_emails_from(noon_root, 10, since="2026-11-19",
                                             until="2026-11-19", tz=NY)
    assert _subjects(day_before) == ["late last night"]
    assert day_before["until"] == "2026-11-20T00:00:00-05:00"


def test_since_with_an_explicit_offset(noon_root) -> None:
    for since in ("2026-11-20T17:00:00+00:00", "2026-11-20T17:00Z", "2026-11-20T12:00-05:00"):
        result = lre.list_recent_emails_from(noon_root, 10, since=since, tz=NY)
        assert _subjects(result) == ["sent copy", "Fandango"], since
        assert result["since"] == "2026-11-20T12:00:00-05:00"
    # The same text in London's zone is a different instant (no offset = local).
    london = lre.list_recent_emails_from(noon_root, 10, since="2026-11-20T17:00", tz=LONDON)
    assert _subjects(london) == ["sent copy", "Fandango"]
    assert london["emails"][0]["date"] == "2026-11-20T17:30:00+00:00"


def test_until_is_exclusive_and_time_only_means_today(noon_root) -> None:
    result = lre.list_recent_emails_from(noon_root, 10, since="11:00", until="12:16:17",
                                         tz=NY, now=NOW)
    assert _subjects(result) == ["before noon"]
    assert result["since"] == "2026-11-20T11:00:00-05:00"


@pytest.mark.parametrize("since", ["12 pm", "yesterday", "2026-13-01", "25:00", "2026-11-20T"])
def test_a_bad_since_is_a_clear_error(noon_root, since) -> None:
    result = lre.list_recent_emails_from(noon_root, 10, since=since, tz=NY, now=NOW)
    assert result["status"] == "error"
    assert result["error"].startswith(f"invalid since {since!r}")
    assert "2026-11-20T12:00" in result["error"] and "12:00 (today)" in result["error"]


def test_a_bad_or_backwards_until_is_a_clear_error(noon_root) -> None:
    bad = lre.list_recent_emails_from(noon_root, 10, until="soon", tz=NY, now=NOW)
    assert bad["status"] == "error" and bad["error"].startswith("invalid until 'soon'")
    back = lre.list_recent_emails_from(noon_root, 10, since="2026-11-20T13:00",
                                       until="2026-11-20T12:00", tz=NY)
    assert back["status"] == "error" and "must be after" in back["error"]


def test_blank_since_is_no_range(noon_root) -> None:
    result = lre.list_recent_emails_from(noon_root, 10, since="  ", until="", tz=NY)
    assert result["status"] == "ok" and "truncated" not in result


# ---- DST / zones -----------------------------------------------------------------


def test_dst_offsets_come_from_the_zone(tmp_path) -> None:
    root = _root(tmp_path, [
        ("summer", _ts(2026, 7, 1, 12, 16)),
        ("before fall back", _ts(2026, 11, 1, 0, 30)),   # EDT
        ("after fall back", _ts(2026, 11, 1, 3, 30)),    # EST
    ])
    dates = {e["subject"]: e["date"] for e in lre.list_recent_emails_from(root, 10, tz=NY)["emails"]}
    assert dates == {"summer": "2026-07-01T12:16:00-04:00",
                     "before fall back": "2026-11-01T00:30:00-04:00",
                     "after fall back": "2026-11-01T03:30:00-05:00"}
    summer = lre.list_recent_emails_from(root, 10, since="2026-07-01T12:00",
                                         until="2026-07-02", tz=NY)
    assert _subjects(summer) == ["summer"] and summer["since"].endswith("-04:00")
    # The local day of 1 Nov holds both, whatever its offset.
    day = lre.list_recent_emails_from(root, 10, since="2026-11-01", until="2026-11-01", tz=NY)
    assert _subjects(day) == ["after fall back", "before fall back"]


def test_dst_via_the_system_zone(tmp_path, system_zone) -> None:
    root = _root(tmp_path, [("summer", _ts(2026, 7, 1, 12, 16))])
    system_zone("America/New_York")
    result = lre.list_recent_emails_from(root, 10, since="2026-07-01T12:00")
    assert result["emails"][0]["date"] == "2026-07-01T12:16:00-04:00"
    assert result["since"] == "2026-07-01T12:00:00-04:00"


# ---- read_email -------------------------------------------------------------------


def test_read_email_date_is_local_with_offset(system_zone) -> None:
    r = re_mod.read_email_from(101, FIXTURE_ROOT, tz=NY)  # Date: Mon, 09 Nov 2026 10:00:00 -0800
    assert r["date"] == "2026-11-09T13:00:00-05:00"
    system_zone("Europe/London")
    assert re_mod.read_email_from(101, FIXTURE_ROOT)["date"] == "2026-11-09T18:00:00+00:00"


def test_local_header_date_edge_cases() -> None:
    assert re_mod.local_header_date("Fri, 20 Nov 2026 17:16:17 -0000", NY) == "2026-11-20T12:16:17-05:00"
    assert re_mod.local_header_date("not a date", NY) == "not a date"
    assert re_mod.local_header_date("", NY) == ""
    assert re_mod.local_header_date(None, NY) == ""


# ---- nothing that compares mail dates regressed --------------------------------------


def test_callers_that_parse_mail_dates_see_the_same_instant_and_the_local_day() -> None:
    from ai import knowledge_index, watches
    from events import lookahead

    evening = _ts(2026, 11, 20, 20, 30)  # 01:30 UTC on the 21st
    local, utc = mw._iso_date(evening, NY), mw._iso_date(evening, timezone.utc)
    # knowledge index (_before / cut-offs) and lookahead parse to an instant.
    assert knowledge_index._timestamp(local) == knowledge_index._timestamp(utc) == evening
    assert lookahead._local(local, NOW) == lookahead._local(utc, NOW)
    # Watches key and show the mail's day: now the user's day, not UTC's.
    fallback = NOW.date()
    assert watches._event_date(local, fallback) == "2026-11-20"
    assert watches._event_date(utc, fallback) == "2026-11-21"


def test_knowledge_index_full_scan_sql_is_unchanged(noon_root) -> None:
    conn = sqlite3.connect(noon_root / "V10" / "MailData" / "Envelope Index")
    sql = lre._build_sql(mw.table_columns(conn))
    rows = conn.execute(sql, (500, 0)).fetchall()
    conn.close()
    assert "WHERE" not in sql and len(rows) == 4


def test_the_tool_takes_since_and_stays_read_only(noon_root, monkeypatch) -> None:
    from tools import meta_tool_generator as mtg

    monkeypatch.setenv("AGENT_MAIL_ROOT", str(noon_root))
    tool = next(t for t in tools.AVAILABLE_TOOLS if t.name == "list_recent_emails")
    assert set(tool.inputs) == {"count", "mailbox", "since", "until"}
    assert "since" in tool.description and "LOCAL" in tool.description
    assert "list_recent_emails" in tools.READ_ONLY_TOOL_NAMES
    section = mtg._format_available_tools_context()
    assert ("call: list_recent_emails(count: integer = optional, mailbox: string = optional, "
            "since: string = optional, until: string = optional)") in section
    assert 'use since for "how many since' in section
    result = tools.list_recent_emails(count=5, since="2000-01-01")
    assert result["count"] == 4


# ---- eval case -----------------------------------------------------------------------


def test_the_eval_case_wants_since_and_scores_both_ways() -> None:
    from tests.test_evals import _case, _run_tool_case

    case = _case("chat_count_emails_since_noon")
    good = _run_tool_case(case, ("list_recent_emails", {"since": "2026-11-20T12:00"}))
    assert good["ok"] is True, good
    capped = _run_tool_case(case, ("list_recent_emails", {"count": 50}))
    assert capped["ok"] is False and "missing argument since" in capped["reason"]
