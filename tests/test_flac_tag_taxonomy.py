"""
tests/test_flac_tag_taxonomy.py — the 2026-09-16 FLAC tag taxonomy.

Write side (build_recording_tags / write_flac_tags): ARTIST and ALBUMARTIST
both carry the act, ALBUM is "date - venue", DATE is the full partial date,
VENUE / LOCATION / SOURCE replace the CONCERT* and RECORDINGSOURCE keys, GENRE
is the act's genre, and PERFORMER repeats once per musician in the show's
resolved lineup. Existing comments are erased first, DISCNUMBER included.

Read side (read_flac_tags): the new keys and every retired key both land in
the same container fields, because every file tagged before this change still
carries the old ones.
"""
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.genre import Genre
from app.models.musician import Musician
from app.models.performance_personnel import PerformancePersonnel
from app.models.recording import Recording
from app.utils.ingest import (build_recording_tags, write_flac_tags,
                              read_flac_tags, _loose_tag_date)

RETIRED = {"CONCERTDATE", "CONCERTVENUE", "CONCERTLOCATION", "RECORDINGSOURCE"}


def _rec(seeded_ids):
    return _db.session.get(Recording, seeded_ids["recording_id"])


# ── Write side ──────────────────────────────────────────────────────────────

def test_container_tags_follow_the_taxonomy(app, seeded_ids):
    tags, total = build_recording_tags(_rec(seeded_ids))
    assert tags["ARTIST"] == tags["ALBUMARTIST"] == "Bill Evans"
    assert tags["ALBUM"] == "1980-02-22 - Sprague Memorial Hall"   # no act name
    assert tags["DATE"] == "1980-02-22"
    assert tags["VENUE"] == "Sprague Memorial Hall"
    assert tags["LOCATION"] == "New Haven, CT, US"
    assert tags["SOURCE"] == "AUD"
    assert tags["PERFORMER"] == ["Bill Evans"]      # inherited from the roster
    assert not RETIRED & set(tags)
    assert "GENRE" not in tags                      # no genre -> no empty tag
    assert total == "2"


def test_partial_date_is_not_padded(app, seeded_ids):
    rec = _rec(seeded_ids)
    rec.performance.start_day = None
    tags, _ = build_recording_tags(rec)
    assert tags["DATE"] == "1980-02"
    assert tags["ALBUM"] == "1980-02 - Sprague Memorial Hall"


def test_genre_and_guests(app, seeded_ids):
    rec = _rec(seeded_ids)
    genre = Genre(name="Jazz")
    _db.session.add(genre)
    rec.performance.artist.genre = genre
    guest = Musician(name="Eddie Gomez")
    _db.session.add(guest)
    _db.session.flush()
    _db.session.add(PerformancePersonnel(performance_id=rec.performance_id,
                                         musician_id=guest.id, is_guest=True, order=5))
    _db.session.commit()

    tags, _ = build_recording_tags(rec)
    assert tags["GENRE"] == "Jazz"
    assert tags["PERFORMER"] == ["Bill Evans", "Eddie Gomez"]   # guests unmarked


def _silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")


def test_write_erases_foreign_tags_and_writes_multi_performer(app, seeded_ids, tmp_path):
    rec = _rec(seeded_ids)
    guest = Musician(name="Marc Johnson")
    _db.session.add(guest)
    _db.session.flush()
    _db.session.add(PerformancePersonnel(performance_id=rec.performance_id,
                                         musician_id=guest.id, is_guest=False, order=1))
    _db.session.commit()

    for t in rec.tracks:
        f = tmp_path / rec.folder_path / t.file_path
        _silent_flac(f)
        audio = FLAC(str(f))
        audio["DISCNUMBER"] = "1"
        audio["DISCTOTAL"] = "2"
        audio["CONCERTVENUE"] = "Old Value"
        audio["COMMENT"] = "taper notes"
        audio.save()

    n, errors = write_flac_tags(rec, str(tmp_path))
    assert (n, errors) == (2, [])

    audio = FLAC(str(tmp_path / rec.folder_path / "01.flac"))
    keys = {k.upper() for k in audio.keys()}
    assert not {"DISCNUMBER", "DISCTOTAL", "COMMENT"} & keys
    assert not RETIRED & keys
    assert audio["PERFORMER"] == ["Bill Evans", "Marc Johnson"]
    assert audio["ALBUMARTIST"] == ["Bill Evans"]
    assert audio["DATE"] == ["1980-02-22"]
    assert audio["TITLE"] == ["My Foolish Heart"]
    assert audio["TRACKTOTAL"] == ["2"]


