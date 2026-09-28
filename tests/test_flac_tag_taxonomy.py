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
import base64
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from app.extensions import db as _db
from app.models.genre import Genre
from app.models.musician import Musician
from app.models.performance_personnel import PerformancePersonnel
from app.models.recording import Recording
from app.utils.ingest import (build_recording_tags, write_flac_tags,
                              read_flac_tags, read_recording_tags,
                              open_tags, scan_folder, _loose_tag_date)

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


def test_disc_number_and_total_written_for_a_multi_disc_recording(app, seeded_ids, tmp_path):
    """DISCNUMBER/DISCTOTAL are written per track once any track carries a
    disc_number, and DISCTOTAL is max(disc_number) across the recording."""
    rec = _rec(seeded_ids)
    rec.tracks[0].disc_number = 1
    rec.tracks[1].disc_number = 2
    _db.session.commit()

    for t in rec.tracks:
        f = tmp_path / rec.folder_path / t.file_path
        _silent_flac(f)

    n, errors = write_flac_tags(rec, str(tmp_path))
    assert (n, errors) == (2, [])

    audio0 = FLAC(str(tmp_path / rec.folder_path / rec.tracks[0].file_path))
    audio1 = FLAC(str(tmp_path / rec.folder_path / rec.tracks[1].file_path))
    assert audio0["DISCNUMBER"] == ["1"]
    assert audio0["DISCTOTAL"] == ["2"]
    assert audio1["DISCNUMBER"] == ["2"]
    assert audio1["DISCTOTAL"] == ["2"]


def test_disc_tags_absent_for_a_single_disc_recording(app, seeded_ids, tmp_path):
    rec = _rec(seeded_ids)
    assert all(t.disc_number is None for t in rec.tracks)

    for t in rec.tracks:
        f = tmp_path / rec.folder_path / t.file_path
        _silent_flac(f)

    assert write_flac_tags(rec, str(tmp_path)) == (2, [])
    for t in rec.tracks:
        keys = {k.upper() for k in FLAC(str(tmp_path / rec.folder_path / t.file_path)).keys()}
        assert not {"DISCNUMBER", "DISCTOTAL"} & keys


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


# ── DISCNUMBER tag carrier (scan_folder) ─────────────────────────────────────

def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def test_flat_folder_disc_tags_fill_disc_fields_from_index_order(tmp_path):
    root = tmp_path / "flat_tagged"; root.mkdir()
    _flac_with_tags(root / "01.flac", DISCNUMBER="1", TRACKNUMBER="1", DISCTOTAL="2")
    _flac_with_tags(root / "02.flac", DISCNUMBER="1", TRACKNUMBER="2", DISCTOTAL="2")
    _flac_with_tags(root / "03.flac", DISCNUMBER="2", TRACKNUMBER="1", DISCTOTAL="2")
    _flac_with_tags(root / "04.flac", DISCNUMBER="2", TRACKNUMBER="2", DISCTOTAL="2")

    result = scan_folder(str(root))

    assert [f["index"] for f in result["audio_files"]] == [1, 2, 3, 4]
    assert [f["disc_number"] for f in result["audio_files"]] == [1, 1, 2, 2]
    assert [f["disc_track_number"] for f in result["audio_files"]] == [1, 2, 1, 2]


def test_disc_tags_win_over_subdirs_that_disagree(tmp_path):
    root = tmp_path / "cd_vs_tag"; root.mkdir()
    cd1 = root / "CD1"; cd1.mkdir()
    cd2 = root / "CD2"; cd2.mkdir()
    # Subdir says CD1/CD2 -> disc 1/2, but every file's own tag says the
    # opposite. The tag wins.
    _flac_with_tags(cd1 / "01.flac", DISCNUMBER="2", TRACKNUMBER="1")
    _flac_with_tags(cd1 / "02.flac", DISCNUMBER="2", TRACKNUMBER="2")
    _flac_with_tags(cd2 / "01.flac", DISCNUMBER="1", TRACKNUMBER="1")
    _flac_with_tags(cd2 / "02.flac", DISCNUMBER="1", TRACKNUMBER="2")

    result = scan_folder(str(root))

    by_path = {f["path"]: f for f in result["audio_files"]}
    assert by_path[str(cd1 / "01.flac")]["disc_number"] == 2
    assert by_path[str(cd1 / "02.flac")]["disc_number"] == 2
    assert by_path[str(cd2 / "01.flac")]["disc_number"] == 1
    assert by_path[str(cd2 / "02.flac")]["disc_number"] == 1
    assert [f["disc_track_number"] for f in result["audio_files"]] == [1, 2, 1, 2]


