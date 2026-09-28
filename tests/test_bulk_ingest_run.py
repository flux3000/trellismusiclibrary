"""
tests/test_bulk_ingest_run.py -- Bulk Ingest worker (app/utils/bulk_ingest_run.py),
spec chunk 5b/5c/5e.

Real files under a temp LIBRARY_ROOT, run through discover()/process()
directly with a stop_flag that never fires -- no thread, no app.config
LIBRARY_ROOT, the run object's own `.root` drives everything.
"""

import hashlib
import os

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.quality import QualityAnalysis
from app.models.performance import Performance
from app.utils import bulk_ingest_run
from app.utils import node_settings


def _never_stop():
    return False


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    # A real-world FLAC always carries TRACKNUMBER; Track.track_number is
    # NOT NULL, so a synthetic fixture needs one too unless the test
    # supplies its own.
    tags.setdefault("TRACKNUMBER", "1")
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="WAV")


def _snapshot_tree(root):
    """(relpath, is_dir) for everything under root, for a before/after
    filesystem-untouched comparison."""
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out.add((os.path.relpath(os.path.join(dirpath, d), root), True))
        for f in filenames:
            out.add((os.path.relpath(os.path.join(dirpath, f), root), False))
    return out


def _build_tree(root):
    """
    One folder of every shape the spec table cares about. Returns the set of
    rel_paths that SHOULD have become BulkIngestItem rows (i.e. everything
    resolve_shows_in_dir considers a show).
    """
    # Clean live show: full date + venue -> ingested
    _flac(root / "CleanShow" / "01.flac",
          ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Barton Hall")

    # Year + venue, no month/day -> ingested (year+venue rule)
    _flac(root / "YearVenueShow" / "01.flac",
          ARTIST="Phish", DATE="1995", VENUE="The Gorge")

    # Year only, no venue -> review / needs_date
    _flac(root / "YearOnlyNoVenueShow" / "01.flac",
          ARTIST="Phish", DATE="1995")

    # No date at all -> review / needs_date
    _flac(root / "NoDateShow" / "01.flac", ARTIST="Phish")

    # Dateless studio album (ALBUM tag, no month anywhere) -> ingested / studio
    _flac(root / "StudioAlbum" / "01.flac",
          ARTIST="Phish", ALBUM="A Picture of Nectar", TRACKNUMBER="1")
    _flac(root / "StudioAlbum" / "02.flac",
          ARTIST="Phish", ALBUM="A Picture of Nectar", TRACKNUMBER="2")

    # No artist anywhere (has a date) -> review / needs_artist
    _flac(root / "NoArtistShow" / "01.flac", DATE="1980-01-01")

    # .wav only -> review / unsupported_format
    _wav(root / "WavOnlyShow" / "01.wav")

    # Empty folder -> not a show at all (resolve_shows_in_dir drops an
    # audio-less folder merely encountered while walking); never becomes an
    # BulkIngestItem.
    (root / "EmptyShow").mkdir(parents=True)

    # Trellis's own namespace -- never walked into, never an item.
    (root / "_venues" / "X" / "_images").mkdir(parents=True)
    (root / "_venues" / "X" / "_images" / "a.jpg").write_bytes(b"\xff\xd8\xff")

    # Dot-prefixed -- same treatment.
    (root / ".hidden").mkdir(parents=True)
    (root / ".hidden" / "01.flac").write_bytes(b"not real")

    # CD1/CD2 nested show -- ONE show, ONE recording, continuous track
    # numbering across discs.
    _flac(root / "MultiDiscShow" / "CD1" / "01.flac",
          ARTIST="Bela Fleck", DATE="1998-03-01", VENUE="The Fillmore", TRACKNUMBER="1")
    _flac(root / "MultiDiscShow" / "CD2" / "01.flac",
          ARTIST="Bela Fleck", DATE="1998-03-01", VENUE="The Fillmore", TRACKNUMBER="1")

    return {
        "CleanShow", "YearVenueShow", "YearOnlyNoVenueShow", "NoDateShow",
        "StudioAlbum", "NoArtistShow", "WavOnlyShow", "MultiDiscShow",
    }


def test_discover_and_process_full_tree(app, tmp_path, seeded_ids, monkeypatch):
    root = tmp_path / "Library"
    root.mkdir()
    expected_show_rels = _build_tree(root)

    # A folder already present as a Recording (folder_path matches exactly).
    already = root / "AlreadyIngested"
    _flac(already / "01.flac", ARTIST="Bill Evans", DATE="1980-02-22")
    _db.session.add(Recording(
        performance_id=seeded_ids["performance_id"],
        folder_path="AlreadyIngested",
        is_complete=True, is_official=False,
    ))
    _db.session.commit()

    before = _snapshot_tree(root)

    # _do_confirm's in-root-source check (app/api/ingest.py) reads
    # LIBRARY_ROOT off current_app.config, not off the run object -- in
    # production these always agree (the /api/bulk-ingest/start route passes
    # current_app.config["LIBRARY_ROOT"] straight into start_run()), so the
    # test has to set the same config the route would.
    app.config["LIBRARY_ROOT"] = str(root)

    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()

    # MusicBrainz must never be called for a bulk ingest.
    from app.utils import musicbrainz as _mb
    def _boom(*a, **k):
        raise AssertionError("MusicBrainz must not be called during bulk ingest")
    orig = _mb.try_match_artist
    _mb.try_match_artist = _boom
    try:
        # R2-N4: process() reaching 'done' starts the real follow-up thread
        # (enqueue_followups -> _enqueue) against this test's temp DB. Left
        # unpatched, that background thread keeps running after the test (and
        # its session/engine) tear down, and has intermittently logged a
        # swallowed OperationalError on a later test's stderr.
        monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id: True)
        bulk_ingest_run.discover(run)
        bulk_ingest_run.process(run, _never_stop)
    finally:
        _mb.try_match_artist = orig

    after = _snapshot_tree(root)
    assert before == after, "bulk ingest must never touch the filesystem"

    items = {it.rel_path: it for it in
             _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all()}

    # Every genuine show folder became exactly one item; nothing else did.
    assert set(items) == expected_show_rels | {"AlreadyIngested"}

    assert items["CleanShow"].status == "ingested"
    assert items["CleanShow"].kind == "live"
    assert items["CleanShow"].recording_id is not None
    rec = _db.session.get(Recording, items["CleanShow"].recording_id)
    assert rec.kind == "live"

    assert items["YearVenueShow"].status == "ingested"

    assert items["YearOnlyNoVenueShow"].status == "review"
    assert items["YearOnlyNoVenueShow"].reason == "needs_date"

    assert items["NoDateShow"].status == "review"
    assert items["NoDateShow"].reason == "needs_date"

    assert items["StudioAlbum"].status == "ingested"
    assert items["StudioAlbum"].kind == "studio"
    studio_rec = _db.session.get(Recording, items["StudioAlbum"].recording_id)
    assert studio_rec.kind == "studio"
    assert studio_rec.title == "A Picture of Nectar"

    assert items["NoArtistShow"].status == "review"
    assert items["NoArtistShow"].reason == "needs_artist"

    assert items["WavOnlyShow"].status == "review"
    assert items["WavOnlyShow"].reason == "unsupported_format"

    assert items["MultiDiscShow"].status == "ingested"
    disc_rec = _db.session.get(Recording, items["MultiDiscShow"].recording_id)
    assert len(disc_rec.tracks) == 2
    # Both discs' lone file is tagged TRACKNUMBER=1 -- ingested track_number
    # must be the scan's continuous index (1, 2), never the duplicate raw
    # tag, and the disc fields must be set.
    disc_tracks = sorted(disc_rec.tracks, key=lambda t: t.track_number)
    assert [t.track_number for t in disc_tracks] == [1, 2]
    assert [t.disc_number for t in disc_tracks] == [1, 2]
    assert [t.disc_track_number for t in disc_tracks] == [1, 1]
    # B1: a subdir-carrier show (CD1/CD2) must ingest with a file_path that
    # is the file's rel_path WITHIN the show folder (e.g. "CD1/01.flac"),
    # not the bare basename -- otherwise library_root/folder_path/file_path
    # points at nothing and the track can never stream or verify.
    for t in disc_tracks:
        assert "/" in t.file_path
        assert os.path.isfile(os.path.join(root, disc_rec.folder_path, t.file_path))

    assert items["AlreadyIngested"].status == "skipped"
    assert items["AlreadyIngested"].reason == "already_in_library"
    assert items["AlreadyIngested"].recording_id is None  # never touched

    # Review items got a QualityAnalysis staging row, source_dir == the run's
    # root, scores left null.
    for rel in ("YearOnlyNoVenueShow", "NoDateShow", "NoArtistShow", "WavOnlyShow"):
        folder_abs = str(root / rel)
        from app.utils.quality_store import norm_path
        row = (_db.session.query(QualityAnalysis)
               .filter_by(folder_path=norm_path(folder_abs)).first())
        assert row is not None, f"no staging row for {rel}"
        assert row.source_dir == norm_path(str(root))
        assert row.listening_quality is None
        assert row.triage_status == "pending"

    run = _db.session.get(BulkIngestRun, run.id)
    assert run.status == "done"
    assert run.finished_at is not None


