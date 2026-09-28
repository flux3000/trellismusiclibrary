"""
tests/test_bulk_ingest_classify.py -- Bulk Ingest's pure classification layer
(app/utils/bulk_ingest.py). Real tiny FLAC/MP3 files tagged via mutagen, real
folders under tmp_path acting as library_root. No DB, no network.
"""
import re
import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import TPE1, TALB, TDRC

from app.utils.bulk_ingest import (extract, classify, confirm_payload,
                                    _folder_tree_artist, classify_kind, folder_format)


def test_no_musicbrainz_import():
    import ast
    with open("app/utils/bulk_ingest.py") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    assert not any("musicbrainz" in n for n in names)


def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _mp3_with_tags(path, artist=None, album=None, date=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A real tiny MP3 (silence) so MP3()/mutagen can open it.
    import subprocess, shutil as _sh
    if _sh.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.1", "-codec:a", "libmp3lame", str(path)],
            check=True, capture_output=True)
    else:
        # Minimal MP3 frame header + silence, good enough for mutagen.MP3 to open.
        frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(100)
        path.write_bytes(frame * 20)
    audio = MP3(str(path))
    if audio.tags is None:
        audio.add_tags()
    if artist:
        audio.tags.setall("TPE1", [TPE1(encoding=3, text=[artist])])
    if album:
        audio.tags.setall("TALB", [TALB(encoding=3, text=[album])])
    if date:
        audio.tags.setall("TDRC", [TDRC(encoding=3, text=[date])])
    audio.save()


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="WAV")


