"""tests/test_location_model.py -- Recording.location as the source of truth
(Ryan, 2026-10-14): one recording per folder from the first read, the Review form's
autosave, one promote path into the library, Move / Trash from the Queue, Workshop and
Backlog, Reset Queue, and entity lists that show only what has a library recording.
"""
import os

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.bulk_ingest import BulkIngestItem, BulkIngestRun
from app.models.engineer import Engineer
from app.models.event import Event
from app.models.genre import Genre
from app.models.musician import Musician, Membership
from app.models.recording import Recording, RecordingFingerprint
from app.models.track import Track
from app.models.venue import Venue
from app.utils import bulk_ingest_run as bir
from app.utils.paths import recording_folder_abs

from tests.test_bulk_ingest_review_first import (  # noqa: F401  (env is a fixture)
    env, _flac, _good, _flagged, _hold_run, _item, _admin, _never_stop, _run as _new_run)
from tests.test_studio_sharing import _recording

HASH = "abcd1234abcd1234abcd1234abcd1234"


@pytest.fixture()
def client(app, env):  # noqa: F811
    c = app.test_client()
    _admin(c)
    return c


@pytest.fixture()
def work(app, env, tmp_path):  # noqa: F811
    allowed = env.lib.parent
    ws, bl = allowed / "Workshop", allowed / "Backlog"
    ws.mkdir()
    bl.mkdir()
    app.config["TRIAGE_DIRS"] = {"workshop": str(ws), "backlog": str(bl)}
    return type("Work", (), dict(workshop=ws, backlog=bl))


def _rec_of(run, rel):
    it = _item(run, rel)
    return _db.session.get(Recording, it.recording_id) if it.recording_id else None


# ── the hybrid ───────────────────────────────────────────────────────────────

def test_is_published_is_derived_from_location_in_python_and_sql(app):
    pub, hidden = _recording("Pub"), _recording("Hid")
    hidden.location = "backlog"
    _db.session.commit()
    assert pub.is_published is True and hidden.is_published is False
    ids = lambda cond: {r.id for r in _db.session.query(Recording).filter(cond)}   # noqa: E731
    assert pub.id in ids(Recording.is_published.is_(True)) and hidden.id not in ids(Recording.is_published.is_(True))
    assert hidden.id in ids(Recording.is_published.is_(False))
    hidden.location = "queue"
    assert hidden.is_published is False
    hidden.location = "library"
    assert hidden.is_published is True
    # The old column is kept in step so a rolled-back build reads the right answer.
    _db.session.commit()
    from sqlalchemy import text
    assert _db.session.execute(text("select is_published from recording where id = :i"),
                               {"i": hidden.id}).scalar() == 1


def test_serialized_payload_keeps_is_published(app, client):
    rec = _recording("Ser")
    body = client.get(f"/api/recordings/{rec.id}").get_json()
    assert body["is_published"] is True and body["location"] == "library"


# ── a recording from the first read ──────────────────────────────────────────

def test_a_row_is_created_when_the_queue_reads_a_folder(app, env):
    _good(env.incoming, "S1")
    before = _db.session.query(Recording).count()
    run = _hold_run(env)
    it = _item(run, "S1")
    rec = _db.session.get(Recording, it.recording_id)
    assert it.status == "ready" and rec.location == "queue" and rec.is_published is False
    assert _db.session.query(Recording).count() == before + 1
    assert rec.performance.artist.name == "Grateful Dead"
    assert [t.track_number for t in rec.tracks] == [1]
    assert recording_folder_abs(rec) == str(env.incoming / "S1")      # resolved through the Queue row


def test_a_folder_that_fails_to_read_gets_no_row(app, env):
    bad = env.incoming / "Broken"
    bad.mkdir()
    (bad / "01.flac").write_bytes(b"this is not audio")
    before = _db.session.query(Recording).count()
    run = _hold_run(env)
    it = _item(run, "Broken")
    assert it.status == "failed" and it.recording_id is None
    assert _db.session.query(Recording).count() == before


def test_exact_duplicate_of_a_queue_recording_is_linked_and_gets_no_row(app, env):
    for n in ("A", "B"):
        _good(env.incoming, n)
        (env.incoming / n / "x.ffp").write_text(f"{HASH} *01.flac\n")
    before = _db.session.query(Recording).count()
    run = _hold_run(env)
    first, second = _item(run, "A"), _item(run, "B")
    assert first.recording_id and first.status == "ready"
    assert (second.status, second.reason, second.duplicate_of, second.detail) == (
        "skipped", "duplicate_content", first.recording_id, "queue")
    assert second.recording_id is None
    assert _db.session.query(Recording).count() == before + 1


