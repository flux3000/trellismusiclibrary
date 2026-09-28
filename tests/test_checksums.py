"""
tests/test_checksums.py — pure-logic coverage for fingerprint file parsing
and checksum matching/verification (app/utils/checksums.py). No FLAC files
needed for parsing/matching; file_md5 uses a small real temp file since
that's just raw bytes, no audio decoding involved.
"""

import hashlib
import os
import tempfile

from app.utils.checksums import (
    parse_checksum_file,
    match_entries_to_tracks,
    verify_track_checksum,
    file_md5,
    flac_md5_signature,
)


# ── parse_checksum_file ──────────────────────────────────────────────────────

def test_parse_md5sum_style():
    content = (
        "d41d8cd98f00b204e9800998ecf8427e *01 - Dark Star.flac\n"
        "5d41402abc4b2a76b9719d911017c592  02 - St. Stephen.flac\n"
    )
    entries = parse_checksum_file(content)
    assert len(entries) == 2
    assert entries[0]["filename"] == "01 - Dark Star.flac"
    assert entries[0]["checksum"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert entries[1]["filename"] == "02 - St. Stephen.flac"


def test_parse_colon_style():
    content = "01 - Dark Star.flac:d41d8cd98f00b204e9800998ecf8427e\n"
    entries = parse_checksum_file(content)
    assert entries == [{"filename": "01 - Dark Star.flac",
                         "rel_path": "01 - Dark Star.flac",
                         "checksum": "d41d8cd98f00b204e9800998ecf8427e"}]


def test_parse_bare_metaflac_output_no_filenames():
    # `metaflac --show-md5sum *.flac > ffp.txt` — just one hex string per line.
    content = "d41d8cd98f00b204e9800998ecf8427e\n5d41402abc4b2a76b9719d911017c592\n"
    entries = parse_checksum_file(content)
    assert entries == [
        {"filename": None, "rel_path": None, "checksum": "d41d8cd98f00b204e9800998ecf8427e"},
        {"filename": None, "rel_path": None, "checksum": "5d41402abc4b2a76b9719d911017c592"},
    ]


def test_parse_skips_blank_and_non_data_lines():
    content = "\n  \nSome header comment with no hash\nd41d8cd98f00b204e9800998ecf8427e *track.flac\n"
    entries = parse_checksum_file(content)
    assert len(entries) == 1


def test_parse_empty_content():
    assert parse_checksum_file("") == []
    assert parse_checksum_file(None) == []


# ── match_entries_to_tracks ──────────────────────────────────────────────────

class _FakeTrack:
    def __init__(self, track_number, file_path):
        self.track_number = track_number
        self.file_path = file_path
    def __repr__(self):
        return f"Track({self.track_number}, {self.file_path})"


def test_match_by_exact_filename():
    tracks = [_FakeTrack(1, "01 - Dark Star.flac"), _FakeTrack(2, "02 - St. Stephen.flac")]
    entries = [
        {"filename": "01 - Dark Star.flac", "checksum": "aaa"},
        {"filename": "02 - St. Stephen.flac", "checksum": "bbb"},
    ]
    matched = match_entries_to_tracks(entries, tracks)
    assert matched[tracks[0]] == "aaa"
    assert matched[tracks[1]] == "bbb"


def test_match_by_stem_across_extensions():
    # fingerprint generated against the original .shn set, tracks are now .flac
    tracks = [_FakeTrack(1, "01 - Dark Star.flac")]
    entries = [{"filename": "01 - Dark Star.shn", "checksum": "aaa"}]
    matched = match_entries_to_tracks(entries, tracks)
    assert matched[tracks[0]] == "aaa"


def test_match_positional_fallback_when_no_filenames():
    tracks = [_FakeTrack(1, "01.flac"), _FakeTrack(2, "02.flac")]
    entries = [{"filename": None, "checksum": "aaa"}, {"filename": None, "checksum": "bbb"}]
    matched = match_entries_to_tracks(entries, tracks)
    assert matched[tracks[0]] == "aaa"
    assert matched[tracks[1]] == "bbb"


def test_no_positional_fallback_when_counts_differ():
    tracks = [_FakeTrack(1, "01.flac"), _FakeTrack(2, "02.flac")]
    entries = [{"filename": None, "checksum": "aaa"}]
    assert match_entries_to_tracks(entries, tracks) == {}


def test_no_guessing_when_filenames_present_but_unmatched():
    tracks = [_FakeTrack(1, "01.flac")]
    entries = [{"filename": "totally different.flac", "checksum": "aaa"}]
    assert match_entries_to_tracks(entries, tracks) == {}


def test_nested_basename_collision_requires_caller_side_scoping():
    """R2-4: an ambiguous basename (two tracks that both happen to be named
    "01.flac", e.g. CD1/01.flac and CD2/01.flac before flattening, presented
    here without their subdir prefix) is left UNMATCHED rather than handed
    to whichever track was listed last -- match_entries_to_tracks refuses to
    guess when a basename could mean more than one track. The caller
    (app.api.ingest._do_confirm step 9) still pre-scopes candidates to one
    fingerprint file's own directory (spec section 3.1 D6/D7) before ever
    calling this function, so a real ingest never reaches this ambiguous
    path at all -- this test proves the fallback itself is safe even when a
    caller doesn't scope."""
    cd1_track = _FakeTrack(1, "01.flac")   # originally CD1/01.flac
    cd2_track = _FakeTrack(2, "01.flac")   # originally CD2/01.flac, same basename
    entries = [{"filename": "01.flac", "checksum": "cd1-hash"}]

    # Unscoped: both tracks share a basename with no rel_path on the entry
    # to disambiguate by -- refused, not guessed.
    matched_unscoped = match_entries_to_tracks(entries, [cd1_track, cd2_track])
    assert matched_unscoped == {}

    # Scoped to CD1's own tracks only (what the caller does when a
    # fingerprint file's rel_path dirname is used to filter candidates
    # before matching) -- unambiguous, and correct.
    matched_scoped = match_entries_to_tracks(entries, [cd1_track])
    assert matched_scoped == {cd1_track: "cd1-hash"}


def test_rootlevel_relpath_disambiguates_colliding_basenames():
    """R2-4: a root-level checksum file that lists per-disc subpaths
    ("CD1/01.flac", "CD2/01.flac") matches each entry to its own track by
    relative path, even though both tracks share the basename "01.flac"."""
    cd1_track = _FakeTrack(1, "CD1/01.flac")
    cd2_track = _FakeTrack(2, "CD2/01.flac")
    entries = [
        {"filename": "01.flac", "rel_path": "CD1/01.flac", "checksum": "cd1-hash"},
        {"filename": "01.flac", "rel_path": "CD2/01.flac", "checksum": "cd2-hash"},
    ]
    matched = match_entries_to_tracks(entries, [cd1_track, cd2_track])
    assert matched == {cd1_track: "cd1-hash", cd2_track: "cd2-hash"}


def test_relpath_match_is_backslash_and_case_tolerant():
    track = _FakeTrack(1, "CD1/01.flac")
    entries = [{"filename": "01.FLAC", "rel_path": "cd1\\01.FLAC", "checksum": "aaa"}]
    matched = match_entries_to_tracks(entries, [track])
    assert matched == {track: "aaa"}


# ── verify_track_checksum ────────────────────────────────────────────────────

def test_verify_md5_match_and_mismatch():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".flac") as f:
        f.write(b"not really flac audio, just bytes for a whole-file md5 test")
        path = f.name
    try:
        real_md5 = hashlib.md5(open(path, "rb").read()).hexdigest()
        assert verify_track_checksum(path, "md5", real_md5) == "match"
        assert verify_track_checksum(path, "md5", "0" * 32) == "mismatch"
        assert verify_track_checksum(path, "md5", None) == "unverified"
    finally:
        os.unlink(path)


