"""
tests/test_followups.py -- Bulk Ingest follow-up queue (spec section 1.6,
chunk 6): the typed queue in app/api/ingest.py, enqueue_followups(), and the
/api/ingest/pipeline pending_by_kind report.
"""

import queue

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.recording import Recording
from app.models.quality import RecordingQuality
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.api import ingest as ingest_api
from app.utils import musicbrainz as _mb


@pytest.fixture(autouse=True)
def _isolate_followup_queue(monkeypatch):
    """
    _ANALYSIS_Q / _QUEUED_KEYS / _PENDING_BY_KIND are module-level state in
    app/api/ingest.py, shared by the whole test process -- including any
    earlier test that made a REAL _enqueue_analysis() call and left the
    single background worker thread running against an app object that
    test's own fixture has since torn down (its sqlite file unlinked). That
    thread is blocked inside a blocking Queue.get() on the OLD queue object;
    swapping in FRESH objects here (rather than mutating the shared ones)
    means every call this test makes routes through queues that zombie
    thread was never given, so it can never race this test's own
    synchronous draining -- it just stays parked on the queue nobody will
    ever put anything into again.

    Forcing "worker already started" additionally stops _enqueue() from
    spinning up ANOTHER real thread against this test's own (about to be
    torn down) app object.
    """
    monkeypatch.setattr(ingest_api, "_ANALYSIS_Q", queue.Queue())
    monkeypatch.setattr(ingest_api, "_QUEUED_KEYS", set())
    monkeypatch.setattr(ingest_api, "_PENDING_BY_KIND",
                        {"analysis": 0, "score": 0, "mb_artist": 0, "mb_release": 0, "images": 0})
    orig_worker = ingest_api._ANALYSIS_STATE["worker"]
    ingest_api._ANALYSIS_STATE["worker"] = True
    yield
    ingest_api._ANALYSIS_STATE["worker"] = orig_worker


def _neutralize_seed(seeded_ids):
    """
    conftest's seeded recording/artist would otherwise also count as
    "unscored" / "never looked up" and pollute the counts these tests check
    for their OWN fixtures.
    """
    _db.session.add(RecordingQuality(recording_id=seeded_ids["recording_id"],
                                     listening_quality=80.0))
    artist = _db.session.get(Artist, seeded_ids["artist_id"])
    artist.mb_status = "matched"
    _db.session.commit()