def test_exact_duplicate_of_a_library_recording_is_skipped_with_no_row(app, env):
    lib_rec = _recording("Lib")
    _db.session.add(RecordingFingerprint(recording_id=lib_rec.id, fingerprint_type="ffp",
                                         filename="a.ffp", content=f"{HASH} *01.flac\n"))
    _db.session.commit()
    _good(env.incoming, "Dup")
    (env.incoming / "Dup" / "x.ffp").write_text(f"{HASH} *01.flac\n")
    before = _db.session.query(Recording).count()
    run = _hold_run(env)
    it = _item(run, "Dup")
    assert (it.status, it.duplicate_of, it.recording_id) == ("skipped", lib_rec.id, None)
    assert _db.session.query(Recording).count() == before


def test_same_show_as_an_existing_recording_gets_its_own_row_flagged(app, env):
    import json
    other = _recording("Other")
    _flac(env.incoming / "Twin" / "01.flac", ARTIST="Other Band", DATE="1999-01-01", VENUE="Hall")
    run = _hold_run(env)
    it = _item(run, "Twin")
    assert it.recording_id and it.recording_id != other.id and it.status == "ready"
    assert json.loads(it.meta)["same_show"] == [{"id": other.id, "in": None}]


def test_a_waiting_item_without_a_recording_gets_one_on_review(app, env, client):
    _good(env.incoming, "Old")
    run = _new_run(env.incoming)
    bir.discover(run)
    it = _item(run, "Old")
    assert it.recording_id is None and it.status == "pending"
    r = client.post(f"/api/bulk-ingest/items/{it.id}/review")
    assert r.status_code == 200, r.get_json()
    rec = _db.session.get(Recording, r.get_json()["recording_id"])
    assert rec.location == "queue"
    assert _db.session.get(BulkIngestItem, it.id).recording_id == rec.id
    # A second Review opens the same recording; it does not read again.
    assert client.post(f"/api/bulk-ingest/items/{it.id}/review").get_json()["recording_id"] == rec.id


# ── the Review form: open, autosave ──────────────────────────────────────────

def test_review_endpoint_returns_the_scan_and_the_saved_draft(app, env, client):
    _good(env.incoming, "S1")
    rec = _rec_of(_hold_run(env), "S1")
    body = client.get(f"/api/recordings/{rec.id}/review").get_json()
    assert body["recording_id"] == rec.id and body["location"] == "queue"
    assert body["draft"]["artist_name"] == "Grateful Dead"
    assert body["draft"]["tracks"] and body["suggestions"]
    lib = _recording("Lib")
    assert client.get(f"/api/recordings/{lib.id}/review").status_code == 409


def test_autosave_takes_partial_data_and_confirms_nothing(app, env, client):
    _good(env.incoming, "S1")
    rec = _rec_of(_hold_run(env), "S1")
    artist = rec.performance.artist
    r = client.put(f"/api/recordings/{rec.id}/draft", json={"notes": "n", "venue_name": "Fresh Hall"})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert rec.notes == "n" and rec.performance.venue.name == "Fresh Hall"
    assert rec.location == "queue"
    assert _db.session.get(Artist, artist.id).confirmed_at is None            # a save is not a confirmation
    assert rec.performance.artist_id == artist.id
    assert _db.session.query(Recording).filter(Recording.location == "queue").count() == 1


def test_autosave_reruns_the_verdict_of_a_queue_row(app, env, client):
    _flagged(env.incoming, "F1")
    run = _hold_run(env)
    it = _item(run, "F1")
    rec = _db.session.get(Recording, it.recording_id)
    assert it.status == "review" and "needs_month" in it.reason
    r = client.put(f"/api/recordings/{rec.id}/draft",
                   json={"start_year": 1997, "start_month": 7, "start_day": 4})
    assert r.get_json()["status"] == "ready"
    _db.session.expire_all()
    it = _db.session.get(BulkIngestItem, it.id)
    assert it.status == "ready" and "needs_month" not in (it.reason or "")
    # Taking the date away puts it back.
    r = client.put(f"/api/recordings/{rec.id}/draft", json={"start_month": None, "start_day": None})
    assert r.get_json()["status"] == "review" and "needs_month" in r.get_json()["reasons"]


