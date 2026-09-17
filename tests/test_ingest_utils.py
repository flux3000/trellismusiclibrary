"""
tests/test_ingest_utils.py — move_to_library()'s Move-behavior cleanup
(2026-07-23): once a recording folder is moved into the library, its
immediate parent (the "Artist Name" staging folder in a typical Bulk
Import layout) should be removed too if left empty — but only ONE level up,
only ever for behavior="move" (never "copy"), and never anything that isn't
unambiguously a disposable staging folder. Pure filesystem logic, no DB/app
context needed.
"""

from pathlib import Path

from app.utils.ingest import move_to_library


def _make_show(parent, show_name="1994-07-30 Show", filename="track.flac"):
    show = parent / show_name
    show.mkdir(parents=True)
    (show / filename).write_bytes(b"x" * 100)
    return show


def test_move_deletes_empty_parent_staging_folder(tmp_path):
    """The common Bulk Import case: Import/Artist Name/Show Folder/ — once
    the Show Folder is moved (its only content), the now-empty "Artist
    Name" folder should go too. Its own parent (Import) is left alone —
    cleanup is ONE level only, not a climb toward the root."""
    import_dir = tmp_path / "Import"
    artist_dir = import_dir / "Artist Name"
    show = _make_show(artist_dir)
    lib = tmp_path / "lib"; lib.mkdir()

    move_to_library(str(show), str(lib), "Artist Name", "1994-07-30 Show",
                     behavior="move")

    assert not show.exists()
    assert not artist_dir.exists()
    assert import_dir.exists()   # one level only — not also removed


def test_move_keeps_nonempty_parent(tmp_path):
    """A sibling show folder still under "Artist Name" means it's not
    empty — must survive."""
    artist_dir = tmp_path / "Artist Name"
    show1 = _make_show(artist_dir, "Show 1")
    _make_show(artist_dir, "Show 2")
    lib = tmp_path / "lib"; lib.mkdir()

    move_to_library(str(show1), str(lib), "Artist Name", "Show 1", behavior="move")

    assert not show1.exists()
    assert artist_dir.exists()
    assert (artist_dir / "Show 2").exists()


def test_move_ignores_ds_store_when_checking_empty(tmp_path):
    """A folder Finder has visited almost always has a stray .DS_Store —
    that alone shouldn't block cleanup, and the junk file itself should be
    removed along with the folder."""
    artist_dir = tmp_path / "Artist Name"
    show = _make_show(artist_dir)
    (artist_dir / ".DS_Store").write_bytes(b"junk")
    lib = tmp_path / "lib"; lib.mkdir()

    move_to_library(str(show), str(lib), "Artist Name", "1994-07-30 Show",
                     behavior="move")

    assert not artist_dir.exists()


def test_copy_never_touches_source(tmp_path):
    """behavior="copy" must never remove the source show folder OR its
    parent, regardless of emptiness — copy's whole contract is "source stays
    untouched."""
    artist_dir = tmp_path / "Artist Name"
    show = _make_show(artist_dir)
    lib = tmp_path / "lib"; lib.mkdir()

    move_to_library(str(show), str(lib), "Artist Name", "1994-07-30 Show",
                     behavior="copy")

    assert show.exists()
    assert (show / "track.flac").exists()
    assert artist_dir.exists()


def test_move_never_deletes_protected_dir_name(tmp_path):
    """Even if a staging show folder's immediate parent happens to be named
    like a standard macOS user folder (Desktop, Downloads, ...), it must
    never be auto-deleted just because it's empty afterward — this cleanup
    is for disposable staging folders, not general-purpose ones."""
    desktop = tmp_path / "Desktop"
    show = _make_show(desktop)
    lib = tmp_path / "lib"; lib.mkdir()

    move_to_library(str(show), str(lib), "Desktop", "1994-07-30 Show", behavior="move")

    assert not show.exists()
    assert desktop.exists()   # protected by name, even though now empty


def test_move_never_deletes_home_directory(tmp_path, monkeypatch):
    """If the show folder's parent happens to resolve to the user's home
    directory (e.g. a show folder dropped directly in $HOME), that must
    never be removed even if briefly empty."""
    home = tmp_path / "home_stand_in"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    show = _make_show(home)
    lib = tmp_path / "lib"; lib.mkdir()

    move_to_library(str(show), str(lib), "whoever", "1994-07-30 Show", behavior="move")

    assert not show.exists()
    assert home.exists()


