"""
tests/test_review_fixes.py -- regression tests from the independent review of
the file-handling build (review.md, 2026-09-25).

The review's root-cause finding: chunk-3/6 tests hand-built dicts around
scan_folder()/_do_confirm() rather than driving the real path, so a payload
builder that silently dropped fields (B1, S2) or an engine that invented a
" (2)" suffix (B2) went uncaught. These tests instead go
scan_folder() -> build_scan_payload() -> a real HTTP confirm through the
Flask test client -> rows in the DB, on a real two-disc folder with tiny
valid FLACs.
"""

import hashlib
import os
import time

import numpy as np
import pytest
import soundfile as sf

from app.extensions import db as _db
from app.models.user import User
from app.models.recording import Recording
from app.utils import node_settings
from app.utils.ingest import build_scan_payload
from app.utils.file_naming import rename_plan


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


def _silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")


def _file_md5(path):
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


@pytest.fixture()
def client(app):
    return app.test_client()


def _make_two_disc_source(tmp_path):
    """
    CD1/01.flac, CD1/checksum.md5, CD2/01.flac, CD2/checksum.md5 -- a
    two-disc source under a folder name that carries a source tag
    ('schoeps') and an etree shnid (118671), per spec section 5's
    detectors.
    """
    src = tmp_path / "src" / "gd1988-05-01.schoeps.118671"
    (src / "CD1").mkdir(parents=True)
    (src / "CD2").mkdir(parents=True)
    _silent_flac(src / "CD1" / "01.flac")
    _silent_flac(src / "CD2" / "01.flac")

    original_bytes = {
        "CD1/01.flac": (src / "CD1" / "01.flac").read_bytes(),
        "CD2/01.flac": (src / "CD2" / "01.flac").read_bytes(),
    }
    (src / "CD1" / "checksum.md5").write_text(
        "{0} *01.flac\n".format(_file_md5(src / "CD1" / "01.flac")))
    (src / "CD2" / "checksum.md5").write_text(
        "{0} *01.flac\n".format(_file_md5(src / "CD2" / "01.flac")))
    return src, original_bytes


def _tracks_from_payload(payload):
    """
    What the FIXED buildIngestTracks (app.js) sends for a source with no FLAC
    tags and no info file: one entry per scanned audio file, carrying every
    field build_scan_payload's audio_files now exports (B1/S2 fix) -- this is
    the real payload shape, not a hand-built dict.
    """
    return [
        {
            "track_number":      af["index"],
            "title":             "Track {0}".format(af["index"]),
            "filename":          af["rel_path"],
            "set_number":        af.get("set_number"),
            "disc_number":       af.get("disc_number"),
            "disc_track_number": af.get("disc_track_number"),
        }
        for af in payload["audio_files"]
    ]


