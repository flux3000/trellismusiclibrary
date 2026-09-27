"""
tests/test_rename_files_action.py -- POST /api/recordings/<id>/rename-files
(spec section 6.4/3.1 D12): an explicit, always-allowed action that renames
a recording's audio files on disk to a chosen naming scheme, independent of
file_handling_mode. Also GET /api/recordings/<id>'s files_staged field
(section 6.4), which previews whether the button should show as staged
with no disk access.
"""

import os

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.venue import Venue
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.models.user import User
from app.utils import node_settings


@pytest.fixture()
def api(app):
    """A real logged-in session -- current_user.id is read by the endpoint
    under test (the files_renamed event), unlike LOGIN_DISABLED's anonymous
    user."""
    from app.extensions import login_manager
    client = app.test_client()
    user = _db.session.query(User).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass
    return client


@pytest.fixture()
def rec_with_files(app, tmp_path):
    """A flat two-track recording whose current names differ from what
    'number_title' would produce -- the shape a Rename Files click targets."""
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    artist = Artist(name="Rename Files Test Act")
    _db.session.add(artist)
    venue = Venue(name="Some Hall", city="Anytown", state="XX", country="US")
    _db.session.add(venue)
    _db.session.flush()
    perf = Performance(artist_id=artist.id, venue_id=venue.id,
                       start_year=1979, start_month=10, start_day=19)
    _db.session.add(perf)
    _db.session.flush()

    rel = "Rename Files Test Act/1979-10-19"
    (tmp_path / rel).mkdir(parents=True)
    rec = Recording(performance_id=perf.id, source="AUD", folder_path=rel)
    _db.session.add(rec)
    _db.session.flush()

    tracks = []
    for num, title, fname in ((1, "Big Boss Man", "th1979-10-19t01.flac"),
                              (2, "Little Red Rooster", "th1979-10-19t02.flac")):
        (tmp_path / rel / fname).write_bytes(f"audio-{num}".encode())
        t = Track(recording_id=rec.id, track_number=num, title=title,
                  file_path=fname, original_file_path=fname)
        _db.session.add(t)
        tracks.append(t)
    _db.session.commit()

    return {"root": tmp_path, "rel": rel, "rec_id": rec.id,
            "track_ids": [t.id for t in tracks]}


def test_files_staged_nonzero_when_names_differ_from_the_active_scheme(app, db, api, rec_with_files):
    node_settings.apply_mode("organize")   # number_title, differs from current names

    resp = api.get(f"/api/recordings/{rec_with_files['rec_id']}")
    assert resp.status_code == 200
    assert resp.get_json()["files_staged"] == 2


def test_files_staged_zero_when_names_already_match(app, db, api, rec_with_files):
    node_settings.apply_mode("keep")   # 'original' scheme -- every name already matches itself

    resp = api.get(f"/api/recordings/{rec_with_files['rec_id']}")
    assert resp.status_code == 200
    assert resp.get_json()["files_staged"] == 0


def test_rename_files_plan_preview_has_no_disk_effect(app, db, api, rec_with_files):
    """GET (files_staged) and the naming preview endpoint never touch disk --
    only the POST does."""
    from app.utils.file_naming import rename_plan
    rec = _db.session.get(Recording, rec_with_files["rec_id"])
    rename_plan(rec, "number_title")   # computing a plan alone

    folder = rec_with_files["root"] / rec_with_files["rel"]
    assert sorted(p.name for p in folder.iterdir()) == [
        "th1979-10-19t01.flac", "th1979-10-19t02.flac",
    ]


def test_rename_files_with_the_global_scheme(app, db, api, rec_with_files):
    node_settings.apply_mode("organize")
    folder = rec_with_files["root"] / rec_with_files["rel"]

    resp = api.post(f"/api/recordings/{rec_with_files['rec_id']}/rename-files", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["renamed"] == 2
    assert body["errors"] == []

    names = sorted(p.name for p in folder.iterdir())
    assert names == ["01 - Big Boss Man.flac", "02 - Little Red Rooster.flac"]

    rec = _db.session.get(Recording, rec_with_files["rec_id"])
    file_paths = sorted(t.file_path for t in rec.tracks)
    assert file_paths == ["01 - Big Boss Man.flac", "02 - Little Red Rooster.flac"]

    events = db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="files_renamed").all()
    assert len(events) == 1
    assert events[0].note == "2 file(s) renamed"


def test_rename_files_per_click_override_scheme_not_saved(app, db, api, rec_with_files):
    """A per-click override scheme in the request body applies to this call
    only -- it never touches the stored naming_scheme setting."""
    node_settings.apply_mode("keep")   # global scheme stays 'original'

    resp = api.post(f"/api/recordings/{rec_with_files['rec_id']}/rename-files",
                    json={"scheme": "number_title"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["renamed"] == 2

    folder = rec_with_files["root"] / rec_with_files["rel"]
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["01 - Big Boss Man.flac", "02 - Little Red Rooster.flac"]

    # The stored setting itself is untouched by the override.
    assert node_settings.get_file_handling()["naming_scheme"] == "original"


def test_rename_files_one_failure_does_not_block_the_others(app, db, api, rec_with_files):
    """A file missing from disk is reported, not fatal to the batch."""
    node_settings.apply_mode("organize")
    folder = rec_with_files["root"] / rec_with_files["rel"]
    os.remove(folder / "th1979-10-19t01.flac")   # track 1's file has vanished

    resp = api.post(f"/api/recordings/{rec_with_files['rec_id']}/rename-files", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["renamed"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0][0] == "th1979-10-19t01.flac"

    # Track 2 still made it through.
    assert (folder / "02 - Little Red Rooster.flac").exists()


def test_rename_files_re_matches_checksums(app, db, api, rec_with_files):
    """After a rename, a checksum file listing the OLD names still resolves,
    proving matching re-runs against the new file_path (D7)."""
    import hashlib
    from app.models.recording import RecordingFingerprint

    node_settings.apply_mode("organize")
    folder = rec_with_files["root"] / rec_with_files["rel"]
    b1 = (folder / "th1979-10-19t01.flac").read_bytes()
    b2 = (folder / "th1979-10-19t02.flac").read_bytes()
    md5_1, md5_2 = hashlib.md5(b1).hexdigest(), hashlib.md5(b2).hexdigest()

    _db.session.add(RecordingFingerprint(
        recording_id=rec_with_files["rec_id"], fingerprint_type="md5",
        filename="checksum.md5",
        content=f"{md5_1} *th1979-10-19t01.flac\n{md5_2} *th1979-10-19t02.flac\n",
        rel_path="checksum.md5",
    ))
    _db.session.commit()

    resp = api.post(f"/api/recordings/{rec_with_files['rec_id']}/rename-files", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["checked"] == 2
    assert all(t["checksum_status"] == "match" for t in body["tracks"])