# ── Fingerprint detection by CONTENT, not filename (2026-08-02) ───────────────
# Ryan's Aoife O'Donovan folder held two .txt files: the real info file
# (`Aoife O'Donovan Band.txt`) and an md5 list named after the show
# (`AoifeODonovan2012-08-19_MCE400_16bit.txt`). Nothing in the latter's NAME
# matches FINGERPRINT_MARKERS, so it was filed as an info-file candidate — and
# won the scoring, because _score_text_file awards +5 for a date pattern in the
# filename and the genuine info file scored 0. A 32-hex hash landed in the
# Venue box, and the checksums were never registered for verification.

def _md5_list(n=14):
    return "\n".join(
        "%032x *AoifeODonovan2012-08-19_MCE400_24bit%02d.flac" % (i * 7777777, i)
        for i in range(1, n + 1))


_INFO_TEXT = """Aoife O'Donovan Band
Club Passim, Cambridge, MA
2012-08-19

Source: SBD > DAT
Lineage: DAT > CDR > EAC > FLAC

01 Intro
02 Red and White Blue and Gold
03 Pearls
04 Bridal Rose
"""


def test_checksum_list_named_after_the_show_is_not_an_info_file(tmp_path):
    """THE bug. Both files are .txt; only one is prose."""
    from app.utils.ingest import scan_folder

    show = tmp_path / "Aoife O'Donovan"
    show.mkdir()
    (show / "track01.flac").write_bytes(b"x" * 100)
    (show / "AoifeODonovan2012-08-19_MCE400_16bit.txt").write_text(_md5_list())
    (show / "Aoife O'Donovan Band.txt").write_text(_INFO_TEXT)

    scan = scan_folder(str(show))
    names = [t["filename"] for t in scan["text_files"]]
    assert names == ["Aoife O'Donovan Band.txt"], \
        "the checksum list must not be offered as an info-file candidate"
    # ...and the flip side: it is now registered for verification, which it
    # never was before.
    fps = {f["filename"]: f["type"] for f in scan["fingerprints"]}
    assert fps == {"AoifeODonovan2012-08-19_MCE400_16bit.txt": "md5"}


def test_ffp_shape_is_detected_and_typed(tmp_path):
    """ffp puts the hash last; md5sum puts it first. Both are content-detected."""
    from app.utils.ingest import fingerprint_type_for_file

    p = tmp_path / "gd1977-05-08.txt"
    p.write_text("\n".join("gd77-05-08d1t%02d.flac:%032x" % (i, i * 33333)
                           for i in range(1, 13)))
    assert fingerprint_type_for_file(str(p)) == "ffp"

    q = tmp_path / "somename.txt"
    q.write_text(_md5_list())
    assert fingerprint_type_for_file(str(q)) == "md5"


def test_prose_info_file_is_never_mistaken_for_a_checksum_list(tmp_path):
    """The guard that matters: an info file quoting a few hashes stays an info
    file. A real checksum list is essentially nothing BUT hashes, so the ratio
    test separates them without needing to understand either format."""
    from app.utils.ingest import fingerprint_type_for_file

    plain = tmp_path / "info.txt"
    plain.write_text(_INFO_TEXT)
    assert fingerprint_type_for_file(str(plain)) is None

    mixed = tmp_path / "notes.txt"
    mixed.write_text(_INFO_TEXT + "\n" + "\n".join(
        "%032x *t%02d.flac" % (i * 99991, i) for i in range(1, 4)))
    assert fingerprint_type_for_file(str(mixed)) is None


def test_filename_marker_still_wins_without_reading_the_file(tmp_path):
    """An explicitly-named file is classified on its name — st5 in particular
    can only ever be identified that way, since it is byte-identical to ffp."""
    from app.utils.ingest import fingerprint_type_for_file

    p = tmp_path / "checksum.st5"
    p.write_text("whatever")
    assert fingerprint_type_for_file(str(p)) == "st5"


# ── Pre-ingest fingerprint audit (2026-08-02) ─────────────────────────────────
# The third triage tab. A checksum is the only HARD evidence on the card —
# everything else is an estimate. Verifying before ingest is the point at which
# a damaged tape is cheapest to reject.

def _flac_stub(path, name):
    """A file that exists so verify_track_checksum can attempt (and fail) it."""
    (path / name).write_bytes(b"not really flac")


def test_audit_reports_no_fingerprint_files(tmp_path):
    from app.utils.checksums import audit_folder_fingerprints
    show = tmp_path / "show"; show.mkdir()
    _flac_stub(show, "01.flac")
    a = audit_folder_fingerprints(str(show), [{"filename": "01.flac",
                                               "rel_path": "01.flac",
                                               "path": str(show / "01.flac"),
                                               "index": 1}])
    assert a["verdict"] == "none" and a["files"] == []