def _poll_confirm(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get("/api/ingest/confirm/{0}".format(job_id))
        body = resp.get_json()
        if body.get("status") != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("confirm job {0} did not finish within {1}s".format(job_id, timeout))


def test_real_ingest_path_carries_disc_source_tag_shnid_and_fp_rel_path(app, client, tmp_path):
    """
    B1 + S2 + S7, driven end to end: scan_folder() -> build_scan_payload()
    -> POST /api/ingest/confirm (real Flask route, real background job) ->
    rows in the DB. Keep mode (the default): nesting and names preserved,
    every original file still exists, byte-identical, at its same relative
    path inside the library folder.
    """
    _login_as(client)
    app.config["LIBRARY_ROOT"] = str(tmp_path / "lib")
    os.makedirs(app.config["LIBRARY_ROOT"], exist_ok=True)

    src, original_bytes = _make_two_disc_source(tmp_path)

    # -- scan_folder() -> build_scan_payload() --------------------------------
    payload = build_scan_payload(str(src))
    assert payload is not None

    # The folder-name detectors (spec section 5) that feed the Add Recording
    # form fields (S7) -- proven here at the payload level, same values the
    # real JS forwards.
    info = payload["suggestions"]["from_info_file"]
    assert info["source_tag"] == "schoeps"
    assert info["etree_shnid"] == 118671

    # B1: disc_number/disc_track_number must survive into the payload.
    by_rel = {af["rel_path"]: af for af in payload["audio_files"]}
    assert by_rel["CD1/01.flac"]["disc_number"] == 1
    assert by_rel["CD1/01.flac"]["disc_track_number"] == 1
    assert by_rel["CD2/01.flac"]["disc_number"] == 2
    assert by_rel["CD2/01.flac"]["disc_track_number"] == 1

    # S2: fingerprint entries must carry rel_path (nested per-disc files
    # share the bare filename "checksum.md5" -- rel_path is what tells them
    # apart).
    fp_rel_paths = sorted(fp["rel_path"] for fp in payload["fingerprints"])
    assert fp_rel_paths == ["CD1/checksum.md5", "CD2/checksum.md5"]

    tracks = _tracks_from_payload(payload)

    confirm_body = {
        "source_folder_path": str(src),
        "artist_name":        "Review Fix Test Artist",
        "start_year": 1988, "start_month": 5, "start_day": 1,
        "source":      "AUD",
        "source_tag":  info["source_tag"],
        "etree_shnid": info["etree_shnid"],
        "is_complete": True,
        "skip_analysis": True,
        "fingerprints": payload["fingerprints"],
        "info_file_content": payload.get("info_file_content"),
        "tracks": tracks,
    }

    # -- The confirm request, through the real Flask test client -------------
    resp = client.post("/api/ingest/confirm", json=confirm_body)
    assert resp.status_code == 202, resp.get_data(as_text=True)
    job_id = resp.get_json()["job_id"]

    result_body = _poll_confirm(client, job_id)
    assert result_body["status"] == "done", result_body
    recording_id = result_body["result"]["recording_id"]

    rec = _db.session.get(Recording, recording_id)
    assert rec.source_tag == "schoeps"
    assert rec.etree_shnid == 118671

    by_orig = {t.original_file_path: t for t in rec.tracks}
    t1 = by_orig["CD1/01.flac"]
    t2 = by_orig["CD2/01.flac"]

    assert t1.disc_number == 1 and t1.disc_track_number == 1
    assert t2.disc_number == 2 and t2.disc_track_number == 1
    assert t1.set_number is None and t2.set_number is None

    # Fingerprint rows: rel_path preserved, and scoped-matching (S2) actually
    # matched each disc's own track against its own checksum.md5 -- stored,
    # never recomputed-and-compared at confirm (MD5 never automatic,
    # Ryan 2026-09-27), so both are left unverified.
    fps = {fp.rel_path: fp for fp in rec.fingerprints}
    assert set(fps) == {"CD1/checksum.md5", "CD2/checksum.md5"}
    for fp in fps.values():
        assert fp.content   # not None -- the confirm-time re-read found it

    for t in (t1, t2):
        assert t.checksum_status == "unverified", (t.original_file_path, t.checksum_status)

    # Keep mode (default): file_path == the original rel_path, nesting kept,
    # and the bytes at that path inside the library are byte-identical to
    # what was on disk before ingest moved them.
    library_root = app.config["LIBRARY_ROOT"]
    for orig_rel, t in (("CD1/01.flac", t1), ("CD2/01.flac", t2)):
        assert t.file_path == orig_rel
        final_abs = os.path.join(library_root, rec.folder_path, t.file_path)
        assert os.path.isfile(final_abs)
        assert open(final_abs, "rb").read() == original_bytes[orig_rel]


def test_keep_mode_rename_files_idle_on_nested_two_disc_recording(app, tmp_path):
    """
    B2, exactly as the reviewer described it: a keep-mode nested two-disc
    recording (CD1/01.flac, CD2/01.flac -- same basename, different discs)
    under the default 'Keep original' scheme must never look staged for
    Rename Files, and the engine's own plan must be empty.
    """
    from app.models.performance import Performance
    from app.models.artist import Artist
    from app.models.track import Track

    node_settings.apply_mode("keep")

    artist = _db.session.query(Artist).first()
    perf = Performance(artist_id=artist.id, start_year=1978, start_month=9, start_day=19)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, folder_path="gd1978-09-19")
    _db.session.add(rec)
    _db.session.flush()
    t1 = Track(recording_id=rec.id, track_number=1, title="One",
               file_path="CD1/01.flac", disc_number=1, disc_track_number=1)
    t2 = Track(recording_id=rec.id, track_number=2, title="Two",
               file_path="CD2/01.flac", disc_number=2, disc_track_number=1)
    _db.session.add_all([t1, t2])
    _db.session.commit()

    fh = node_settings.get_file_handling()
    assert fh["naming_scheme"] == "original"

    plan = rename_plan(rec, fh["naming_scheme"], fh["naming_template"] or None)
    # Every proposed path must equal its current path -- nothing to rename.
    assert [(cur, new) for _t, cur, new in plan if cur != new] == []

    # Same computation the GET /api/recordings/<id> route makes for
    # files_staged (B2's fix: compared against the full rel path, not a
    # basename).
    files_staged = sum(1 for _t, cur, new in plan if new != cur)
    assert files_staged == 0


# ── S3: leading date in a disc-carrier filename must not read as the track
# number -- ported from the reviewer's throwaway probe (/tmp/review_tests).