def test_track_note_becomes_comment(app, seeded_ids, tmp_path):
    rec = _rec(seeded_ids)
    rec.tracks[0].notes = "  Garcia on pedal steel.  "
    _db.session.commit()
    for t in rec.tracks:
        f = tmp_path / rec.folder_path / t.file_path
        _silent_flac(f)
        audio = FLAC(str(f)); audio["COMMENT"] = "taper notes"; audio.save()

    assert write_flac_tags(rec, str(tmp_path)) == (2, [])
    noted = FLAC(str(tmp_path / rec.folder_path / rec.tracks[0].file_path))
    plain = FLAC(str(tmp_path / rec.folder_path / rec.tracks[1].file_path))
    assert noted["COMMENT"] == ["Garcia on pedal steel."]
    assert "COMMENT" not in {k.upper() for k in plain.keys()}   # foreign comment erased, none written


# ── Read side ───────────────────────────────────────────────────────────────

def _read_one(tmp_path, **tags):
    f = tmp_path / "x.flac"
    _silent_flac(f)
    audio = FLAC(str(f))
    for k, v in tags.items():
        audio[k] = v
    audio.save()
    return read_flac_tags([{"path": str(f), "index": 0, "filename": "x.flac"}])["container"]


def test_reads_new_keys(app, tmp_path):
    c = _read_one(tmp_path, ARTIST="Hot Rize", DATE="1983-06-25", VENUE="Telluride Town Park",
                  LOCATION="Telluride, CO", SOURCE="SBD", LINEAGE="DAT > CD")
    assert c == {"artist": "Hot Rize", "concert_date": "1983-06-25", "year": "1983",
                 "venue": "Telluride Town Park", "location": "Telluride, CO",
                 "source": "SBD", "lineage": "DAT > CD"}


def test_reads_retired_keys_and_prefers_the_precise_date(app, tmp_path):
    c = _read_one(tmp_path, ARTIST="Hot Rize", DATE="1983", CONCERTDATE="1983-06-25",
                  CONCERTVENUE="Telluride Town Park", CONCERTLOCATION="Telluride, CO",
                  RECORDINGSOURCE="AUD")
    assert c["concert_date"] == "1983-06-25"
    assert c["year"] == "1983"
    assert (c["venue"], c["location"], c["source"]) == ("Telluride Town Park", "Telluride, CO", "AUD")


def test_date_in_venue_fills_empty_fields(app, tmp_path):
    c = _read_one(tmp_path, VENUE="2015.02.27 - Ryman Auditorium - Nashville, TN")
    assert c["venue"] == "Ryman Auditorium"
    assert c["location"] == "Nashville, TN"
    assert c["concert_date"] == "2015-02-27"


@pytest.mark.parametrize("raw,expected", [
    ("1977-05-08", "1977-05-08"), ("2015.02.27", "2015-02-27"), ("1977/5/8", "1977-05-08"),
    ("1977-05", "1977-05"), ("2026", "2026"), ("August 19, 2012", "2012-08-19"),
    ("1977-13-01", None), ("", None), ("unknown", None),
])
def test_loose_tag_date(raw, expected):
    assert _loose_tag_date(raw) == expected
