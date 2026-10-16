"""Recording.location, Move updating the stored path, the boot backfill, and
duplicate handling against recordings in Workshop/Backlog (Ryan, 2026-10-14).

Reworked 2026-10-14 (location as the source of truth): there is no Restore to
Library any more (the endpoint is gone), and an exact duplicate of a recording in
Workshop/Backlog is linked to it instead of being held for a restore."""
import os

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording, RecordingFingerprint
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.models.user import User
from app.utils import bulk_ingest_run
from app.utils.paths import location_root, recording_folder_abs
from app.utils.published import backfill_working_folder_locations

from tests.test_studio_sharing import _recording
from tests.test_studio_library_payloads import client  # noqa: F401  (fixture)

HASH = "abcd1234abcd1234abcd1234abcd1234"


@pytest.fixture()
def dirs(app, tmp_path):
    lib = tmp_path / "lib"
    work, back = tmp_path / "Workshop", tmp_path / "Backlog"
    for d in (lib, work, back):
        d.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["TRIAGE_DIRS"] = {"workshop": str(work), "backlog": str(back)}
    return lib, work, back


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    a = FLAC(str(path))
    tags.setdefault("TRACKNUMBER", "1")
    for k, v in tags.items():
        a[k] = v
    a.save()


def _rec_with_folder(root, name="Show", label="Moved"):
    rec = _recording(label)
    rec.folder_path = name
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "01.flac").write_bytes(b"fLaC")
    _db.session.add(Track(recording_id=rec.id, track_number=1, title="One", file_path="01.flac"))
    _db.session.commit()
    return rec


def test_helper_resolves_each_location(app, dirs):
    lib, work, back = dirs
    rec = _recording("H")
    assert recording_folder_abs(rec) == os.path.join(str(lib), rec.folder_path)
    rec.location, rec.folder_path = "backlog", "X"
    assert recording_folder_abs(rec) == os.path.join(str(back), "X")
    assert location_root("workshop") == str(work)


def test_move_rewrites_path_and_stays_playable(app, client, dirs):
    lib, work, _ = dirs
    rec = _rec_with_folder(lib)
    tid = rec.tracks[0].id
    assert client.get(f"/api/stream/{tid}").status_code == 200

    r = client.post(f"/api/recordings/{rec.id}/move", json={"destination": "workshop"})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert (rec.location, rec.folder_path, rec.is_published) == ("workshop", "Show", False)
    assert (work / "Show" / "01.flac").exists() and not (lib / "Show").exists()
    assert client.get(f"/api/stream/{tid}").status_code == 200          # still playable
    assert client.get(f"/api/recordings/{rec.id}").get_json()["location"] == "workshop"
    assert _db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="moved_out_of_library").count() == 1


def test_restore_endpoint_is_gone(app, client, dirs):
    lib, _, _ = dirs
    rec = _rec_with_folder(lib)
    assert client.post(f"/api/recordings/{rec.id}/restore").status_code in (404, 405)
    assert client.post("/api/bulk-ingest/items/1/restore").status_code in (404, 405)


def test_working_folder_page_names_the_recording(app, client, dirs):
    lib, work, _ = dirs
    rec = _rec_with_folder(work, "Known")
    (work / "Stranger").mkdir()
    (work / "Stranger" / "a.flac").write_bytes(b"fLaC")
    rec.location, rec.is_published = "workshop", False
    _db.session.commit()
    rows = {f["name"]: f["recording_id"] for f in
            client.get("/api/downloads/folder?which=workshop").get_json()["folders"]}
    assert rows == {"Known": rec.id, "Stranger": None}


def test_folder_rename_skips_working_folders(app, dirs):
    from app.utils.folder_naming import rename_recording_folder
    rec = _recording("R")
    rec.location = "workshop"
    assert rename_recording_folder(rec, "/x", rename_folders=True) is None


def test_backfill_uses_latest_event_and_leaves_missing_alone(app, dirs):
    lib, work, back = dirs
    uid = _db.session.query(User).first().id
    a = _recording("A")
    a._is_published_legacy = False           # a row from before `location`: legacy flag off, still 'library'
    a.folder_path = "Old/A"
    (back / "A-new").mkdir()
    for note in ("Moved to workshop as A-old", "Moved to backlog as A-new"):
        _db.session.add(RecordingEvent(recording_id=a.id, user_id=uid,
                                       event_type="moved_out_of_library", note=note))
    b = _recording("B")
    b._is_published_legacy = False
    b.folder_path = "Old/B"
    _db.session.add(RecordingEvent(recording_id=b.id, user_id=uid,
                                   event_type="moved_out_of_library", note="Moved to workshop as nope"))
    _db.session.commit()
    assert backfill_working_folder_locations() == 1
    _db.session.expire_all()
    a, b = _db.session.get(Recording, a.id), _db.session.get(Recording, b.id)
    assert (a.location, a.folder_path) == ("backlog", "A-new")
    assert (b.location, b.folder_path) == ("library", "Old/B")
    assert backfill_working_folder_locations() == 0                        # idempotent


