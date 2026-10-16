"""app/reference/build_lma.py: reading the archive's rights text, and merging into the list.

No network: parse_rights is pure, and apply() runs against temp copies of the files.
"""
import csv

import pytest

from app.reference import build_lma as b


@pytest.mark.parametrize("text, rules", [
    ("Audience and Soundboard uploads are both fine. 1/16/2020 update: "
     "No soundboard uploads for shows after May 2018.", [("SBD", "2018-05-01")]),
    ("Audience recordings only, please.", [("SBD", "")]),
    ("Soundboard recordings are not permitted.", [("SBD", "")]),
    ("Please do not upload soundboards.", [("SBD", "")]),
    ("No SBDs from shows after 12/29/2001.", [("SBD", "2001-12-29")]),
    ("No soundboards. No SBDs after 2010.", [("SBD", "")]),        # undated rule wins
    ("Trading of audience and soundboard recordings is encouraged.", []),
    ("Free for non-commercial use.", []),
])
def test_parse_rights_rules(text, rules):
    assert b.parse_rights(text)[0] == rules


def test_unread_restriction_goes_to_review_not_the_list():
    rules, review = b.parse_rights("Shows are stream only at the request of the band.")
    assert rules == []
    assert review == ["Shows are stream only at the request of the band."]


def test_dates():
    assert b.first_date("after May 2018") == ("2018-05-01", False)
    assert b.first_date("since 3/4/99") == ("1999-03-04", True)
    assert sorted(b.full_dates("1/16/2020 update, 5/26/2017")) == ["2017-05-26", "2020-01-16"]


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))[1:]


def test_apply_replaces_only_archive_rows_and_merges_names(tmp_path, monkeypatch):
    pol, thes = tmp_path / "p.csv", tmp_path / "t.csv"
    _write(pol, [["artist_name", "policy", "source", "start_date", "policy_source", "policy_date"],
                 ["Allman Brothers Band", "Restrict", "All", "", "dime", "2026-10-14"],
                 ["Old Band", "Permit", "All", "", "archive.org", "2010-01-01"]])
    _write(thes, [["artist_name", "variant", "variant_type", "mbid"],
                  ["Allman Brothers Band", "Allman Brothers Band", "name", ""],
                  ["Allman Brothers Band", "Allman Brothers", "alias", ""],
                  ["Old Band", "Old Band", "name", ""]])
    monkeypatch.setattr(b, "POLICY", pol)
    monkeypatch.setattr(b, "THESAURUS", thes)
    monkeypatch.setattr(b, "ATLAS", tmp_path / "no-atlas.sqlite")
    docs = [{"identifier": "ABB", "title": "Allman Brothers", "rights": "", "publicdate": "2005-01-01T00:00:00Z"},
            {"identifier": "NewBand", "title": "New Band", "rights": "No soundboards.", "publicdate": "2012-02-03"}]
    new, names, review = b.build_rows(docs)
    b.apply(new, names)
    p = _read(pol)
    assert ["Allman Brothers Band", "Restrict", "All", "", "dime", "2026-10-14"] in p      # other sources kept
    assert ["Allman Brothers Band", "Permit", "All", "", "archive.org", "2005-01-01"] in p  # merged by variant
    assert ["New Band", "Restrict", "SBD", "", "archive.org", "2012-02-03"] in p
    assert not any(r[0] == "Old Band" and r[4] == "archive.org" for r in p)                 # stale archive row gone
    t = _read(thes)
    assert ["New Band", "New Band", "name", ""] in t
    assert sum(1 for r in t if r[1] == "Allman Brothers") == 1