def test_leading_date_filename_does_not_give_a_bogus_disc_track_number(tmp_path):
    from app.utils.ingest import scan_folder

    src = tmp_path / "show"
    for d in ("CD1", "CD2"):
        for n in ("01", "02"):
            p = src / d / "1977-05-08 - {0} - Title.flac".format(n)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")

    scan = scan_folder(str(src))
    nums = [a["disc_track_number"] for a in scan["audio_files"]]
    # 1-based counter order within each disc, not the leading "1977" parsed
    # off the date -- every file in each disc agreed on nothing, so the
    # whole disc falls back to counter order.
    assert nums == [1, 2, 1, 2], nums


# ── S1: keep mode must not rename a source folder onto itself when the
# canonical destination IS the source (placement=root, or the artist-folder
# equivalent) -- ported from the reviewer's throwaway probe.

def test_keep_mode_source_already_at_destination_is_not_suffixed(app, db, tmp_path):
    from app.api.ingest import _do_confirm
    from app.utils import node_settings

    lib = tmp_path / "lib"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    node_settings.apply_mode("keep")
    node_settings.set_file_handling(placement="root")

    src = lib / "gd1978-09-19.aud.schoeps"
    (src).mkdir(parents=True)
    (src / "01.flac").write_bytes(b"a" * 10)
    (src / "02.flac").write_bytes(b"b" * 10)

    uid = db.session.query(User).first().id
    data = {
        "source_folder_path": str(src), "artist_name": "Grateful Dead",
        "start_year": 1978, "start_month": 9, "start_day": 19, "source": "AUD",
        "tracks": [{"track_number": 1, "title": "A", "filename": "01.flac"},
                   {"track_number": 2, "title": "B", "filename": "02.flac"}],
        "fingerprints": [],
    }
    result = _do_confirm(data, uid, None)
    rec = db.session.get(Recording, result["recording_id"])

    # Must keep its OWN name -- not " (2)" from colliding with itself.
    assert rec.folder_path == "gd1978-09-19.aud.schoeps", rec.folder_path
    assert (src / "01.flac").exists()
    assert (src / "01.flac").read_bytes() == b"a" * 10


# ── B2, pure-object version (no DB) -- matches tests/test_file_naming.py's
# own helpers, ported from the reviewer's throwaway probe.

def test_original_scheme_on_nested_keep_recording_keeps_each_discs_own_path():
    from types import SimpleNamespace as O

    perf = O(start_year=1978, start_month=9, start_day=19,
             artist=O(name="GD", abbreviation=None), venue=None,
             city=None, state=None, country=None)
    tracks = [
        O(track_number=1, title="A", file_path="CD1/01.flac",
          original_file_path="CD1/01.flac", disc_number=1,
          disc_track_number=1, set_number=None),
        O(track_number=2, title="B", file_path="CD2/01.flac",
          original_file_path="CD2/01.flac", disc_number=2,
          disc_track_number=1, set_number=None),
    ]
    rec = O(source=None, source_tag=None, etree_shnid=None,
            performance=perf, tracks=tracks)
    plan = rename_plan(rec, "original")
    proposed = [p for _, _, p in plan]
    # Each disc's own nesting is kept -- no invented " (2)" for two discs
    # that each have their own "01.flac".
    assert proposed == ["CD1/01.flac", "CD2/01.flac"], proposed


# ── R2 (re-review, 2026-09-25), superseded 2026-09-26 by the Bulk Ingest
# in-root fix (spec section 1.1, chunk 1): move_to_library's same-place
# branch used to still apply the rename map/flatten in place under organize
# mode. That branch is gone -- an in-root source (which this re-add case
# always is, since its "own canonical destination" sits under LIBRARY_ROOT)
# is now ingested with no renaming at all, in every mode. See
# tests/test_db_logic.py's test_do_confirm_ingests_nested_in_root_source_in_organize_mode
# for the current behavior this test used to check the opposite of.