def test_staging_endpoint_tolerates_null_scores(app, tmp_path, seeded_ids, monkeypatch):
    root = tmp_path / "Library2"
    root.mkdir()
    _flac(root / "NoDateShow" / "01.flac", ARTIST="Phish")
    app.config["LIBRARY_ROOT"] = str(root)

    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    # R2-N4: process() reaching 'done' starts the real follow-up thread
    # (enqueue_followups -> _enqueue) against this test's temp DB. Left
    # unpatched, that background thread keeps running after the test (and
    # its session/engine) tear down, and has intermittently logged a
    # swallowed OperationalError on a later test's stderr.
    monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id: True)
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    app.config["LOGIN_DISABLED"] = True
    client = app.test_client()
    from app.models.user import User
    user = _db.session.query(User).filter_by(username="admin").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    resp = client.get("/api/quality/staging", query_string={"source_dir": str(root)})
    assert resp.status_code == 200
    body = resp.get_json()
    assert any(r["listening_quality"] is None for r in body["results"])


def test_do_confirm_promote_error_is_not_masked_by_pendingrollback(app, tmp_path, seeded_ids):
    """N5: a real exception during promote_to_recording's own flush (here,
    RecordingEvent.user_id NOT NULL with no admin user to own the event)
    used to be swallowed and printed, after which the very next line's
    db.session.commit() raised an UNRELATED PendingRollbackError from the
    now-dirty session -- and THAT masking error is what every caller
    (bulk_ingest's per-item failure detail included) actually saw. The real
    cause must propagate instead."""
    from app.api.ingest import _do_confirm
    from app.utils import quality_store as qs

    root = tmp_path / "Library3"
    root.mkdir()
    _flac(root / "Show1" / "01.flac", ARTIST="A", DATE="1977-05-08", VENUE="V")
    app.config["LIBRARY_ROOT"] = str(root)

    folder_path = str(root / "Show1")
    qs.upsert_staging(folder_path, source_dir=str(root),
                      scored={"listening_quality": 77}, features={})

    # user_id=None, the same value _owner_user_id() hands _do_confirm when
    # no admin exists to own the RecordingEvent.
    payload = {
        "source_folder_path": folder_path, "artist_name": "A",
        "start_year": 1977, "start_month": 5, "start_day": 8,
        "venue_name": "V", "is_complete": True, "skip_analysis": True,
        "tracks": [{"track_number": 1, "title": "T", "filename": "01.flac"}],
    }

    with pytest.raises(Exception) as exc_info:
        _do_confirm(payload, None, bulk=True)
    _db.session.rollback()

    message = str(exc_info.value)
    assert "PendingRollbackError" not in message
    assert "user_id" in message or "NOT NULL" in message