def test_partially_tagged_subdirs_keep_carrier_result(tmp_path):
    root = tmp_path / "cd_partial"; root.mkdir()
    cd1 = root / "CD1"; cd1.mkdir()
    cd2 = root / "CD2"; cd2.mkdir()
    _flac_with_tags(cd1 / "01.flac", DISCNUMBER="1")   # tagged
    (cd1 / "02.flac").parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(cd1 / "02.flac"), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    # no DISCNUMBER tag on cd1/02.flac -> not every file agrees -> ignored
    _flac_with_tags(cd2 / "01.flac", DISCNUMBER="2")

    result = scan_folder(str(root))

    # Matches today's subdir-only result exactly: disc_number from CD1/CD2,
    # disc_track_number 1-based within each disc.
    by_path = {f["path"]: f for f in result["audio_files"]}
    assert by_path[str(cd1 / "01.flac")]["disc_number"] == 1
    assert by_path[str(cd1 / "02.flac")]["disc_number"] == 1
    assert by_path[str(cd2 / "01.flac")]["disc_number"] == 2
    assert [f["disc_track_number"] for f in result["audio_files"]] == [1, 2, 1]


def test_lone_disc_one_with_no_disctotal_stays_null(tmp_path):
    root = tmp_path / "lone_disc"; root.mkdir()
    _flac_with_tags(root / "01.flac", DISCNUMBER="1", TRACKNUMBER="1")
    _flac_with_tags(root / "02.flac", DISCNUMBER="1", TRACKNUMBER="2")

    result = scan_folder(str(root))

    assert all(f["disc_number"] is None for f in result["audio_files"])
    assert all(f["disc_track_number"] is None for f in result["audio_files"])


def test_disc_one_of_disctotal_greater_than_one_is_kept(tmp_path):
    root = tmp_path / "disc_one_of_many"; root.mkdir()
    _flac_with_tags(root / "01.flac", DISCNUMBER="1", TRACKNUMBER="1", DISCTOTAL="3")
    _flac_with_tags(root / "02.flac", DISCNUMBER="1", TRACKNUMBER="2", DISCTOTAL="3")

    result = scan_folder(str(root))

    assert [f["disc_number"] for f in result["audio_files"]] == [1, 1]
    assert [f["disc_track_number"] for f in result["audio_files"]] == [1, 2]


def test_tag_disc_carrier_sets_sets_detected(tmp_path):
    """The latent bug (spec chunk 7f): a flat folder with no subdir/filename
    carrier at all still got its disc fields stamped by DISCNUMBER tags, but
    sets_detected stayed False -- app.js's toTracks() trusts that flag, not
    the presence of disc_number, to pick index vs tag ordering."""
    root = tmp_path / "flat_tagged_sets"; root.mkdir()
    _flac_with_tags(root / "01.flac", DISCNUMBER="1", TRACKNUMBER="1", DISCTOTAL="2")
    _flac_with_tags(root / "02.flac", DISCNUMBER="1", TRACKNUMBER="2", DISCTOTAL="2")
    _flac_with_tags(root / "03.flac", DISCNUMBER="2", TRACKNUMBER="1", DISCTOTAL="2")
    _flac_with_tags(root / "04.flac", DISCNUMBER="2", TRACKNUMBER="2", DISCTOTAL="2")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True


# ── MP3 parity ────────────────────────────────────────────────────────────────

# A minimal, real (ffmpeg-encoded) silent MP3 captured once, base64-encoded,
# for machines that don't have ffmpeg available to render a fresh one.
_TINY_MP3_B64 = (
    "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tAwAAAAAAA"
    "AAAAAAAAAAAAAAAAASW5mbwAAAA8AAAADAAAB7wCTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5"
    "OTk5OTk5OTk5PKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr////////////"
    "////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAA"
    "JAKjAAAAAAAAAe8wbCjYAAAAAAD/+xDEAAPAAAGkAAAAIAAANIAAAARMQU1FMy4xMDBVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVf/7EsQpg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7EMRTg8AAAaQAAAAgAAA0gAAABFVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
)