def test_a_person_edited_row_keeps_its_values_when_read_again(app, env, client):
    from app.api.bulk_ingest import _reset_for_reanalysis
    _good(env.incoming, "S1")
    run = _hold_run(env)
    rec = _rec_of(run, "S1")
    client.put(f"/api/recordings/{rec.id}/draft", json={"venue_name": "Kept Hall"})
    _reset_for_reanalysis(_item(run, "S1"))
    _db.session.commit()
    bir.process(_db.session.get(BulkIngestRun, run.id), _never_stop)
    _db.session.expire_all()
    assert _db.session.get(Recording, rec.id).performance.venue.name == "Kept Hall"
    assert _db.session.query(Recording).filter(Recording.location == "queue").count() == 1


def test_confirm_flag_fires_the_signals_on_leaving_the_form(app, env, client):
    _good(env.incoming, "S1")
    rec = _rec_of(_hold_run(env), "S1")
    artist_id = rec.performance.artist_id
    client.put(f"/api/recordings/{rec.id}/draft", json={"notes": "x"})
    assert _db.session.get(Artist, artist_id).confirmed_at is None
    client.put(f"/api/recordings/{rec.id}/draft", json={"notes": "x", "confirm": True})
    _db.session.expire_all()
    assert _db.session.get(Artist, artist_id).confirmed_at is not None


def test_autosave_refuses_a_library_recording(app, client):
    lib = _recording("Lib")
    assert client.put(f"/api/recordings/{lib.id}/draft", json={"notes": "x"}).status_code == 409


# ── one promote path ─────────────────────────────────────────────────────────

def _payload_for(folder, env):
    from app.api.ingest import _confirm_payload_from_resolved
    from app.utils.ingest import build_scan_payload
    from app.utils.resolve import resolve
    scan = build_scan_payload(str(folder))
    return _confirm_payload_from_resolved(resolve(scan, library_root=str(env.lib), placement=None), scan)


def test_import_from_the_form_promotes_the_row_without_a_second_one(app, env):
    from app.api.ingest import _do_confirm
    _good(env.incoming, "S1")
    run = _hold_run(env)
    rec = _rec_of(run, "S1")
    folder = env.incoming / "S1"
    payload = _payload_for(folder, env)
    payload["source_folder_path"] = str(folder)
    payload["recording_id"] = rec.id
    before = _db.session.query(Recording).count()
    out = _do_confirm(payload, bir._owner_user_id())
    assert out["recording_id"] == rec.id
    assert _db.session.query(Recording).count() == before            # no second row
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert rec.location == "library" and rec.is_published is True
    assert os.path.isdir(os.path.join(str(env.lib), rec.folder_path)) and not folder.exists()
    assert _db.session.get(BulkIngestItem, _item(run, "S1").id).status == "ingested"


def test_import_all_ready_promotes_without_new_rows(app, env, client):
    run = _hold_run(env, "S1", "S22")
    before = _db.session.query(Recording).count()
    assert client.post(f"/api/bulk-ingest/runs/{run.id}/ingest-ready").status_code == 202
    bir.process(_db.session.get(BulkIngestRun, run.id), _never_stop)
    assert _db.session.query(Recording).count() == before
    for n in ("S1", "S22"):
        it = _item(run, n)
        assert it.status == "ingested" and _db.session.get(Recording, it.recording_id).location == "library"


def test_import_automatically_makes_one_row_per_folder(app, env):
    _good(env.incoming, "S1")
    before = _db.session.query(Recording).count()
    run = _new_run(env.incoming, mode="auto")
    bir.discover(run)
    bir.process(run, _never_stop)
    it = _item(run, "S1")
    assert it.status == "ingested" and _db.session.query(Recording).count() == before + 1
    assert _db.session.get(Recording, it.recording_id).location == "library"


