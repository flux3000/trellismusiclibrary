"""
tests/test_tag_staging.py -- GET /api/recordings/<id>/tags carries, per track,
the tags Write Tags to Files WOULD write ("staged") next to the ones on disk, so
the Tags pane can show Current/After. desired_track_tags() is the single source
for that and for the writer, so after a write the two sides must be identical.
"""

import numpy as np
import soundfile as sf

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.user import User
from app.utils import node_settings


def _silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")


def _login(client):
    user = _db.session.query(User).filter_by(username="admin").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _tracks(client, rec_id):
    res = client.get(f"/api/recordings/{rec_id}/tags")
    assert res.status_code == 200
    return res.get_json()["tracks"]


def test_staged_tags_follow_the_database_until_written(app, db, tmp_path):
    from app.api.ingest import _do_confirm

    node_settings.apply_mode("organize")   # tags are written at ingest
    src = tmp_path / "src"; src.mkdir()
    _silent_flac(src / "t01.flac")
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id
    result = _do_confirm({
        "source_folder_path": str(src), "artist_name": "Staging Act",
        "start_year": 1981, "start_month": 3, "start_day": 4, "source": "SBD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100,
                    "filename": "t01.flac"}],
    }, uid, None)
    rec_id = result["recording_id"]
    client = app.test_client()
    _login(client)

    # Freshly written: nothing is staged, both sides agree.
    t = _tracks(client, rec_id)[0]
    assert t["staged"] == t["tags"]

    # Edit the database only: the staged side moves, the disk side does not.
    track = _db.session.get(Recording, rec_id).tracks[0]
    track.title = "Renamed"
    track.notes = "a note"
    track.songwriter = "A. Writer"
    _db.session.commit()
    t = _tracks(client, rec_id)[0]
    assert t["tags"]["TITLE"] == "One" and t["staged"]["TITLE"] == "Renamed"
    assert "COMMENT" not in t["tags"] and t["staged"]["COMMENT"] == "a note"
    assert "COMPOSER" not in t["tags"] and t["staged"]["COMPOSER"] == "A. Writer"

    # Writing makes them identical again: one source feeds both.
    assert client.post(f"/api/recordings/{rec_id}/write-tags").status_code == 200
    t = _tracks(client, rec_id)[0]
    assert t["staged"] == t["tags"]
