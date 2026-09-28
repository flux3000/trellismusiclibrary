"""
tests/test_ingest_tag_write.py -- ingest-time tag write (spec section 3.2,
_do_confirm step 12): write_tags_on_ingest gates a write_flac_tags() call
after the move and the main commit, hitting the tracks' FINAL path. Off by
default under 'keep'; per-file errors go on the job result as tag_errors
without failing the ingest; a tags_written event is added only on success
(mirrors POST /api/recordings/<id>/write-tags's own rule).
"""

import os
import stat

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.user import User
from app.utils import node_settings


def _silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")


def test_ingest_tag_write_hits_the_renamed_path(app, db, tmp_path):
    """write_tags_on_ingest on: the recording gets tagged after the move,
    at the FINAL (renamed) path, and a tags_written event is recorded."""
    from app.api.ingest import _do_confirm

    node_settings.apply_mode("organize")

    src = tmp_path / "src_show"; src.mkdir()
    _silent_flac(src / "t01.flac")
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Tag Write Test Act",
        "start_year": 1981, "start_month": 3, "start_day": 4,
        "source": "SBD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100,
                    "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    assert result["tag_errors"] == []

    rec = _db.session.get(Recording, result["recording_id"])
    track = rec.tracks[0]
    assert track.file_path == "01 - One.flac"   # organize's default scheme

    abs_path = os.path.join(str(lib), rec.folder_path, track.file_path)
    audio = FLAC(abs_path)
    assert audio["TITLE"][0] == "One"
    assert audio["TRACKNUMBER"][0] == "1"

    events = db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").all()
    assert len(events) == 1
    assert events[0].note == "1 file(s) written"


def test_ingest_tag_write_off_by_default_in_keep(app, db, tmp_path):
    """Default 'keep' state: write_tags_on_ingest is off, so no tag write
    happens and no tags_written event is created."""
    from app.api.ingest import _do_confirm

    src = tmp_path / "src_show2"; src.mkdir()
    _silent_flac(src / "t01.flac")
    lib = tmp_path / "lib2"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "No Tag Write Act",
        "start_year": 1982, "start_month": 3, "start_day": 4,
        "source": "SBD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100,
                    "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    assert result["tag_errors"] == []

    rec = _db.session.get(Recording, result["recording_id"])
    events = db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").all()
    assert events == []

    abs_path = os.path.join(str(lib), rec.folder_path, rec.tracks[0].file_path)
    audio = FLAC(abs_path)
    # Untouched by any tag write -- no Vorbis comments at all.
    assert list(audio.keys()) == []


def test_ingest_tag_write_reports_a_read_only_file_error_without_failing(app, db, tmp_path):
    """A file the process can't write to (permissions changed between the
    move and the tag-write phase) yields exactly one tag_errors entry on the
    job result -- the ingest itself still returns normally (job status stays
    'done', spec section 3.2), and since n_written is 0 for this one-track
    recording, no tags_written event is logged (mirrors the write-tags
    endpoint's own no-event-on-total-failure rule)."""
    from app.api.ingest import _do_confirm
    from app.utils import ingest as ingest_utils

    node_settings.apply_mode("organize")

    src = tmp_path / "src_show3"; src.mkdir()
    _silent_flac(src / "t01.flac")
    lib = tmp_path / "lib3"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Read Only Tag Test Act",
        "start_year": 1983, "start_month": 3, "start_day": 4,
        "source": "SBD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100,
                    "filename": "t01.flac"}],
    }

    # write_flac_tags() is called from inside _do_confirm after the move has
    # already placed the file at its final path -- chmod it read-only right
    # then, via a thin wrapper around the real function, so the test exerts
    # the actual ingest codepath rather than calling write_flac_tags a
    # second time on the side.
    real_write_flac_tags = ingest_utils.write_flac_tags

    def _chmod_then_write(recording, library_root):
        abs_path = os.path.join(library_root, recording.folder_path,
                                 recording.tracks[0].file_path)
        os.chmod(abs_path, stat.S_IREAD)
        try:
            return real_write_flac_tags(recording, library_root)
        finally:
            os.chmod(abs_path, stat.S_IREAD | stat.S_IWRITE)

    import app.api.ingest as ingest_api
    ingest_api.write_flac_tags = _chmod_then_write
    try:
        result = _do_confirm(data, uid, None)
    finally:
        ingest_api.write_flac_tags = real_write_flac_tags

    assert len(result["tag_errors"]) == 1

    rec = _db.session.get(Recording, result["recording_id"])
    events = db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").all()
    assert events == []



def test_in_root_source_never_gets_the_tag_write_even_when_bulk_is_false(app, db, tmp_path):
    """R2-N7: an in-root source (found already sitting inside LIBRARY_ROOT --
    e.g. a Review & Ingest Accept on a Bulk Ingest review row) must skip
    step 12 regardless of write_tags_on_ingest, even though that call goes
    through _do_confirm with bulk=False (Accept is a deliberate click, not
    an unattended pass, so bulk alone can't be the gate -- see
    [[project_bring_your_own_library]])."""
    from app.api.ingest import _do_confirm

    node_settings.apply_mode("organize")
    assert node_settings.get_file_handling()["write_tags_on_ingest"] is True

    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    show = lib / "Phish" / "ph1997-11-22"
    _silent_flac(show / "01.flac")
    audio = FLAC(str(show / "01.flac")); audio["TITLE"] = "Original Title"; audio.save()
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(show),
        "artist_name": "Phish",
        "start_year": 1997, "start_month": 11, "start_day": 22,
        "venue_name": "Hampton Coliseum",
        "tracks": [{"track_number": 1, "title": "Mike's Song",
                    "filename": "01.flac"}],
        "is_complete": True,
    }
    result = _do_confirm(data, uid, None)  # bulk defaults to False

    rec = _db.session.get(Recording, result["recording_id"])
    events = db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").all()
    assert events == [], "in-root source must never get the ingest tag write"

    abs_path = os.path.join(str(lib), rec.folder_path, rec.tracks[0].file_path)
    assert FLAC(abs_path)["TITLE"][0] == "Original Title"


def test_outside_root_source_still_gets_the_tag_write_in_organize(app, db, tmp_path):
    """R2-N7 (guard rail): the fix is scoped to in-root sources only -- a
    source arriving from OUTSIDE LIBRARY_ROOT still follows
    write_tags_on_ingest exactly as before, bulk or not."""
    from app.api.ingest import _do_confirm

    node_settings.apply_mode("organize")

    lib = tmp_path / "lib_out"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    src = tmp_path / "outside_src"; src.mkdir()
    _silent_flac(src / "01.flac")
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Grateful Dead",
        "start_year": 1977, "start_month": 5, "start_day": 8,
        "venue_name": "Barton Hall",
        "tracks": [{"track_number": 1, "title": "Scarlet Begonias",
                    "filename": "01.flac"}],
        "is_complete": True,
    }
    result = _do_confirm(data, uid, None)

    rec = _db.session.get(Recording, result["recording_id"])
    events = db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").all()
    assert len(events) == 1