def test_md5_is_parsed_and_matched_but_left_pending_when_not_deep(tmp_path):
    """Ryan's call: FFP/ST5 verify for free at triage, MD5 waits for a click.
    'Pending' must be distinguishable from 'unmatched' — one means we have not
    looked yet, the other means there is no checksum for that file at all."""
    from app.utils.checksums import audit_folder_fingerprints

    show = tmp_path / "show"; show.mkdir()
    for n in ("01.flac", "02.flac"):
        _flac_stub(show, n)
    (show / "show.md5").write_text(
        "%032x *01.flac\n%032x *02.flac\n" % (1, 2))

    audio = [{"filename": n, "rel_path": n, "path": str(show / n), "index": i}
             for i, n in enumerate(("01.flac", "02.flac"), 1)]

    shallow = audit_folder_fingerprints(str(show), audio, deep=False)
    assert shallow["files"][0]["type"] == "md5"
    assert shallow["files"][0]["matched_count"] == 2
    assert shallow["files"][0]["verified"] is False
    assert shallow["summary"]["pending_deep"] == 2
    assert shallow["summary"]["mismatch"] == 0
    assert shallow["verdict"] == "unverified"

    # Deep pass actually hashes — and these stub files do not match, which is
    # the point: a wrong checksum must surface as a mismatch, not be swallowed.
    deep = audit_folder_fingerprints(str(show), audio, deep=True)
    assert deep["summary"]["mismatch"] == 2
    assert deep["verdict"] == "mismatch"


def test_audit_flags_checksum_entries_with_no_matching_audio(tmp_path):
    """A checksum list longer than the folder means tracks are missing. Worth
    saying out loud — it is the cheapest possible detection of an incomplete
    download."""
    from app.utils.checksums import audit_folder_fingerprints

    show = tmp_path / "show"; show.mkdir()
    _flac_stub(show, "01.flac")
    (show / "show.md5").write_text(
        "%032x *01.flac\n%032x *02.flac\n%032x *03.flac\n" % (1, 2, 3))

    a = audit_folder_fingerprints(str(show), [
        {"filename": "01.flac", "rel_path": "01.flac",
         "path": str(show / "01.flac"), "index": 1}])
    assert a["files"][0]["orphan_entries"] == 2


def test_audit_scopes_a_disc_subdir_fingerprint_to_that_disc(tmp_path):
    """Same rule _do_confirm applies. Bare names like '01.flac' collide across
    discs once audio is flattened, so a CD1 checksum must never be handed to a
    CD2 track."""
    from app.utils.checksums import audit_folder_fingerprints

    show = tmp_path / "show"; show.mkdir()
    for disc in ("CD1", "CD2"):
        (show / disc).mkdir()
        _flac_stub(show / disc, "01.flac")
    (show / "CD1" / "cd1.md5").write_text("%032x *01.flac\n" % 1)

    audio = [{"filename": "01.flac", "rel_path": f"{d}/01.flac",
              "path": str(show / d / "01.flac"), "index": i}
             for i, d in enumerate(("CD1", "CD2"), 1)]

    a = audit_folder_fingerprints(str(show), audio, deep=False)
    f = a["files"][0]
    # Only CD1's track is a candidate, so exactly one row comes back.
    assert len(f["tracks"]) == 1
    assert f["matched_count"] == 1


# ── Collision handling at ingest time (2026-09-01) ────────────────────────────
# move_to_library() used to do dest_folder.mkdir(parents=True, exist_ok=True)
# unconditionally — if a folder of the same canonical name already existed
# under the artist directory (e.g. two undated-source "Various Artists" shows
# at the same venue), that mkdir silently reused it and both recordings'
# files landed in one folder. It must now dedupe the same way
# rename_recording_folder() always has.

def test_move_dedupes_against_an_existing_same_named_folder(tmp_path):
    """The Ryman Auditorium 1964 bug: a second recording with an identical
    canonical folder name must land in its own "(2)" folder, never merge
    into the first one's."""
    lib = tmp_path / "lib"; lib.mkdir()
    artist_dir = lib / "Various Artists"
    existing = artist_dir / "Various Artists - 1964 - Ryman Auditorium - Nashville, TN"
    existing.mkdir(parents=True)
    (existing / "existing_track.flac").write_bytes(b"x" * 10)

    show = _make_show(
        tmp_path / "Import", "Various Artists - 1964 - Ryman Auditorium - Nashville, TN",
        filename="new_track.flac")

    new_rel = move_to_library(
        str(show), str(lib), "Various Artists",
        "Various Artists - 1964 - Ryman Auditorium - Nashville, TN",
        behavior="move")

    assert new_rel == "Various Artists/Various Artists - 1964 - Ryman Auditorium - Nashville, TN (2)"
    # The first recording's folder and its file are untouched...
    assert (existing / "existing_track.flac").exists()
    # ...and the second recording's file is in its own new folder, not mixed in.
    new_folder = lib / new_rel
    assert (new_folder / "new_track.flac").exists()
    assert not (existing / "new_track.flac").exists()