def test_enqueue_followups_counts_dedup_and_drain(app, monkeypatch, seeded_ids):
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]

    rec_scored = Recording(performance_id=perf_id, folder_path="X/Scored",
                           is_complete=True, is_official=False)
    rec_a = Recording(performance_id=perf_id, folder_path="X/UnscoredA",
                      is_complete=True, is_official=False)
    rec_b = Recording(performance_id=perf_id, folder_path="X/UnscoredB",
                      is_complete=True, is_official=False)
    _db.session.add_all([rec_scored, rec_a, rec_b])
    _db.session.flush()
    _db.session.add(RecordingQuality(recording_id=rec_scored.id, listening_quality=70.0))

    artist_never = Artist(name="Never Looked Up Band")
    artist_matched = Artist(name="Matched Band", mb_status="matched")
    _db.session.add_all([artist_never, artist_matched])
    _db.session.commit()

    counts = ingest_api.enqueue_followups()
    # "images" (S8, independent review v1): every recording in this test's DB
    # (rec_scored, rec_a, rec_b, and the neutralized seeded recording) has
    # zero RecordingImage rows and a null images_checked_at, so all four are
    # queued for the artwork backfill regardless of their "score" status.
    assert counts == {"score": 2, "mb_artist": 1, "mb_release": 0, "images": 4}

    queued = set(ingest_api._QUEUED_KEYS)
    assert queued == {
        ("score", rec_a.id), ("score", rec_b.id),
        ("mb_artist", artist_never.id),
        ("images", rec_scored.id), ("images", rec_a.id), ("images", rec_b.id),
        ("images", seeded_ids["recording_id"]),
    }
    assert not any(kind == "analysis" for kind, _ in queued)
    assert ingest_api._ANALYSIS_Q.qsize() == 7

    # A second call: everything above is still sitting in the queue, so
    # nothing new gets enqueued.
    again = ingest_api.enqueue_followups()
    assert again == {"score": 0, "mb_artist": 0, "mb_release": 0, "images": 0}
    assert ingest_api._ANALYSIS_Q.qsize() == 7

    # Stub the scorer chain so this test needs no real audio on disk --
    # _handle_score imports these two names fresh from app.utils.quality
    # inside its own body, so patching the module attributes is enough.
    def _fake_extract(folder_abs):
        return {"fake": True}

    def _fake_score(features, source=None):
        return {"listening_quality": 55.5, "score_tone": 1.0, "score_noise": 1.0,
                "score_dynamics": 1.0, "technical_deduction": 0.0,
                "technical_issues": [], "flags": [], "score_version": "test"}

    monkeypatch.setattr("app.utils.quality.extract_recording_features", _fake_extract)
    monkeypatch.setattr("app.utils.quality.score_recording", _fake_score)

    mb_calls = []
    orig_try_match = _mb.try_match_artist

    def _spy_try_match(artist):
        mb_calls.append(artist.id)
        return orig_try_match(artist)

    monkeypatch.setattr(_mb, "try_match_artist", _spy_try_match)

    drained = []
    while True:
        try:
            kind, item_id = ingest_api._ANALYSIS_Q.get_nowait()
        except Exception:
            break
        ingest_api._QUEUED_KEYS.discard((kind, item_id))
        drained.append((kind, item_id))
        ingest_api._handle_item(kind, item_id)

    assert set(drained) == {
        ("score", rec_a.id), ("score", rec_b.id),
        ("mb_artist", artist_never.id),
        ("images", rec_scored.id), ("images", rec_a.id), ("images", rec_b.id),
        ("images", seeded_ids["recording_id"]),
    }

    assert _db.session.query(RecordingQuality).filter_by(recording_id=rec_a.id).first() is not None
    assert _db.session.query(RecordingQuality).filter_by(recording_id=rec_b.id).first() is not None

    # The MusicBrainz handler ran exactly once, and -- because app.config
    # TESTING=True makes musicbrainz.enabled() False -- try_match_artist's
    # own gate returned immediately with no network call, leaving mb_status
    # untouched (still "never looked up", not "none" or "matched").
    assert mb_calls == [artist_never.id]
    _db.session.refresh(artist_never)
    assert artist_never.mb_status is None


def test_studio_recording_never_enqueued_and_handler_skips_it(app, seeded_ids):
    """
    Studio recordings are never scored (2026-09-27) -- enqueue_followups()
    must never queue "score" for one, and _handle_score() must return
    immediately (no RecordingQuality row written) even if something else
    enqueued it anyway.
    """
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]

    studio_rec = Recording(performance_id=perf_id, folder_path="X/StudioAlbum",
                           is_complete=True, is_official=False, kind="studio")
    live_rec = Recording(performance_id=perf_id, folder_path="X/LiveShow",
                         is_complete=True, is_official=False, kind="live")
    _db.session.add_all([studio_rec, live_rec])
    _db.session.commit()

    counts = ingest_api.enqueue_followups()
    assert counts["score"] == 1
    assert ("score", studio_rec.id) not in ingest_api._QUEUED_KEYS
    assert ("score", live_rec.id) in ingest_api._QUEUED_KEYS

    # Even called directly (as if something had queued it anyway), the
    # handler's own guard must refuse to score a studio recording.
    ingest_api._handle_score(studio_rec.id)
    assert _db.session.query(RecordingQuality).filter_by(recording_id=studio_rec.id).first() is None


def test_score_handler_no_audio_reachable_writes_no_row(app, seeded_ids):
    """
    A recording whose folder is not on disk (moved, deleted, unmounted
    volume): extract_recording_features() returns {"error": ...} and
    _handle_score() must leave the recording unscored rather than writing a
    junk RecordingQuality row.
    """
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]
    rec = Recording(performance_id=perf_id, folder_path="Nowhere/Nothing",
                    is_complete=True, is_official=False)
    _db.session.add(rec)
    _db.session.commit()

    ingest_api._handle_score(rec.id)

    assert _db.session.query(RecordingQuality).filter_by(recording_id=rec.id).first() is None