def _shn(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real shn file, just bytes")


# ── Folder-tree artist ───────────────────────────────────────────────────────

def test_folder_tree_artist_skips_year_folder(tmp_path):
    root = tmp_path / "Root"
    show = root / "Grateful Dead" / "1977" / "show"
    show.mkdir(parents=True)
    assert _folder_tree_artist(str(show), str(root)) == "Grateful Dead"


def test_folder_tree_artist_none_for_root_placement(tmp_path):
    root = tmp_path / "Root"
    show = root / "Grateful Dead" / "1977" / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac")
    extracted = extract(str(show), str(root), placement="root")
    assert extracted["artist"] is None
    assert extracted["artist_source"] is None


def test_folder_tree_artist_skips_underscore_ancestor(tmp_path):
    root = tmp_path / "Root"
    show = root / "_venues" / "Grateful Dead" / "show"
    show.mkdir(parents=True)
    assert _folder_tree_artist(str(show), str(root)) == "Grateful Dead"


# ── Artist resolution / classification ──────────────────────────────────────

def test_tag_artist_mismatch_yields_no_tag_artist(tmp_path):
    root = tmp_path / "Root"
    show = root / "Grateful Dead" / "1977-05-08"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Grateful Dead")
    _flac_with_tags(show / "02.flac", ARTIST="Some Other Band")
    extracted = extract(str(show), str(root), placement="artist")
    # Tags disagree, no info file -- falls through to folder tree.
    assert extracted["artist"] == "Grateful Dead"
    assert extracted["artist_source"] == "folder"


def test_needs_artist_when_nothing_resolves(tmp_path):
    root = tmp_path / "Root"
    show = root / "1977-05-08"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert (status, reason) == ("review", "needs_artist")


def test_live_show_full_date_ingested(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08",
                     VENUE="Barton Hall")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["year"] == 1977 and extracted["month"] == 5 and extracted["day"] == 8
    assert extracted["venue"] == "Barton Hall"
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("ingested", None, "live")


def test_live_show_date_in_tags_no_venue_still_ingested(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["venue"] is None
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("ingested", None, "live")


def test_year_only_with_venue_ingested(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995", VENUE="The Gorge")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["year"] == 1995 and extracted["month"] is None
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("ingested", None, "live")


def test_year_and_month_no_day_ingested(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995-07")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["year"] == 1995 and extracted["month"] == 7 and extracted["day"] is None
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("ingested", None, "live")


def test_year_only_no_venue_needs_date(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("review", "needs_date", "live")


def test_no_year_needs_date(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("review", "needs_date", "live")


# ── Placeholder venue ────────────────────────────────────────────────────────

def test_placeholder_venue_counts_as_none(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995",
                     VENUE="Unknown Venue")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["venue"] is None
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("review", "needs_date", "live")


# ── Studio / dateless album ──────────────────────────────────────────────────

def test_studio_album_ingests_without_date(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")
    _flac_with_tags(show / "02.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["album"] == "A Picture of Nectar"
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("ingested", None, "studio")


def test_mp3_studio_with_year_no_venue_ingested(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _mp3_with_tags(show / "01.mp3", artist="The Band", album="The Band", date="1969")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["artist"] == "The Band"
    assert extracted["album"] == "The Band"
    status, reason, kind = classify(extracted)
    assert (status, reason, kind) == ("ingested", None, "studio")


def test_confirm_payload_studio_title_is_album_no_venue(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    body = confirm_payload(extracted, kind)
    assert body["title"] == "A Picture of Nectar"
    assert body["venue_name"] is None
    assert body["kind"] == "studio"


def test_confirm_payload_track_numbers_are_continuous_across_discs(tmp_path):
    # Two-disc source with per-disc TRACKNUMBER tags (1, 2, 1 -- CD2 restarts
    # at 1). track_number must be the scan's continuous index (1, 2, 3), not
    # the raw per-disc tag, and disc_number/disc_track_number must carry
    # through to the confirm payload (see the "Multi-disc detection"
    # contract in CONTEXT.md and app/api/ingest.py's _do_confirm step 8).
    root = tmp_path / "Root"
    show = root / "show"
    (show / "CD1").mkdir(parents=True)
    (show / "CD2").mkdir(parents=True)
    _flac_with_tags(show / "CD1" / "01.flac", ARTIST="Phish", ALBUM="Live Show",
                     TITLE="One", DATE="1997-07-01", VENUE="The Barn",
                     TRACKNUMBER="1")
    _flac_with_tags(show / "CD1" / "02.flac", ARTIST="Phish", ALBUM="Live Show",
                     TITLE="Two", DATE="1997-07-01", VENUE="The Barn",
                     TRACKNUMBER="2")
    _flac_with_tags(show / "CD2" / "01.flac", ARTIST="Phish", ALBUM="Live Show",
                     TITLE="Three", DATE="1997-07-01", VENUE="The Barn",
                     TRACKNUMBER="1")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    body = confirm_payload(extracted, kind)
    tracks = body["tracks"]
    assert [t["track_number"] for t in tracks] == [1, 2, 3]
    assert [t["disc_number"] for t in tracks] == [1, 1, 2]
    assert [t["disc_track_number"] for t in tracks] == [1, 2, 1]


def test_confirm_payload_track_numbers_continuous_for_tag_disc_carrier(tmp_path):
    # Flat folder (no CD1/CD2 subdirs) where every file carries a DISCNUMBER
    # tag instead -- scan_folder()'s DISCNUMBER tag-carrier path
    # (_apply_tag_disc_carrier) populates disc_number/disc_track_number here
    # but never flips payload["sets_detected"], so confirm_payload must not
    # rely on that flag alone to decide the tag is untrustworthy.
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "a.flac", ARTIST="Phish", ALBUM="Live Show",
                     TITLE="One", DATE="1997-07-01", VENUE="The Barn",
                     TRACKNUMBER="1", DISCNUMBER="1", DISCTOTAL="2")
    _flac_with_tags(show / "b.flac", ARTIST="Phish", ALBUM="Live Show",
                     TITLE="Two", DATE="1997-07-01", VENUE="The Barn",
                     TRACKNUMBER="2", DISCNUMBER="1", DISCTOTAL="2")
    _flac_with_tags(show / "c.flac", ARTIST="Phish", ALBUM="Live Show",
                     TITLE="Three", DATE="1997-07-01", VENUE="The Barn",
                     TRACKNUMBER="1", DISCNUMBER="2", DISCTOTAL="2")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    body = confirm_payload(extracted, kind)
    tracks = body["tracks"]
    assert [t["track_number"] for t in tracks] == [1, 2, 3]
    assert [t["disc_number"] for t in tracks] == [1, 1, 2]


# ── Unsupported / no audio ───────────────────────────────────────────────────

def test_wav_only_folder_is_unsupported_format(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _wav(show / "01.wav")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert (status, reason) == ("review", "unsupported_format")


def test_shn_only_folder_is_unsupported_format(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _shn(show / "01.shn")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert (status, reason) == ("review", "unsupported_format")


def test_empty_folder_is_no_audio(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert (status, reason) == ("failed", "no_audio")


def test_corrupt_flac_only_folder_is_failed_unreadable(tmp_path):
    """S8: every readable-format file in the folder fails to open (a .flac
    that is not actually FLAC data) -- this must fail as 'unreadable', not
    be waved through to needs_artist because nothing could be tagged."""
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    (show / "01.flac").write_bytes(b"not actually flac data")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["all_unreadable"] is True
    status, reason, kind = classify(extracted)
    assert (status, reason) == ("failed", "unreadable")


def test_all_unreadable_detail_is_the_open_error_not_the_reason_word(tmp_path):
    """R2-N2: extract()'s detail for an all-corrupt folder is the actual
    open error read_flac_tags hit, not just the word 'unreadable' repeated
    -- process() (app/utils/bulk_ingest_run.py) uses this for the item's
    stored detail instead of the bare reason."""
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    (show / "01.flac").write_bytes(b"not actually flac data")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["all_unreadable"] is True
    assert extracted["unreadable_detail"]
    assert extracted["unreadable_detail"] != "unreadable"


def test_one_corrupt_file_among_readable_ones_is_not_all_unreadable(tmp_path):
    """A folder with at least one openable readable-format file must not be
    marked all_unreadable, even if a sibling file is corrupt."""
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="A", DATE="1977-05-08",
                    TRACKNUMBER="1")
    (show / "02.flac").write_bytes(b"not actually flac data")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["all_unreadable"] is False
    status, reason, kind = classify(extracted)
    assert status == "ingested"


def test_one_unreadable_file_does_not_break_artist_consistency(tmp_path):
    """N7: a folder with one corrupt/untaggable file among fully tagged
    FLACs must still resolve ARTIST from the files that DID read -- a
    consistency check across every file (including ones that could not be
    read at all) is stricter than intended."""
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="A", DATE="1977-05-08",
                    TRACKNUMBER="1")
    _flac_with_tags(show / "02.flac", ARTIST="A", DATE="1977-05-08",
                    TRACKNUMBER="2")
    (show / "03.flac").write_bytes(b"not actually flac data")
    extracted = extract(str(show), str(root), placement="artist")
    assert extracted["artist"] == "A"
    status, reason, kind = classify(extracted)
    assert status == "ingested"


# ── FORMAT / TYPE pure functions (2026-09-27) ────────────────────────────────
# classify_kind()/folder_format() were factored out of classify() so
# batch_scan() and the Review & Ingest staging payload could compute Type and
# Format without running has_audio/unreadable/artist/date checks that don't
# bear on either. These confirm the factored functions still agree with what
# classify()/extract() have always returned.

def test_classify_kind_matches_classify_for_a_studio_album(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")
    _flac_with_tags(show / "02.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert kind == "studio"
    assert classify_kind(extracted["payload"], extracted["album"], extracted["venue"]) == "studio"


def test_classify_kind_matches_classify_for_a_live_show(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995-07-08",
                     VENUE="Deer Creek")
    extracted = extract(str(show), str(root), placement="artist")
    status, reason, kind = classify(extracted)
    assert kind == "live"
    assert classify_kind(extracted["payload"], extracted["album"], extracted["venue"]) == "live"


def test_folder_format_labels_flac_wav_shn_alphabetically(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="A")
    _wav(show / "02.wav")
    _shn(show / "03.shn")
    from app.utils.ingest import scan_folder
    assert folder_format(scan_folder(str(show))) == "FLAC, SHN, WAV"