def test_import_of_a_workshop_recording_goes_through_the_same_path(app, env, work):
    from app.api.ingest import promote_to_library, save_recording_payload
    folder = work.workshop / "Show"
    _flac(folder / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Hall W")
    payload = _payload_for(folder, env)
    rec = save_recording_payload(payload, bir._owner_user_id(), None, location="workshop",
                                 folder_path="Show", source_folder=str(folder))
    _db.session.commit()
    before = _db.session.query(Recording).count()
    promote_to_library(rec, bir._owner_user_id(), payload, source_folder=str(folder))
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert _db.session.query(Recording).count() == before
    assert rec.location == "library" and os.path.isdir(os.path.join(str(env.lib), rec.folder_path))


# ── Move ─────────────────────────────────────────────────────────────────────

def test_move_a_queue_recording_to_workshop(app, env, work, client):
    _good(env.incoming, "S1")
    run = _hold_run(env)
    rec = _rec_of(run, "S1")
    r = client.post(f"/api/recordings/{rec.id}/move", json={"destination": "workshop"})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert (rec.location, rec.folder_path) == ("workshop", "S1")
    assert (work.workshop / "S1" / "01.flac").exists() and not (env.incoming / "S1").exists()
    assert _item(run, "S1").status == "moved"                    # leaves the Queue tab
    assert recording_folder_abs(rec) == str(work.workshop / "S1")


def test_move_keeps_the_whole_folder_and_suffixes_a_collision(app, env, work, client):
    _good(env.incoming, "S1")
    (env.incoming / "S1" / "Artwork").mkdir()
    (env.incoming / "S1" / "Artwork" / "cover.jpg").write_bytes(b"x")
    (work.backlog / "S1").mkdir()
    rec = _rec_of(_hold_run(env), "S1")
    r = client.post(f"/api/recordings/{rec.id}/move", json={"destination": "backlog"})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert (rec.location, rec.folder_path) == ("backlog", "S1 (2)")
    assert (work.backlog / "S1 (2)" / "Artwork" / "cover.jpg").exists()      # not flattened


def test_move_between_workshop_and_backlog(app, env, work, client):
    _good(env.incoming, "S1")
    rec = _rec_of(_hold_run(env), "S1")
    client.post(f"/api/recordings/{rec.id}/move", json={"destination": "workshop"})
    r = client.post(f"/api/recordings/{rec.id}/move", json={"destination": "backlog"})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert (rec.location, rec.folder_path) == ("backlog", "S1")
    assert (work.backlog / "S1" / "01.flac").exists() and not (work.workshop / "S1").exists()
    r = client.post(f"/api/recordings/{rec.id}/move", json={"destination": "workshop"})
    assert r.status_code == 200 and _db.session.get(Recording, rec.id).location == "workshop"
    assert client.post(f"/api/recordings/{rec.id}/move", json={"destination": "workshop"}).status_code == 409


def test_a_failed_move_leaves_the_recording_where_it_was(app, env, work, client, monkeypatch):
    from app.api import quality
    _good(env.incoming, "S1")
    run = _hold_run(env)
    rec = _rec_of(run, "S1")
    monkeypatch.setattr(quality, "move_folder_to_triage", lambda *a, **k: ({"error": "nope"}, 500))
    assert client.post(f"/api/recordings/{rec.id}/move", json={"destination": "workshop"}).status_code == 500
    _db.session.expire_all()
    assert _db.session.get(Recording, rec.id).location == "queue" and _item(run, "S1").status == "ready"


def test_queue_item_move_endpoint_moves_the_recording_with_it(app, env, work, client):
    _good(env.incoming, "S1")
    run = _hold_run(env)
    it = _item(run, "S1")
    r = client.post(f"/api/bulk-ingest/items/{it.id}/move", json={"dest": "backlog"})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    assert _db.session.get(Recording, it.recording_id).location == "backlog"
    assert _db.session.get(BulkIngestItem, it.id).status == "moved"


# ── Open / Move for a folder with no recording ───────────────────────────────

def test_open_folder_reads_a_workshop_folder_and_creates_its_recording(app, env, work, client):
    _flac(work.workshop / "Loose" / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Hall L")
    before = _db.session.query(Recording).count()
    r = client.post("/api/recordings/open-folder", json={"which": "workshop", "name": "Loose"})
    assert r.status_code == 200, r.get_json()
    rec = _db.session.get(Recording, r.get_json()["recording_id"])
    assert (rec.location, rec.folder_path) == ("workshop", "Loose")
    assert _db.session.query(Recording).count() == before + 1
    # Opening it again finds the same recording.
    assert client.post("/api/recordings/open-folder",
                       json={"which": "workshop", "name": "Loose"}).get_json()["recording_id"] == rec.id
    # The client never names a path.
    assert client.post("/api/recordings/open-folder",
                       json={"which": "workshop", "name": "../x"}).status_code == 404


def test_move_folder_from_downloads_reads_then_moves(app, env, work, client, monkeypatch):
    import app.utils.downloads_dir as dd
    dl = env.lib.parent / "Downloads"
    _flac(dl / "Fresh" / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-09", VENUE="Hall D")
    monkeypatch.setattr(dd, "downloads_dir", lambda: str(dl))
    r = client.post("/api/recordings/move-folder", json={"which": "downloads", "name": "Fresh",
                                                         "destination": "backlog"})
    assert r.status_code == 200, r.get_json()
    rec = _db.session.get(Recording, r.get_json()["recording_id"])
    assert (rec.location, rec.folder_path) == ("backlog", "Fresh")
    assert (work.backlog / "Fresh" / "01.flac").exists() and not (dl / "Fresh").exists()


# ── Trash ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def trash_log(monkeypatch):
    import shutil
    from app.utils import trash
    seen = []
    monkeypatch.setattr(trash, "move_to_trash", lambda p: (seen.append(p), shutil.rmtree(p)))
    return seen


def test_trash_a_queue_recording_trashes_the_folder_then_deletes_the_row(app, env, client, trash_log):
    _good(env.incoming, "S1")
    run = _hold_run(env)
    rec = _rec_of(run, "S1")
    artist_id, rec_id = rec.performance.artist_id, rec.id
    r = client.post(f"/api/recordings/{rec_id}/trash")
    assert r.status_code == 200, r.get_json()
    assert trash_log == [str(env.incoming / "S1")] and not (env.incoming / "S1").exists()
    _db.session.expire_all()
    assert _db.session.get(Recording, rec_id) is None
    assert _db.session.get(Artist, artist_id) is None                 # nobody confirmed it: gone with the row
    assert _db.session.query(Track).filter_by(recording_id=rec_id).count() == 0
    assert _item(run, "S1").status == "moved"


def test_trash_a_workshop_recording_and_keep_a_confirmed_artist(app, env, work, client, trash_log):
    folder = work.workshop / "Show"
    _flac(folder / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Hall W")
    r = client.post("/api/recordings/open-folder", json={"which": "workshop", "name": "Show"})
    rec = _db.session.get(Recording, r.get_json()["recording_id"])
    artist = rec.performance.artist
    artist.confirmed_at = _db.func.now()
    _db.session.commit()
    aid, rid = artist.id, rec.id
    assert client.post(f"/api/recordings/{rid}/trash").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(Recording, rid) is None and not folder.exists()
    assert _db.session.get(Artist, aid) is not None                   # a person confirmed it: kept


def test_trash_with_the_folder_already_gone_still_removes_the_record(app, env, client, trash_log):
    import shutil
    _good(env.incoming, "S1")
    rec = _rec_of(_hold_run(env), "S1")
    rid = rec.id
    shutil.rmtree(env.incoming / "S1")
    assert client.post(f"/api/recordings/{rid}/trash").status_code == 200
    assert _db.session.get(Recording, rid) is None


def test_trash_a_library_recording_trashes_the_folder_then_deletes_the_row(app, client, trash_log):
    # View Recording's Move to Trash (2026-10-15): library recordings trash too.
    from app.utils.paths import location_root
    lib = _recording("Lib")
    folder = os.path.join(location_root("library"), lib.folder_path)
    os.makedirs(folder)
    open(os.path.join(folder, "01.flac"), "wb").close()
    rid = lib.id
    r = client.post(f"/api/recordings/{rid}/trash")
    assert r.status_code == 200, r.get_json()
    assert trash_log[0] == os.path.realpath(folder) and not os.path.exists(folder)
    _db.session.expire_all()
    assert _db.session.get(Recording, rid) is None


def test_trash_refuses_a_library_folder_another_recording_shares(app, client, trash_log):
    from app.utils.paths import location_root
    a, b = _recording("Shared"), _recording("Other")
    b.folder_path = a.folder_path
    _db.session.commit()
    os.makedirs(os.path.join(location_root("library"), a.folder_path))
    assert client.post(f"/api/recordings/{a.id}/trash").status_code == 409
    assert trash_log == [] and _db.session.get(Recording, a.id) is not None


def test_a_failed_row_can_only_be_trashed(app, env, work, client, trash_log):
    bad = env.incoming / "Broken"
    bad.mkdir()
    (bad / "01.flac").write_bytes(b"this is not audio")
    run = _hold_run(env)
    it = _item(run, "Broken")
    assert client.post(f"/api/bulk-ingest/items/{it.id}/move", json={"dest": "workshop"}).status_code == 409
    assert client.post(f"/api/bulk-ingest/items/{it.id}/move", json={"dest": "trash"}).status_code == 200
    assert trash_log == [str(bad)]


# ── Reset Queue ──────────────────────────────────────────────────────────────

def test_reset_queue_deletes_queue_recordings_and_unconfirmed_entities(app, env):
    _good(env.incoming, "S1")
    _flac(env.incoming / "S2" / "01.flac", ARTIST="Phish", DATE="1997-12-31", VENUE="MSG")
    run = _hold_run(env)
    keep = _rec_of(run, "S2")
    keep.performance.artist.confirmed_at = _db.func.now()          # a person confirmed this act
    _db.session.commit()
    gone_artist = _rec_of(run, "S1").performance.artist_id
    keep_artist = keep.performance.artist_id
    lib = _recording("Lib")                                       # a library recording is never touched
    assert bir.reset_queue(run) == 2
    _db.session.expire_all()
    assert _db.session.query(Recording).filter(Recording.location == "queue").count() == 0
    assert _db.session.get(Artist, gone_artist) is None
    assert _db.session.get(Artist, keep_artist) is not None
    assert _db.session.get(Recording, lib.id) is not None
    assert (env.incoming / "S1" / "01.flac").exists()             # files are never touched


def test_reset_queue_keeps_an_entity_with_enrichment(app, env):
    _good(env.incoming, "S1")
    run = _hold_run(env)
    rec = _rec_of(run, "S1")
    artist = rec.performance.artist
    artist.bio = "A written biography."
    _db.session.commit()
    aid = artist.id
    bir.reset_queue(run)
    _db.session.expire_all()
    assert _db.session.get(Artist, aid) is not None


# ── entity lists show only what has a library recording ──────────────────────

def _entity_set(label, location):
    rec = _recording(label)
    rec.location = location
    perf = rec.performance
    venue = Venue(name=f"{label} Hall")
    event = Event(name=f"{label} Fest")
    genre = Genre(name=f"{label} Genre")
    eng = Engineer(name=f"{label} Engineer")
    musician = Musician(name=f"{label} Player")
    _db.session.add_all([venue, event, genre, eng, musician])
    _db.session.flush()
    perf.venue_id, perf.event_id = venue.id, event.id
    perf.artist.genre_id = genre.id
    rec.recording_engineer_id = eng.id
    _db.session.add(Membership(artist_id=perf.artist_id, musician_id=musician.id))
    _db.session.commit()
    return rec


_LISTS = [("/api/artists/", "Band"), ("/api/venues/", "Hall"), ("/api/events/", "Fest"),
          ("/api/genres/", "Genre"), ("/api/engineers/", "Engineer"), ("/api/musicians/", "Player")]


@pytest.mark.parametrize("hidden_location", ["queue", "workshop", "backlog"])
def test_entity_lists_hide_entities_without_a_library_recording(app, client, hidden_location):
    _entity_set("Shown", "library")
    _entity_set("Hidden", hidden_location)
    for path, suffix in _LISTS:
        names = {r["name"] for r in client.get(path).get_json()}
        assert f"Shown {suffix}" in names, path
        assert f"Hidden {suffix}" not in names, path
        # The index pages read the same endpoints with ?index=1.
        idx = {r["name"] for r in client.get(path + "?index=1").get_json()}
        assert f"Hidden {suffix}" not in idx, path


def test_form_autocompletes_and_editing_lists_still_offer_hidden_entities(app, client):
    rec = _entity_set("Hidden", "workshop")
    for path, suffix in _LISTS:
        names = {r["name"] for r in client.get(path + "?all=1").get_json()}
        assert f"Hidden {suffix}" in names, path
    assert any(r["name"] == "Hidden Hall" for r in client.get("/api/venues/?q=Hidden").get_json())
    assert any(r["name"] == "Hidden Band" for r in client.get("/api/artists/search?q=Hidden").get_json())
    # Direct entity pages still open.
    assert client.get(f"/api/artists/{rec.performance.artist_id}").status_code == 200
    assert client.get(f"/api/venues/{rec.performance.venue_id}").status_code == 200
    assert client.get(f"/api/events/{rec.performance.event_id}").status_code == 200


def test_entity_lists_show_entities_with_no_recordings_at_all(app, client):
    """Hidden only when it has recordings and none are in the library (Ryan, 2026-10-15)."""
    _entity_set("Shown", "library")
    _entity_set("Hidden", "workshop")
    _db.session.add_all([Artist(name="Bare Band"), Venue(name="Bare Hall"), Event(name="Bare Fest"),
                         Genre(name="Bare Genre"), Engineer(name="Bare Engineer"),
                         Musician(name="Bare Player")])
    _db.session.commit()
    for path, suffix in _LISTS:
        names = {r["name"] for r in client.get(path).get_json()}
        assert f"Bare {suffix}" in names, path
        assert f"Shown {suffix}" in names, path
        assert f"Hidden {suffix}" not in names, path


def test_an_entity_appears_once_a_recording_is_imported(app, client):
    rec = _entity_set("Late", "queue")
    assert "Late Band" not in {r["name"] for r in client.get("/api/artists/").get_json()}
    rec.location = "library"
    _db.session.commit()
    assert "Late Band" in {r["name"] for r in client.get("/api/artists/").get_json()}


# ── the share door ───────────────────────────────────────────────────────────

def test_share_door_exposes_only_library_recordings(app):
    from tests.test_studio_sharing import _peer_granted_junction, _peer_granted_full_library
    from app.utils.peer_access import peer_can_access_recording_id, peer_visible_recording_ids
    lib, wk, qu = _recording("L"), _recording("W"), _recording("Q")
    wk.location, qu.location = "workshop", "queue"
    _db.session.commit()
    _col, peer, _raw = _peer_granted_junction(lib, also=(wk, qu))     # a manual collection holding all three
    assert peer_can_access_recording_id(peer, lib.id) is True
    assert peer_can_access_recording_id(peer, wk.id) is False
    assert peer_can_access_recording_id(peer, qu.id) is False
    assert peer_visible_recording_ids(peer) == {lib.id}
    _col2, peer2, _raw2 = _peer_granted_full_library("Jo")
    seen = peer_visible_recording_ids(peer2)          # the seeded library recording is in it too
    assert lib.id in seen and wk.id not in seen and qu.id not in seen


# ── boot pass for rows from before `location` ────────────────────────────────

def test_boot_pass_places_legacy_unpublished_rows_by_their_folder(app, env, work):
    from app.utils.published import unpublish_working_folder_recordings
    (work.workshop / "Old").mkdir()
    rec = _recording("Old")
    rec.folder_path = str(work.workshop / "Old")
    rec._is_published_legacy = False           # the old flag, with location still the default
    _db.session.commit()
    unpublish_working_folder_recordings()
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert (rec.location, rec.folder_path) == ("workshop", "Old")


def test_boot_pass_reopens_rows_an_earlier_build_held_for_a_working_folder(app, env):
    from app.utils.published import unpublish_working_folder_recordings
    run = BulkIngestRun(root=str(env.incoming), status="done", mode="hold")
    _db.session.add(run)
    _db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="Held", status="review", reason="in_working_folder",
                        duplicate_of=1, detail="workshop")
    _db.session.add(it)
    _db.session.commit()
    unpublish_working_folder_recordings()
    _db.session.expire_all()
    it = _db.session.get(BulkIngestItem, it.id)
    assert (it.status, it.reason, it.duplicate_of) == ("pending", None, None)


def test_confirm_takes_the_folder_from_the_recording_never_the_client(app, env, client, monkeypatch):
    from app.api import ingest as ingest_api
    seen = {}
    monkeypatch.setattr(ingest_api, "_run_ingest_job",
                        lambda job_id, app_, data, uid: seen.update(data))
    _good(env.incoming, "S1")
    rec = _rec_of(_hold_run(env), "S1")
    r = client.post("/api/ingest/confirm", json={"recording_id": rec.id, "artist_name": "Grateful Dead",
                                                  "source_folder_path": "/etc"})
    assert r.status_code == 202, r.get_json()
    import time
    for _ in range(50):
        if seen:
            break
        time.sleep(0.05)
    assert seen["source_folder_path"] == str(env.incoming / "S1")
    lib = _recording("Lib")
    assert client.post("/api/ingest/confirm", json={"recording_id": lib.id, "artist_name": "X"}).status_code == 404
