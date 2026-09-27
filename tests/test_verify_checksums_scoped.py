"""
tests/test_verify_checksums_scoped.py -- POST /api/recordings/<id>/verify-
checksums, scoped by RecordingFingerprint.rel_path (spec section 3.1, D7):
a per-disc checksum file's bare local filenames ("01.flac", ...) are not
unique once matching falls back to original names, so candidates must be
scoped to that fingerprint file's own directory, exactly as ingest-time
matching (D6) already is.
"""

import hashlib

import pytest

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.user import User


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def test_verify_checksums_scopes_per_disc_checksum_file_after_keep_ingest(app, db, api, tmp_path):
    """A CD1/CD2 source ingested under 'keep' (nesting + original names
    preserved -- see chunk 6) with each disc's own checksum.md5. Re-running
    verify-checksums must still match each disc's file to its own tracks,
    not let CD2's shared "01.flac" basename steal CD1's checksum or vice
    versa."""
    from app.api.ingest import _do_confirm
    from app.utils.ingest import scan_folder

    src = tmp_path / "src_scoped"; src.mkdir()
    cd1 = src / "CD1"; cd1.mkdir()
    cd2 = src / "CD2"; cd2.mkdir()

    b1 = b"scoped disc one track one" * 5
    b2 = b"scoped disc two track one" * 5     # same local name "01.flac" as b1
    (cd1 / "01.flac").write_bytes(b1)
    (cd2 / "01.flac").write_bytes(b2)

    md5_1, md5_2 = (hashlib.md5(b).hexdigest() for b in (b1, b2))
    (cd1 / "checksum.md5").write_text(f"{md5_1} *01.flac\n")
    (cd2 / "checksum.md5").write_text(f"{md5_2} *01.flac\n")

    lib = tmp_path / "lib_scoped"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    scan = scan_folder(str(src))
    tracks_payload = [
        {"track_number": i + 1, "title": f"Track {i + 1}",
         "duration": 100, "filename": af["rel_path"], "set_number": af["set_number"],
         "disc_number": af["disc_number"], "disc_track_number": af["disc_track_number"]}
        for i, af in enumerate(scan["audio_files"])
    ]
    data = {
        "source_folder_path": str(src),
        "artist_name": "Scoped Verify Test Act",
        "start_year": 1990, "start_month": 6, "start_day": 6,
        "source": "AUD",
        "tracks": tracks_payload,
        "fingerprints": [
            {"type": fp["type"], "filename": fp["filename"], "rel_path": fp["rel_path"]}
            for fp in scan["fingerprints"]
        ],
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    tracks = sorted(rec.tracks, key=lambda t: t.track_number)

    # Ingest-time matching (already scoped, chunk 6) got it right...
    assert [t.file_path for t in tracks] == ["CD1/01.flac", "CD2/01.flac"]
    assert tracks[0].expected_checksum == md5_1
    assert tracks[1].expected_checksum == md5_2

    # ...and re-running verify-checksums through the endpoint must NOT lose
    # that scoping now that it's re-deriving candidates from RecordingFingerprint
    # rows (rel_path) rather than the ingest-time original_filenames dict.
    resp = api.post(f"/api/recordings/{rec.id}/verify-checksums")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["checked"] == 2

    _db.session.expire_all()
    tracks = sorted(_db.session.get(Recording, rec.id).tracks, key=lambda t: t.track_number)
    assert tracks[0].expected_checksum == md5_1
    assert tracks[0].checksum_status == "match"
    assert tracks[1].expected_checksum == md5_2
    assert tracks[1].checksum_status == "match"

    # The RecordingFingerprint rows created at ingest carry their own
    # directory, which is what makes the re-scope possible.
    fps = sorted(rec.fingerprints, key=lambda fp: fp.rel_path)
    assert [fp.rel_path for fp in fps] == ["CD1/checksum.md5", "CD2/checksum.md5"]