def _sha256_all(root):
    """{relpath: sha256} for every regular file under root, for a
    byte-for-byte before/after comparison (S9: bulk ingest must never
    write inside the collector's library, even with write_tags_on_ingest
    on)."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            abs_path = os.path.join(dirpath, f)
            rel = os.path.relpath(abs_path, root)
            with open(abs_path, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_bulk_ingest_never_writes_tags_even_with_switch_on(app, tmp_path, seeded_ids, monkeypatch):
    """S9: write_tags_on_ingest is the interactive-confirm switch only. With
    'organize' mode on (switch True by default) and FLACs whose tags don't
    match what a write would produce, a bulk ingest run must leave every
    file byte-for-byte untouched and must never log a tags_written event."""
    node_settings.apply_mode("organize")
    assert node_settings.get_file_handling()["write_tags_on_ingest"] is True

    root = tmp_path / "Library"
    root.mkdir()
    # Artist/date/venue consistent with the folder so the show ingests; a
    # wrong TITLE is the tell-tale a tag write would have corrected.
    _flac(root / "CleanShow" / "01.flac",
          ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Barton Hall",
          TITLE="Deliberately Wrong Title", TRACKNUMBER="1")

    before = _sha256_all(root)

    app.config["LIBRARY_ROOT"] = str(root)
    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()

    # R2-N4: process() reaching 'done' starts the real follow-up thread
    # (enqueue_followups -> _enqueue) against this test's temp DB. Left
    # unpatched, that background thread keeps running after the test (and
    # its session/engine) tear down, and has intermittently logged a
    # swallowed OperationalError on a later test's stderr.
    monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id: True)
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    after = _sha256_all(root)
    assert before == after, "bulk ingest must not write tags, even with write_tags_on_ingest on"

    item = (_db.session.query(BulkIngestItem)
            .filter_by(run_id=run.id, rel_path="CleanShow").first())
    assert item is not None
    assert item.status == "ingested"
    assert item.recording_id is not None

    events = (_db.session.query(RecordingEvent)
              .filter_by(recording_id=item.recording_id, event_type="tags_written")
              .all())
    assert events == []