def test_move_does_not_dedupe_a_genuinely_new_folder_name(tmp_path):
    """The common case — nothing on disk yet — must still get the plain
    canonical name, no spurious "(2)"."""
    lib = tmp_path / "lib"; lib.mkdir()
    show = _make_show(tmp_path / "Import", "1994-07-30 Show")

    new_rel = move_to_library(str(show), str(lib), "Artist Name",
                              "1994-07-30 Show", behavior="move")

    assert new_rel == "Artist Name/1994-07-30 Show"


def test_move_dedupes_a_third_collision_past_the_second(tmp_path):
    """Two prior collisions already on disk ("(2)" taken too) must roll to
    "(3)", not clobber "(2)" or retry the bare name forever."""
    lib = tmp_path / "lib"; lib.mkdir()
    artist_dir = lib / "Various Artists"
    (artist_dir / "Show").mkdir(parents=True)
    (artist_dir / "Show (2)").mkdir(parents=True)

    show = _make_show(tmp_path / "Import", "Show")
    new_rel = move_to_library(str(show), str(lib), "Various Artists", "Show",
                              behavior="move")

    assert new_rel == "Various Artists/Show (3)"


# ── Show resolution in a library Trellis did not lay out (2026-09-17) ────────
#
# A collector who points Trellis at their OWN folder gets two kinds of
# directory mixed in with their shows: Trellis's underscore-prefixed buckets
# (_musicians/, _venues/, _events/) and, in a flat library, a plain
# <Artist>/ folder whose only content is the _images/ directory that
# app/api/artists.py writes artist photos into. Before this, every one of
# those came back from resolve_shows_in_dir() as a show with no audio and
# graded red — one bogus review row per photographed artist, on every
# rescan, in a library with 72 of them.

def _flat_library(tmp_path):
    """A collector's flat library: shows and Trellis furniture side by side."""
    def mk(rel, files=()):
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_bytes(b"x")
        return d

    mk("Grateful Dead 1977-05-08 Barton Hall", ["01.flac", "02.flac"])
    mk("Bill Evans 1968-11-02 Ronnie Scotts.sbd", ["t01.flac"])
    mk("Grateful Dead/_images", ["photo.jpg"])          # artist photos
    mk("_venues/Barton Hall/_images", ["v.jpg"])        # bucket
    mk("_musicians/Jerry Garcia/_images", ["m.jpg"])    # bucket
    mk(".sync_metadata", ["junk.txt"])                  # dot-prefixed
    mk("Notes", ["readme.txt"])                         # collector's own
    return mk


def test_resolve_skips_buckets_photo_dirs_and_audioless_folders(tmp_path):
    from app.utils.ingest import resolve_shows_in_dir

    _flat_library(tmp_path)
    skipped = []
    got = sorted(Path(p).relative_to(tmp_path).as_posix()
                 for p in resolve_shows_in_dir(str(tmp_path), skipped=skipped))

    assert got == ["Bill Evans 1968-11-02 Ronnie Scotts.sbd",
                   "Grateful Dead 1977-05-08 Barton Hall"]

    # Audio-less folders are REPORTABLE, so an adoption summary can say where
    # they went. Trellis's own buckets are not — they are furniture, not the
    # collector's missing material.
    assert sorted(Path(p).relative_to(tmp_path).as_posix() for p in skipped) \
        == ["Grateful Dead", "Notes"]


def test_resolve_still_reports_a_folder_the_user_named(tmp_path):
    """The contract resolve_shows() keeps and resolve_shows_in_dir() drops.

    Returning nothing for a folder the user explicitly pointed at reads as
    the app being broken; the scanner has to be able to grade it red and say
    "no audio here". A folder merely encountered while walking was never
    asked about and is simply not a show.
    """
    from app.utils.ingest import resolve_shows

    empty = tmp_path / "Empty Folder"
    empty.mkdir()

    assert resolve_shows(str(empty)) == [str(empty)]
    assert resolve_shows(str(empty), include_empty=False) == []


