"""The shipped artist policy list and its thesaurus (assets/reference/) stay well formed.

Pure stdlib: runs anywhere, including over the Cowork bridge.
"""
import csv
import datetime
import re
from pathlib import Path

REF = Path(__file__).resolve().parent.parent / "assets" / "reference"
POLICY = REF / "artist_policy.csv"
THESAURUS = REF / "artist_thesaurus.csv"


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_policy_columns_and_values():
    rows = _rows(POLICY)
    assert rows
    assert list(rows[0]) == ["artist_name", "policy", "source", "start_date",
                             "policy_source", "policy_date"]
    for r in rows:
        assert r["artist_name"].strip(), r
        assert r["policy"] in ("Permit", "Restrict"), r
        assert r["source"] in ("SBD", "AUD", "All"), r
        assert r["policy_source"].strip(), r   # any named source: etree, dime, an artist's site
        for k in ("start_date", "policy_date"):
            if r[k]:
                datetime.date.fromisoformat(r[k])


def test_policy_has_no_duplicate_rules():
    keys = [(r["artist_name"], r["policy"], r["source"], r["start_date"], r["policy_source"])
            for r in _rows(POLICY)]
    assert len(keys) == len(set(keys))


def test_thesaurus_covers_every_policy_artist():
    policy = {r["artist_name"] for r in _rows(POLICY)}
    thes = _rows(THESAURUS)
    named = {r["artist_name"] for r in thes if r["variant_type"] == "name"}
    assert policy == named
    assert {r["artist_name"] for r in thes} <= policy


def test_thesaurus_values():
    thes = _rows(THESAURUS)
    assert list(thes[0]) == ["artist_name", "variant", "variant_type", "mbid"]
    owner = {}
    for r in thes:
        assert r["variant_type"] in ("name", "alias", "abbreviation", "inverted", "misspelling"), r
        if r["mbid"]:
            assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r["mbid"]), r
        # one variant never points at two artists
        assert owner.setdefault(r["variant"].casefold(), r["artist_name"]) == r["artist_name"], r


def test_build_ships_the_reference_folder():
    spec = (REF.parent.parent / "trellis.spec").read_text(encoding="utf-8")
    assert '("assets/reference", "assets/reference")' in spec