def _silent_mp3(path):
    """A tiny valid MP3 -- ffmpeg-rendered when available, else a canned
    known-good encode of the same clip (see _TINY_MP3_B64 above)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.05", "-codec:a", "libmp3lame", "-b:a", "32k", str(path)],
            check=True, capture_output=True,
        )
    else:
        path.write_bytes(base64.b64decode(_TINY_MP3_B64))


def test_mp3_round_trip_through_open_tags(app, seeded_ids, tmp_path):
    rec = _rec(seeded_ids)
    rec.tracks[0].disc_number = 1
    rec.tracks[1].disc_number = 2
    _db.session.commit()

    mp3_paths = []
    for t in rec.tracks:
        stem = Path(t.file_path).stem
        f = tmp_path / rec.folder_path / f"{stem}.mp3"
        _silent_mp3(f)
        mp3_paths.append(f)
        t.file_path = f"{stem}.mp3"
    _db.session.commit()

    # read_flac_tags (despite the name) reads an MP3 folder through open_tags
    audio_files = [{"path": str(p), "index": i, "filename": p.name}
                   for i, p in enumerate(mp3_paths, start=1)]
    # No tags yet -- container should just come back empty, not error.
    assert read_flac_tags(audio_files)["container"] == {}

    n, errors = write_flac_tags(rec, str(tmp_path))
    assert (n, errors) == (2, [])

    container = read_flac_tags(audio_files)["container"]
    assert container["artist"] == "Bill Evans"
    assert container["venue"] == "Sprague Memorial Hall"
    assert container["concert_date"] == "1980-02-22"

    tag_rows = read_recording_tags(rec, str(tmp_path))
    assert all(row["error"] is None for row in tag_rows)
    assert tag_rows[0]["tags"]["VENUE"] == "Sprague Memorial Hall"

    audio0 = open_tags(str(mp3_paths[0]))
    audio1 = open_tags(str(mp3_paths[1]))
    assert audio0["ARTIST"] == ["Bill Evans"]
    assert audio0["ALBUM"] == ["1980-02-22 - Sprague Memorial Hall"]
    assert audio0["DATE"] == ["1980-02-22"]
    assert audio0["VENUE"] == ["Sprague Memorial Hall"]
    assert audio0["DISCNUMBER"] == ["1"]
    assert audio0["DISCTOTAL"] == ["2"]
    assert audio1["DISCNUMBER"] == ["2"]
    assert audio1["DISCTOTAL"] == ["2"]

    # Underlying ID3: VENUE really is a TXXX frame, disc really is TPOS.
    raw_id3 = MP3(str(mp3_paths[0])).tags
    txxx_venue = raw_id3.getall("TXXX:VENUE")
    assert txxx_venue and txxx_venue[0].text == ["Sprague Memorial Hall"]
    assert raw_id3.getall("TPOS")[0].text == ["1/2"]


# ── S6: MP3 write_tags must keep cover art and foreign frames ──────────────

def test_mp3_write_tags_keeps_apic_and_foreign_txxx(app, seeded_ids, tmp_path):
    """S6: _MP3TagAdapter.clear() used to call ID3.clear(), which removes
    EVERYTHING -- cover art (APIC) and any TXXX frame Trellis does not own.
    It must only remove the frames it is about to rewrite, matching
    FLAC.clear()'s blast radius (Vorbis block only, pictures kept)."""
    from mutagen.id3 import APIC, TXXX

    rec = _rec(seeded_ids)
    f = tmp_path / rec.folder_path / rec.tracks[0].file_path
    f = f.with_suffix(".mp3")
    _silent_mp3(f)
    rec.tracks[0].file_path = f.name
    _silent_flac(tmp_path / rec.folder_path / rec.tracks[1].file_path)
    _db.session.commit()

    audio = MP3(str(f))
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover",
                         data=b"\xff\xd8\xff\xd9"))
    audio.tags.add(TXXX(encoding=3, desc="MY_CUSTOM", text=["keep me"]))
    audio.save()

    n, errors = write_flac_tags(rec, str(tmp_path))
    assert (n, errors) == (2, [])

    raw_id3 = MP3(str(f)).tags
    keys = set(raw_id3.keys())
    assert any(k.startswith("APIC") for k in keys), "cover art must survive the tag write"
    assert "TXXX:MY_CUSTOM" in keys, "a foreign TXXX frame must survive the tag write"


# ── N6: tag-key case must not differ by format ─────────────────────────────

def test_read_recording_tags_case_matches_across_formats(app, seeded_ids, tmp_path):
    """N6: read_recording_tags must return the same-case keys for a FLAC
    track and an MP3 track -- mutagen's native FLAC dict yields lowercase
    Vorbis-comment keys while _MP3TagAdapter always yields uppercase."""
    rec = _rec(seeded_ids)
    flac_f = tmp_path / rec.folder_path / rec.tracks[0].file_path
    mp3_f = (tmp_path / rec.folder_path / rec.tracks[1].file_path).with_suffix(".mp3")
    _silent_flac(flac_f)
    _silent_mp3(mp3_f)
    rec.tracks[1].file_path = mp3_f.name
    _db.session.commit()

    n, errors = write_flac_tags(rec, str(tmp_path))
    assert (n, errors) == (2, [])

    tag_rows = read_recording_tags(rec, str(tmp_path))
    for row in tag_rows:
        assert row["error"] is None
        assert row["tags"]["VENUE"] == "Sprague Memorial Hall"
        assert "venue" not in row["tags"]