def test_originals_folder_does_not_split_or_shadow_its_show(tmp_path):
    """_originals/ holds the pre-conversion WAV/SHN files after an SHN->FLAC
    triage conversion. It must never read as a disc subdir or a second show."""
    from app.utils.ingest import resolve_shows

    show = tmp_path / "Miles 1970-03-07"
    (show / "_originals").mkdir(parents=True)
    (show / "01.flac").write_bytes(b"x")
    (show / "_originals" / "01.shn").write_bytes(b"x")

    assert resolve_shows(str(show)) == [str(show)]


def test_multi_disc_and_deep_nesting_survive_the_exclusions(tmp_path):
    """Negative control: the new rules must not eat anything that was working.

    A CD1/CD2 show is still ONE show, and artist -> year -> show nesting still
    resolves to the show. A checker that only ever removes things is easy to
    get wrong in the direction of removing too much.
    """
    from app.utils.ingest import resolve_shows_in_dir

    for disc in ("CD1", "CD2"):
        d = tmp_path / "Phish 1997-11-17" / disc
        d.mkdir(parents=True)
        (d / "01.flac").write_bytes(b"x")
    deep = tmp_path / "Allman Bros" / "1971" / "Fillmore East"
    deep.mkdir(parents=True)
    (deep / "01.flac").write_bytes(b"x")

    got = sorted(Path(p).relative_to(tmp_path).as_posix()
                 for p in resolve_shows_in_dir(str(tmp_path)))
    assert got == ["Allman Bros/1971/Fillmore East", "Phish 1997-11-17"]


def test_excluded_dir_predicate_has_a_negative_control(tmp_path):
    """A predicate that never fires, or fires on everything, is worse than none."""
    from app.utils.ingest import _is_excluded_dir

    for ordinary in ("Grateful Dead", "CD1", "Art", "audio", "Notes", "1971"):
        assert not _is_excluded_dir(ordinary), ordinary
    for furniture in ("_images", "_venues", "_musicians", "_events",
                      "_originals", ".git", ".DS_Store"):
        assert _is_excluded_dir(furniture), furniture


# ── Library layout: the artist folder is a convention, not a law (2026-09-17) ─
#
# A collector who pointed Trellis at a library they built themselves may keep
# every show at one flat level with the artist in the folder name. Filing new
# material under an <Artist>/ directory there leaves them with a permanently
# hybrid tree, so move_to_library() takes the layout as a parameter. It is read
# from node_setting by the caller, because this function has no app context.

def test_new_recordings_file_under_an_artist_folder_by_default(tmp_path):
    """The default must not change: every library Trellis laid out itself
    already looks like this, and a new keyword argument appearing is not a
    reason for an existing install to change shape."""
    lib = tmp_path / "Library"
    lib.mkdir()
    show = _make_show(tmp_path / "Import", "Show")

    rel = move_to_library(str(show), str(lib), "Grateful Dead",
                          "Grateful Dead - 1977-05-08", behavior="copy")

    assert rel == "Grateful Dead/Grateful Dead - 1977-05-08"
    assert (lib / rel / "track.flac").is_file()


def test_layout_off_files_flat_at_the_library_root(tmp_path):
    lib = tmp_path / "Library"
    lib.mkdir()
    show = _make_show(tmp_path / "Import", "Show")

    rel = move_to_library(str(show), str(lib), "Grateful Dead",
                          "Grateful Dead - 1977-05-08", behavior="copy",
                          under_artist_folder=False)

    assert rel == "Grateful Dead - 1977-05-08"
    assert "/" not in rel, "flat layout must produce a single path segment"
    assert (lib / rel / "track.flac").is_file()
    assert not (lib / "Grateful Dead").exists()


def test_flat_layout_dedupes_against_the_root(tmp_path):
    """Two shows with the same canonical name collide at the ROOT when flat,
    not on one artist's shelf — so the same "(2)" convention has to apply
    there. This is the SBD-vs-AUD case, real in Ryan's library."""
    lib = tmp_path / "Library"
    lib.mkdir()
    name = "Grateful Dead - 1977-05-08"

    first = move_to_library(str(_make_show(tmp_path / "A", "S")), str(lib),
                            "Grateful Dead", name, behavior="copy",
                            under_artist_folder=False)
    second = move_to_library(str(_make_show(tmp_path / "B", "S")), str(lib),
                             "Grateful Dead", name, behavior="copy",
                             under_artist_folder=False)

    assert first == name
    assert second == f"{name} (2)"
    assert (lib / second / "track.flac").is_file()