def test_score_handler_never_calls_checksum_verify(app, monkeypatch, seeded_ids):
    """
    MD5 is never automatic (Ryan, 2026-09-27): the follow-up "score" handler
    must do the Listening Quality score and the non-music signal ONLY. It
    must never reach for app.api.recordings._rematch_and_verify_checksums --
    that call only exists on the manual Re-validate / verify-checksums
    action on View Recording.
    """
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]
    rec = Recording(performance_id=perf_id, folder_path="X/NoChecksumCall",
                    is_complete=True, is_official=False)
    _db.session.add(rec)
    _db.session.commit()

    from app.utils import quality as quality_utils
    monkeypatch.setattr(quality_utils, "extract_recording_features",
                        lambda folder_abs: {"error": "no audio on disk"})

    called = []

    def _boom(*a, **kw):
        called.append((a, kw))
        raise AssertionError("checksum verify must never run from the score handler")

    from app.api import recordings as recordings_api
    monkeypatch.setattr(recordings_api, "_rematch_and_verify_checksums", _boom)

    ingest_api._handle_score(rec.id)

    assert called == []


def test_pipeline_reports_pending_by_kind(app, seeded_ids):
    app.config["LOGIN_DISABLED"] = True
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]
    rec = Recording(performance_id=perf_id, folder_path="X/PipelineCheck",
                    is_complete=True, is_official=False)
    _db.session.add(rec)
    _db.session.commit()

    counts = ingest_api.enqueue_followups()
    assert counts["score"] == 1
    # "images" (S8): this new recording and the neutralized seeded recording
    # both have zero RecordingImage rows and a null images_checked_at, so
    # both are queued for the artwork backfill regardless of "score".
    assert counts["images"] == 2

    client = app.test_client()
    resp = client.get("/api/ingest/pipeline")
    assert resp.status_code == 200
    body = resp.get_json()
    by_kind = body["analysis"]["pending_by_kind"]
    assert by_kind["score"] == 1
    assert by_kind["mb_artist"] == 0
    assert by_kind["analysis"] == 0
    assert by_kind["images"] == 2
    assert body["analysis"]["pending"] == 3


def test_bulk_ingest_run_completion_enqueues_score_for_every_ingested_recording(app, tmp_path, seeded_ids):
    import numpy as np
    import soundfile as sf
    from mutagen.flac import FLAC
    from app.utils import bulk_ingest_run

    _neutralize_seed(seeded_ids)

    root = tmp_path / "Library"
    root.mkdir()

    def _flac(path, **tags):
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
        audio = FLAC(str(path))
        tags.setdefault("TRACKNUMBER", "1")
        for k, v in tags.items():
            audio[k] = v
        audio.save()

    _flac(root / "CleanShow" / "01.flac",
          ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Barton Hall")

    app.config["LIBRARY_ROOT"] = str(root)

    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()

    # MusicBrainz must never be called synchronously during bulk ingest --
    # same guarantee test_bulk_ingest_run.py checks; enqueue_followups() must
    # only QUEUE mb_artist work, never invoke it.
    def _boom(*a, **k):
        raise AssertionError("MusicBrainz must not be called during bulk ingest")
    orig = _mb.try_match_artist
    _mb.try_match_artist = _boom
    try:
        bulk_ingest_run.discover(run)
        bulk_ingest_run.process(run, lambda: False)
    finally:
        _mb.try_match_artist = orig

    run = _db.session.get(BulkIngestRun, run.id)
    assert run.status == "done"

    item = (_db.session.query(BulkIngestItem)
           .filter_by(run_id=run.id, rel_path="CleanShow").first())
    assert item is not None
    assert item.recording_id is not None

    assert ("score", item.recording_id) in ingest_api._QUEUED_KEYS