def test_organize_mode_reingest_in_place_keeps_original_names(app, db, tmp_path):
    from app.api.ingest import _do_confirm
    from app.utils.folder_naming import build_folder_name
    from app.utils import node_settings

    lib = tmp_path / "lib"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    node_settings.apply_mode("organize")
    node_settings.set_file_handling(placement="artist")

    artist_name = "Review Fix R2 Artist"
    folder_name = build_folder_name(
        artist_name=artist_name, start_year=1978, start_month=9, start_day=19,
        venue_name=None, city=None, state=None, country=None, source="AUD",
    )
    # The source ALREADY sits at its own canonical destination -- the S1 case
    # (a show re-added from the library picker that lands exactly where it
    # already is). It is therefore an in-root source, and the Bulk Ingest
    # fix (2026-09-26) means it is ingested with no rename at all, organize
    # mode notwithstanding.
    src = lib / artist_name / folder_name
    src.mkdir(parents=True)
    (src / "01.flac").write_bytes(b"a" * 10)
    (src / "02.flac").write_bytes(b"b" * 10)

    uid = db.session.query(User).first().id
    data = {
        "source_folder_path": str(src), "artist_name": artist_name,
        "start_year": 1978, "start_month": 9, "start_day": 19, "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "filename": "01.flac"},
                   {"track_number": 2, "title": "Two", "filename": "02.flac"}],
        "fingerprints": [], "skip_analysis": True,
    }
    result = _do_confirm(data, uid, None)
    rec = db.session.get(Recording, result["recording_id"])

    lib_folder = lib / rec.folder_path
    on_disk = sorted(p.name for p in lib_folder.iterdir())
    for t in rec.tracks:
        assert (lib_folder / t.file_path).is_file(), (
            "Track.file_path {0!r} not on disk -- dir has {1}".format(t.file_path, on_disk))
    # In-root bulk_ingest overrides organize's rename scheme -- the files keep
    # their original bare names, never "01 - One.flac"/"02 - Two.flac".
    assert sorted(t.file_path for t in rec.tracks) == ["01.flac", "02.flac"]


# ── R1 (re-review, 2026-09-25): a non-flattening custom template on a
# nested CD1/CD2 source must not double the disc directory.

def test_custom_template_no_position_token_does_not_double_the_disc_dir():
    from app.utils.ingest import compute_audio_rename_map, resolve_ingest_file_path
    from app.utils.file_naming import flattens

    tracks = [
        {"track_number": 1, "title": "Dark Star", "filename": "CD1/01.flac"},
        {"track_number": 2, "title": "Eyes", "filename": "CD2/01.flac"},
    ]
    rename_map = compute_audio_rename_map(tracks, "custom", "{title}")
    flatten = flattens("custom", "{title}")
    assert flatten is False   # no position token -- keep mode's nesting rule applies

    out = [resolve_ingest_file_path(t["filename"], rename_map, flatten) for t in tracks]
    assert out == ["CD1/Dark Star.flac", "CD2/Eyes.flac"], (rename_map, out)


# ── R3 (re-review, 2026-09-25): a non-numeric shnid must be rejected before
# any file is moved -- not after Recording() blows up mid-job.

def test_confirm_rejects_non_numeric_shnid_before_moving_anything(app, client, tmp_path):
    _login_as(client)
    app.config["LIBRARY_ROOT"] = str(tmp_path / "lib")
    os.makedirs(app.config["LIBRARY_ROOT"], exist_ok=True)

    src = tmp_path / "src" / "some-show"
    _silent_flac(src / "01.flac")
    assert src.is_dir()

    resp = client.post("/api/ingest/confirm", json={
        "source_folder_path": str(src),
        "artist_name": "Shnid Test Artist",
        "start_year": 1999, "start_month": 1, "start_day": 1,
        "etree_shnid": "abc123",   # non-numeric -- a stray keystroke in the free-text field
        "tracks": [{"track_number": 1, "title": "One", "filename": "01.flac"}],
        "fingerprints": [], "skip_analysis": True,
    })
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "error" in resp.get_json()

    # Nothing moved: the source folder and its file are exactly as they were,
    # and no background job was even started (a 400 here means confirm_ingest
    # returned before touching _INGEST_JOBS/threading at all).
    assert src.is_dir()
    assert (src / "01.flac").is_file()
    from app.models.artist import Artist
    assert _db.session.query(Artist).filter_by(name="Shnid Test Artist").first() is None


def test_do_confirm_rejects_non_numeric_shnid_directly_before_any_move(app, db, tmp_path):
    """Same rejection, called the way a future caller might bypass the
    route -- _do_confirm() itself must refuse before step 1, not step 7."""
    from app.api.ingest import _do_confirm

    lib = tmp_path / "lib"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)

    src = tmp_path / "src" / "some-show"
    _silent_flac(src / "01.flac")

    uid = db.session.query(User).first().id
    data = {
        "source_folder_path": str(src), "artist_name": "Shnid Test Artist 2",
        "start_year": 1999, "start_month": 1, "start_day": 1,
        "etree_shnid": "not-a-number",
        "tracks": [{"track_number": 1, "title": "One", "filename": "01.flac"}],
        "fingerprints": [], "skip_analysis": True,
    }
    with pytest.raises(ValueError):
        _do_confirm(data, uid, None)

    assert src.is_dir()
    assert (src / "01.flac").is_file()
    from app.models.artist import Artist
    assert _db.session.query(Artist).filter_by(name="Shnid Test Artist 2").first() is None