def test_schema_upgrade_adds_location(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from app.utils.schema_upgrades import ensure_recording_location
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, folder_path TEXT)")
    con.execute("INSERT INTO recording (folder_path) VALUES ('x')")
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{path}")
    assert ensure_recording_location(eng) is True
    assert ensure_recording_location(eng) is None
    with eng.connect() as c:
        assert c.exec_driver_sql("SELECT location FROM recording").scalar() == "library"
    assert ensure_recording_location(create_engine(f"sqlite:///{tmp_path / 'none.db'}")) is None
    assert not (tmp_path / "none.db").exists()


# ── duplicates ────────────────────────────────────────────────────────────────

def _never_stop():
    return False


def _run(root, mode="hold"):
    run = BulkIngestRun(root=str(root), status="running", mode=mode)
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)
    return run


def _item(run, rel):
    _db.session.expire_all()
    return _db.session.query(BulkIngestItem).filter_by(run_id=run.id, rel_path=rel).first()


def _fp(rec, name="a.ffp"):
    _db.session.add(RecordingFingerprint(recording_id=rec.id, fingerprint_type="ffp",
                                         filename=name, content=f"{HASH} *01.flac\n"))
    _db.session.commit()


@pytest.mark.parametrize("where", ["workshop", "backlog"])
def test_exact_duplicate_of_a_working_recording_is_linked_not_added(app, client, dirs, tmp_path, where):
    lib, work, back = dirs
    rec = _rec_with_folder(work if where == "workshop" else back, "Held")
    rec.location = where
    _db.session.commit()
    _fp(rec)
    inbox = tmp_path / "inbox"
    _flac(inbox / "Fresh copy" / "01.flac", ARTIST="Moved Band", DATE="1999-01-01", VENUE="Hall")
    (inbox / "Fresh copy" / "c.ffp").write_text(f"{HASH} *01.flac\n")

    before = _db.session.query(Recording).count()
    run = _run(inbox, mode="auto")                       # even Import Automatically
    it = _item(run, "Fresh copy")
    assert (it.status, it.reason, it.duplicate_of, it.detail) == ("skipped", "duplicate_content", rec.id, where)
    assert it.recording_id is None
    assert _db.session.query(Recording).count() == before          # no row for the duplicate
    assert _db.session.get(Recording, rec.id).location == where     # and nothing restored


def test_row_whose_folder_is_the_recordings_own_folder_links_to_it(app, dirs):
    lib, work, _ = dirs
    app.config["TRIAGE_DIRS"] = {"workshop": str(lib / "Workshop")}
    (lib / "Workshop").mkdir()
    _flac(lib / "Workshop" / "Mine" / "01.flac", ARTIST="Moved Band", DATE="1999-01-01", VENUE="Hall")
    rec = _recording("Moved", published=False)
    rec.location, rec.folder_path = "workshop", "Mine"
    _db.session.commit()
    before = _db.session.query(Recording).count()
    run = _run(lib, mode="auto")
    it = _item(run, "Workshop/Mine")
    assert it.recording_id == rec.id and it.status in ("ready", "review")      # never imported by the worker
    assert _db.session.query(Recording).count() == before
    assert _db.session.get(Recording, rec.id).location == "workshop"


def test_published_exact_duplicate_is_still_skipped(app, dirs, tmp_path):
    lib, _, _ = dirs
    rec = _recording("Moved")
    _fp(rec)
    inbox = tmp_path / "inbox"
    _flac(inbox / "Dup" / "01.flac", ARTIST="Moved Band", DATE="1999-01-01")
    (inbox / "Dup" / "c.ffp").write_text(f"{HASH} *01.flac\n")
    it = _item(_run(inbox), "Dup")
    assert (it.status, it.reason, it.duplicate_of) == ("skipped", "duplicate_content", rec.id)


@pytest.mark.parametrize("published,where", [(True, None), (False, "workshop")])
def test_same_show_is_a_flag_not_a_hold(app, client, dirs, tmp_path, published, where):
    rec = _recording("Moved", published=published)
    if where:
        rec.location = where
        _db.session.commit()
    inbox = tmp_path / "inbox"
    _flac(inbox / "Other source" / "01.flac", ARTIST="Moved Band", DATE="1999-01-01", VENUE="Hall")
    it = _item(_run(inbox), "Other source")
    assert it.status == "ready" and it.reason is None
    row = client.get("/api/bulk-ingest/queue/items?status=queue").get_json()["items"][0]
    assert row["same_show"] == [{"id": rec.id, "in": where}]


def test_no_same_show_flag_when_nothing_matches(app, client, dirs, tmp_path):
    inbox = tmp_path / "inbox"
    _flac(inbox / "Solo" / "01.flac", ARTIST="Nobody Else", DATE="2001-02-03", VENUE="Hall")
    _run(inbox)
    row = client.get("/api/bulk-ingest/queue/items?status=queue").get_json()["items"][0]
    assert row["status"] == "ready" and row["same_show"] == []
