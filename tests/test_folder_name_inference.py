"""
tests/test_folder_name_inference.py — what a folder name alone can tell us.

The folder name is a third witness alongside the FLAC tags and the info file,
and for a folder with no info file it is the ONLY one — which is the exact
case a collector ingesting an existing library is in.

Source detection (detect_source_from_name) has been in the codebase since
2026-08 and was never covered by a test; gear detection
(detect_gear_from_name) is new on 2026-09-17. Both are pure string logic, no
DB or app context needed.

The negative controls are the point of this file. A folder name is short and
adversarial — act names, venue names, city names, taper surnames — and a
wrong value written unattended across a few thousand ingested folders is worse
than no value at all. Every NEGATIVE case below is a real-world shape that a
naive matcher gets wrong.
"""

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.artist import Artist
from app.models.recording import Recording
from app.utils.ingest import (detect_source_from_name, detect_gear_from_name,
                              detect_source_tag_from_name, detect_shnid_from_name,
                              build_scan_payload)


def _login_as(client, username="admin"):
    from app.extensions import login_manager
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


@pytest.fixture()
def client(app):
    return app.test_client()


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


# ── Source tag (spec section 5) ──────────────────────────────────────────────
# Recording.source_tag: the first equipment token _GEAR_IN_FOLDER matches,
# verbatim — same detector as detect_gear_from_name, first hit only.

@pytest.mark.parametrize("folder, expected", [
    ("gd1977-05-08.aud.schoeps.nak700.t01flac16", "schoeps"),
    ("wsp2008-02-29.at853.ua5.flac24",            "at853"),
    ("ph1997-11-17.sbd.sony.dat",                 "sony"),
])
def test_source_tag_from_folder_name(folder, expected):
    assert detect_source_tag_from_name(folder) == expected


# ── shnid (spec section 5) ────────────────────────────────────────────────────
# Recording.etree_shnid: the last dot/underscore/hyphen-delimited all-digit
# segment of 3-7 digits that is not the year of a matched date and not itself
# a plausible year (1900..2099).

@pytest.mark.parametrize("folder, expected", [
    ("gd1988-05-01.ec7.bowen.foster.118671.flac16", 118671),
    ("gd1969-01-25.sbd.kaplan.7923.sbeok.shnf",     7923),
    ("pat.metheny-2026-06-07_24.96_tr.16",          None),
])
def test_shnid_from_folder_name(folder, expected):
    assert detect_shnid_from_name(folder) == expected


def test_shnid_ignores_a_glued_digit_run():
    """"flac16" in the LMA example is glued to letters, not a delimited
    segment, so it never competes with the real shnid."""
    assert detect_shnid_from_name("gd1988-05-01.ec7.bowen.foster.118671.flac16") == 118671


# ── Negative control: names that carry neither (spec section 7) ─────────────
# Eight real-world folder-name shapes with no equipment token and no digit
# run outside a date/year — a wrong source_tag or shnid written unattended
# across a few thousand ingested folders is worse than no value at all.

@pytest.mark.parametrize("folder", [
    "Grateful Dead - 1977-05-08 - Barton Hall, Ithaca, NY",
    "Phish 1997-11-17 - Berkeley, CA",
    "Miles Davis at the Fillmore",
    "Nakamura Quartet 1999",
    "Catherine Russell 2015",
    "Sun Ra Arkestra 1978",
    "The Sonics 1965",
    "Beyer Brothers Band",
])
def test_source_tag_and_shnid_negative(folder):
    assert detect_source_tag_from_name(folder) is None
    assert detect_shnid_from_name(folder) is None


def test_build_scan_payload_suggests_shnid_from_folder_name(tmp_path):
    """The LMA example folder name: build_scan_payload surfaces the shnid as
    a suggestion, flagged, and writes nothing on its own — a scan is
    read-only (spec section 5 / chunk 4 done-when)."""
    folder = tmp_path / "gd1988-05-01.ec7.bowen.foster.118671.flac16"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"x")

    scan = build_scan_payload(str(folder))

    assert scan["suggestions"]["from_info_file"]["etree_shnid"] == 118671
    assert scan["shnid_from_folder_name"] is True


def test_build_scan_payload_suggests_source_tag_from_folder_name(tmp_path):
    folder = tmp_path / "gd1977-05-08.aud.schoeps.nak700"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"x")

    scan = build_scan_payload(str(folder))

    assert scan["suggestions"]["from_info_file"]["source_tag"] == "schoeps"
    assert scan["source_tag_from_folder_name"] is True


# ── Round-trip through the PUT endpoints (chunk 4 done-when) ────────────────

def test_recording_source_tag_and_shnid_round_trip(app, client, seeded_ids):
    _login_as(client)
    resp = client.put(f"/api/recordings/{seeded_ids['recording_id']}",
                       json={"source_tag": "schoeps", "etree_shnid": 118671})
    assert resp.status_code == 200

    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    assert rec.source_tag == "schoeps"
    assert rec.etree_shnid == 118671

    got = client.get(f"/api/recordings/{seeded_ids['recording_id']}").get_json()
    assert got["source_tag"] == "schoeps"
    assert got["etree_shnid"] == 118671

    # Clearing shnid through the same string-in, integer-out coercion.
    resp = client.put(f"/api/recordings/{seeded_ids['recording_id']}",
                       json={"etree_shnid": ""})
    assert resp.status_code == 200
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    assert rec.etree_shnid is None


def test_artist_abbreviation_round_trips(app, client, seeded_ids):
    _login_as(client)
    resp = client.put(f"/api/artists/{seeded_ids['artist_id']}",
                       json={"abbreviation": "be"})
    assert resp.status_code == 200

    artist = _db.session.get(Artist, seeded_ids["artist_id"])
    assert artist.abbreviation == "be"

    got = client.get(f"/api/artists/{seeded_ids['artist_id']}").get_json()
    assert got["abbreviation"] == "be"
