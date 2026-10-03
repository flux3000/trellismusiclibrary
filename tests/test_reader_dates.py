"""Strict date grammar (Resolver v2 chunk 1, 2026-10-03): never fills an unwritten component."""
import json
import pathlib

import pytest

from app.utils.reader.dates import best_show_date, find_dates

FIX = pathlib.Path(__file__).parent / "fixtures" / "resolver"


def _ymd(text):
    b, _ = best_show_date(text)
    return (b.year, b.month, b.day) if b else None


def _fixture_rows():
    return [json.loads(l) for l in (FIX / "dates.jsonl").read_text().splitlines() if l.strip()]


@pytest.mark.parametrize("row", _fixture_rows(), ids=lambda r: r["id"])
def test_dates_fixture(row):
    g = row["gold"]["date"]
    assert _ymd(row["info_text"]) == (g["y"], g["m"], g["d"])


@pytest.mark.parametrize("text,expected", [
    ("August 1974", (1974, 8, None)),
    ("JULY 11,1981", (1981, 7, 11)),
    ("1962", (1962, None, None)),
    ("Date of 1971 is an estimate", (1971, None, None)),
    ("October 1975", (1975, 10, None)),
    ("29.August 1979", (1979, 8, 29)),
    ("??/??/1964", (1964, None, None)),
    ("1977-05-08", (1977, 5, 8)),
    ("1977-05", (1977, 5, None)),
    ("2017 11 14, Paris", (2017, 11, 14)),
    ("81-04-20", (1981, 4, 20)),
    ("6/24/01 and 2001", (2001, 6, 24)),
    ("May 5th, 1965", (1965, 5, 5)),
    ("no date here", None),
])
def test_forms(text, expected):
    assert _ymd(text) == expected


def test_range_sets_end_day_with_year_elsewhere():
    b, _ = best_show_date("Telluride 1983\nAug 15-17\n")
    assert (b.year, b.month, b.day, b.end_day) == (1983, 8, 15, 17)


def test_range_iso_end_day():
    b, _ = best_show_date("1965-03-14 thru 1965-03-16")
    assert (b.day, b.end_day) == (14, 16)


def test_alternatives_are_ambiguous_and_leave_day_empty():
    b, _ = best_show_date("February 6th or 7th, 2004")
    assert (b.year, b.month, b.day, b.ambiguous) == (2004, 2, None, True)


def test_unwritten_components_stay_none():
    for text in ("1962", "August 1974", "??/??/1964", "Aug 15"):
        b, _ = best_show_date(text)
        if b:
            assert b.written == {n for n, v in (("year", b.year), ("month", b.month), ("day", b.day)) if v}


def test_eac_log_line_is_never_the_show_date():
    assert best_show_date("EAC extraction logfile from 26. September 2001")[0] is None
    assert _ymd("EAC extraction logfile from 26. September 2001\nJune 18, 1983") == (1983, 6, 18)


def test_transfer_seed_upload_roles():
    ms = find_dates("Transferred FEB 18,2002\nSeeded 3/4/2003\nUploaded 5 June 2004\nJune 18, 1983")
    assert [m.role_hint for m in ms][:3] == ["transfer"] * 3
    assert _ymd("Transferred FEB 18,2002\nSeeded 3/4/2003\nJune 18, 1983") == (1983, 6, 18)


def test_year_and_month_day_combine_when_consistent():
    assert _ymd("Guitar Trio 1983\nJune 18\n") == (1983, 6, 18)


def test_conflicting_later_year_does_not_win_over_earlier_dated_line():
    assert _ymd("Show\n01-??-1953\nlater copy 2/23/1999 note") == (1953, 1, None)


def test_two_digit_year_pivot():
    # Live tapes from 1900-1926 essentially do not exist: small yy reads as 20yy.
    assert _ymd("6/18/83") == (1983, 6, 18)
    assert _ymd("12-15-01") == (2001, 12, 15)
    assert _ymd("6/18/05") == (2005, 6, 18)


def test_day_month_order():
    assert _ymd("5.30.2002") == (2002, 5, 30)                    # 30 cannot be a month
    assert _ymd("29.8.1979") == (1979, 8, 29)                    # 29 cannot be a month
    assert _ymd("15-12-01") == (2001, 12, 15)                    # unambiguous day-first still reads day-first
    assert _ymd("5/6/1983") == (1983, 5, 6)                      # both <= 12: month first, not flagged
    assert best_show_date("5/6/1983")[0].ambiguous is False
    assert _ymd("06/07/81") == (1981, 6, 7)                      # Paris show, still month first
    assert _ymd("12/07/91") == (1991, 12, 7)                     # Germany, still month first
    assert _ymd("5/6/1983\nJune 5, 1983") == (1983, 6, 5)        # another mention settles day-first


def test_guitar_trio_fixture():
    text = (FIX / "guitar_trio.txt").read_text()
    assert _ymd(text) == (1983, 6, 18)


def test_never_fills_from_today():
    # Old dateutil path returned 2026-07-11 / 1974-08-03 for these.
    assert _ymd("JULY 11,1981") == (1981, 7, 11)
    assert _ymd("August 1974")[2] is None