def test_verify_unverified_when_file_missing():
    assert verify_track_checksum("/no/such/file.flac", "md5", "d41d8cd98f00b204e9800998ecf8427e") == "unverified"
    assert verify_track_checksum("/no/such/file.flac", "ffp", "d41d8cd98f00b204e9800998ecf8427e") == "unverified"


def test_flac_md5_signature_none_for_non_flac_file():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".flac") as f:
        f.write(b"not a real flac file")
        path = f.name
    try:
        assert flac_md5_signature(path) is None
    finally:
        os.unlink(path)


def test_file_md5_matches_hashlib():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"hello world")
        path = f.name
    try:
        assert file_md5(path) == hashlib.md5(b"hello world").hexdigest()
    finally:
        os.unlink(path)


# ── shntool .st5 filename parsing (2026-08-02) ───────────────────────────────
# Ryan asked whether ST5 support was worth keeping, because the UI reported
# "0 of 18 entries matched" on a file that plainly contained everything needed.
#
# It was never an ST5 problem. shntool writes
#     <hash>  [shntool]  Track 01.flac
# and the parser handed the whole remainder to os.path.basename(), producing
# the filename "[shntool]  Track 01.flac". The hashes parsed fine; nothing
# matched. The library holds 474 .st5 files and ZERO tracks with
# checksum_type='st5' — it had never worked once.
#
# The format itself is sound: on all 40 recordings carrying both an .st5 and
# an .ffp, the hash lists are byte-identical, confirming .st5 really is the
# same MD5-of-decoded-audio that flac_md5_signature() reads from STREAMINFO.

_ST5 = """;shntool st5 checksums generated by flux3000 on 2021-01-19 19:46:51 +0000

bc0fc62e4c6ed4beea1755dd38c0c64a  [shntool]  Track 01.flac
193fc617dd700a8e9d9faa676b43d984  [shntool]  Track 02.flac
"""


def test_st5_shntool_marker_is_stripped_from_the_filename():
    from app.utils.checksums import parse_checksum_file
    entries = parse_checksum_file(_ST5)
    assert [e["filename"] for e in entries] == ["Track 01.flac", "Track 02.flac"]
    assert entries[0]["checksum"] == "bc0fc62e4c6ed4beea1755dd38c0c64a"


def test_st5_comment_header_is_not_read_as_an_entry():
    from app.utils.checksums import parse_checksum_file
    assert len(parse_checksum_file(_ST5)) == 2


def test_brackets_inside_a_real_filename_survive():
    """LEADING markers only. A track genuinely called "Track 01 [live].flac"
    must keep its brackets — position identifies the marker, not the brackets."""
    from app.utils.checksums import parse_checksum_file
    line = "bc0fc62e4c6ed4beea1755dd38c0c64a *Track 01 [live].flac"
    assert parse_checksum_file(line)[0]["filename"] == "Track 01 [live].flac"


def test_other_checksum_formats_are_unaffected():
    from app.utils.checksums import parse_checksum_file
    ffp = "Track 01.flac:bc0fc62e4c6ed4beea1755dd38c0c64a"
    md5 = "bc0fc62e4c6ed4beea1755dd38c0c64a *Track 01.flac"
    sub = "bc0fc62e4c6ed4beea1755dd38c0c64a *CD1/01.flac"
    assert parse_checksum_file(ffp)[0]["filename"] == "Track 01.flac"
    assert parse_checksum_file(md5)[0]["filename"] == "Track 01.flac"
    assert parse_checksum_file(sub)[0]["filename"] == "01.flac"
