"""
tests/test_folder_name_inference.py — what a folder name alone can tell us.

The folder name is a third witness alongside the FLAC tags and the info file,
and for a folder with no info file it is the ONLY one — which is the exact
case a collector adopting an existing library is in.

Source detection (detect_source_from_name) has been in the codebase since
2026-08 and was never covered by a test; gear detection
(detect_gear_from_name) is new on 2026-09-17. Both are pure string logic, no
DB or app context needed.

The negative controls are the point of this file. A folder name is short and
adversarial — act names, venue names, city names, taper surnames — and a
wrong value written unattended across a few thousand adopted folders is worse
than no value at all. Every NEGATIVE case below is a real-world shape that a
naive matcher gets wrong.
"""

import pytest

from app.utils.ingest import detect_source_from_name, detect_gear_from_name


# ── Source ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("folder, expected", [
    ("gd1977-05-08.aud.schoeps.nak700",            "AUD"),
    ("Grateful Dead - 1977-05-08 - Barton Hall (SBD)", "SBD"),
    ("cs2024-08-16.mtx.koucky.flac1648",           "MTX"),
    ("dm1975-01-12.fm.pre-fm",                     "FM"),
    # Last marker wins: a matrix of an SBD is a matrix.
    ("ph1997.sbd.mtx",                             "MTX"),
])
def test_source_from_folder_name(folder, expected):
    assert detect_source_from_name(folder) == expected


@pytest.mark.parametrize("folder", [
    "Miles Davis at the Fillmore",          # "aud" is not in here at all
    "Grateful Dead - 1977-05-08 - Ithaca, NY",
    "Claude Bolling Trio 1975",             # 'aud' inside Claude
    "The Audience 1980",                    # bare English word
])
def test_source_from_folder_name_negative(folder):
    assert detect_source_from_name(folder) is None


# ── Gear ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("folder, expected", [
    ("gd1977-05-08.aud.schoeps.nak700.t01flac16", ["schoeps", "nak700"]),
    ("dso2003-04-12.akg451.mtx",                  ["akg451"]),
    ("ph1997-11-17.sbd.sony.dat",                 ["sony", "dat"]),
    ("wsp2008-02-29.at853.ua5.flac24",            ["at853", "ua5"]),
    ("Grateful Dead - 1977-05-08 (AUD) [neumann km184]", ["neumann", "km184"]),
    ("bg2011-06-18.ca14.sp-preamp",               ["ca14"]),
    ("jj1998.schoeps>lunatec>dat",                ["schoeps", "lunatec", "dat"]),
    ("trey.mbho603a.dr680",                       ["mbho603a", "dr680"]),
])
def test_gear_from_folder_name(folder, expected):
    assert [g.lower() for g in detect_gear_from_name(folder)] == expected


@pytest.mark.parametrize("folder", [
    "Grateful Dead - 1977-05-08 - Barton Hall, Ithaca, NY",
    "Phish 1997-11-17 - Berkeley, CA",       # state code, not Church Audio
    "Miles Davis at the Fillmore",           # "at" the preposition
    "Show - Baltimore, MD 21201",
    "gd77-05-08d01t01",                      # disc marker, not a deck
    "Nakamura Quartet 1999",                 # surname containing 'nak'
    "Catherine Russell 2015",                # 'ca' inside a word
    "Spyro Gyra 1980",                       # 'sp' inside a word
    "The Sonics 1965",                       # 'sony' is not in 'Sonics'
    "Beyer Brothers Band",                   # surname, no model number
    "Concert - Sacramento, CA 95814",        # glued ZIP must not read as a model
    "Sun Ra Arkestra 1978",
])
def test_gear_from_folder_name_negative(folder):
    assert detect_gear_from_name(folder) == []


def test_gear_is_deduped_in_order_of_appearance():
    assert [g.lower() for g in
            detect_gear_from_name("sci2002.akg451.akg451.mk4")] == ["akg451", "mk4"]


def test_gear_handles_empty_input():
    assert detect_gear_from_name("") == []
    assert detect_gear_from_name(None) == []


def test_gear_is_a_list_not_a_signal_chain():
    """A folder names some gear; it does not state a lineage.

    The caller joins with ", " precisely so nobody reads an invented ">" as
    the taper's own. If this ever starts returning a joined string, the
    fabricated-chain problem comes back with it.
    """
    got = detect_gear_from_name("gd1977.schoeps.nak700")
    assert isinstance(got, list)
    assert not any(">" in g for g in got)
