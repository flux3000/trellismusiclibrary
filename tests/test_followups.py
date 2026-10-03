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
    _LANES / _QUEUED_KEYS / _PENDING_BY_KIND are module-level state in
    app/api/ingest.py, shared by the whole test process -- including any
    earlier test that made a REAL _enqueue() call and left the
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
    monkeypatch.setattr(ingest_api, "_LANES",
                        {"audio": ingest_api._Lane(), "net": ingest_api._Lane()})
    monkeypatch.setattr(ingest_api, "_QUEUED_KEYS", set())
    monkeypatch.setattr(ingest_api, "_PENDING_BY_KIND",
                        {"audio": 0, "mb_artist": 0, "mb_release": 0, "images": 0})
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


def _lane_sizes():
    return {n: l.qsize() for n, l in ingest_api._LANES.items()}


def _fake_score(features, source=None):
    return {"listening_quality": 55.5, "score_tone": 1.0, "score_noise": 1.0,
            "score_dynamics": 1.0, "technical_deduction": 0.0,
            "technical_issues": [], "flags": [], "score_version": "test"}


def _drain(lane=None):
    """Synchronously pop and handle every queued item (no worker thread)."""
    out = []
    for name, ln in list(ingest_api._LANES.items()):
        if lane and name != lane:
            continue
        while True:
            try:
                kind, item_id = ln.get_nowait()
            except queue.Empty:
                break
            ingest_api._QUEUED_KEYS.discard((kind, item_id))
            out.append((kind, item_id))
            ingest_api._handle_item(kind, item_id)
    return out


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
    # All four live recordings (rec_scored, rec_a, rec_b, the seeded one) have
    # no track with a non_music_score yet, so each is owed an audio pass even
    # though rec_scored already has a score; "images": none was scanned.
    assert counts == {"audio": 4, "mb_artist": 1, "mb_release": 0, "images": 4}

    all_recs = [rec_scored, rec_a, rec_b]
    expected = ({("audio", r.id) for r in all_recs} | {("audio", seeded_ids["recording_id"])}
                | {("images", r.id) for r in all_recs} | {("images", seeded_ids["recording_id"])}
                | {("mb_artist", artist_never.id)})
    assert set(ingest_api._QUEUED_KEYS) == expected
    assert _lane_sizes() == {"audio": 4, "net": 5}

    # A second call: everything above is still waiting, nothing new.
    again = ingest_api.enqueue_followups()
    assert again == {"audio": 0, "mb_artist": 0, "mb_release": 0, "images": 0}
    assert _lane_sizes() == {"audio": 4, "net": 5}

    # Stub the scorer chain and the librosa pass so no real audio is needed.
    monkeypatch.setattr("app.utils.quality.extract_recording_features",
                        lambda folder_abs: {"fake": True})
    monkeypatch.setattr("app.utils.quality.score_recording", _fake_score)
    monkeypatch.setattr("app.utils.analysis.analyse_recording",
                        lambda rec, root, sess, reanalyze=True: (0, []))

    mb_calls = []
    orig_try_match = _mb.try_match_artist

    def _spy_try_match(artist):
        mb_calls.append(artist.id)
        return orig_try_match(artist)

    monkeypatch.setattr(_mb, "try_match_artist", _spy_try_match)

    drained = _drain()
    assert set(drained) == expected

    assert _db.session.query(RecordingQuality).filter_by(recording_id=rec_a.id).first() is not None
    assert _db.session.query(RecordingQuality).filter_by(recording_id=rec_b.id).first() is not None

    # The MusicBrainz handler ran exactly once, and -- because app.config
    # TESTING=True makes musicbrainz.enabled() False -- try_match_artist's
    # own gate returned immediately with no network call, leaving mb_status
    # untouched (still "never looked up", not "none" or "matched").
    assert mb_calls == [artist_never.id]
    _db.session.refresh(artist_never)
    assert artist_never.mb_status is None


def _live(perf_id, path, kind="live"):
    rec = Recording(performance_id=perf_id, folder_path=path,
                    is_complete=True, is_official=False, kind=kind)
    _db.session.add(rec)
    _db.session.commit()
    return rec


def test_studio_recording_gets_no_audio_followup_and_handler_skips_it(app, monkeypatch, seeded_ids):
    """Albums are never auto-scored or analysed: not enqueued, and the handler
    refuses one even if something queued it anyway."""
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]
    studio_rec = _live(perf_id, "X/StudioAlbum", kind="studio")
    live_rec = _live(perf_id, "X/LiveShow")

    counts = ingest_api.enqueue_followups()
    assert counts["audio"] == 2          # the seeded live recording + live_rec
    assert ("audio", studio_rec.id) not in ingest_api._QUEUED_KEYS
    assert ("audio", live_rec.id) in ingest_api._QUEUED_KEYS
    # The studio recording still gets its MusicBrainz release follow-up.
    assert ("mb_release", studio_rec.id) in ingest_api._QUEUED_KEYS

    def _boom(*a, **k):
        raise AssertionError("audio pass must not run for a studio recording")
    monkeypatch.setattr(ingest_api, "run_audio_pass", _boom)
    ingest_api._handle_audio(studio_rec.id)
    assert _db.session.query(RecordingQuality).filter_by(recording_id=studio_rec.id).first() is None


def test_audio_handler_scores_when_no_current_score(app, monkeypatch, seeded_ids):
    _neutralize_seed(seeded_ids)
    rec = _live(seeded_ids["performance_id"], "X/NeedsScore")
    calls = []
    monkeypatch.setattr(ingest_api, "run_audio_pass",
                        lambda rid, score=True, reanalyze=False: calls.append((rid, score))
                        or {"errors": []})
    ingest_api._handle_audio(rec.id)
    assert calls == [(rec.id, True)]


def test_audio_handler_skips_scoring_but_runs_signals_and_analysis(app, monkeypatch, seeded_ids):
    """Review First scored the folder pre-ingest: the score part is skipped,
    the signal and Full analysis still run."""
    _neutralize_seed(seeded_ids)
    rec = _live(seeded_ids["performance_id"], "X/AlreadyScored")
    _db.session.add(RecordingQuality(recording_id=rec.id, listening_quality=71.0))
    _db.session.commit()

    def _no_score(folder_abs):
        raise AssertionError("must not rescore a recording with a current score")
    monkeypatch.setattr("app.utils.quality.extract_recording_features", _no_score)
    signal_calls, analysis_calls = [], []
    monkeypatch.setattr(ingest_api, "_store_non_music_signal",
                        lambda tracks, root, folder: signal_calls.append(folder))
    monkeypatch.setattr("app.utils.analysis.analyse_recording",
                        lambda r, root, sess, reanalyze=True: analysis_calls.append(r.id) or (0, []))

    ingest_api._handle_audio(rec.id)
    assert signal_calls == ["X/AlreadyScored"]
    assert analysis_calls == [rec.id]


def test_audio_pass_one_failing_part_does_not_stop_the_others(app, monkeypatch, seeded_ids):
    _neutralize_seed(seeded_ids)
    rec = _live(seeded_ids["performance_id"], "X/PartlyBroken")

    def _boom(folder_abs):
        raise RuntimeError("decode exploded")
    monkeypatch.setattr("app.utils.quality.extract_recording_features", _boom)
    ran = []
    monkeypatch.setattr(ingest_api, "_store_non_music_signal",
                        lambda tracks, root, folder: ran.append("signals"))
    monkeypatch.setattr("app.utils.analysis.analyse_recording",
                        lambda r, root, sess, reanalyze=True: ran.append("analysis") or (3, []))

    res = ingest_api.run_audio_pass(rec.id, score=True)
    assert res["score"] == "failed"
    assert res["signals"] == "ok" and res["analysis"] == "ok"
    assert ran == ["signals", "analysis"]
    assert any("decode exploded" in e for e in res["errors"])

    # And the other direction: signals blowing up still lets analysis run.
    ran.clear()
    monkeypatch.setattr("app.utils.quality.extract_recording_features",
                        lambda folder_abs: {"error": "no audio"})

    def _sig_boom(tracks, root, folder):
        raise RuntimeError("flatness exploded")
    monkeypatch.setattr(ingest_api, "_store_non_music_signal", _sig_boom)
    res = ingest_api.run_audio_pass(rec.id, score=True)
    assert res["signals"] == "failed" and res["analysis"] == "ok"
    assert ran == ["analysis"]


def test_audio_pass_unreachable_audio_writes_no_score_row(app, seeded_ids):
    """Folder not on disk: no junk RecordingQuality row, nothing raised."""
    _neutralize_seed(seeded_ids)
    rec = _live(seeded_ids["performance_id"], "Nowhere/Nothing")
    ingest_api._handle_audio(rec.id)
    assert _db.session.query(RecordingQuality).filter_by(recording_id=rec.id).first() is None


def test_audio_handler_never_calls_checksum_verify(app, monkeypatch, seeded_ids):
    """MD5 is never automatic (Ryan, 2026-09-27)."""
    _neutralize_seed(seeded_ids)
    rec = _live(seeded_ids["performance_id"], "X/NoChecksumCall")
    from app.api import recordings as recordings_api

    def _boom(*a, **kw):
        raise AssertionError("checksum verify must never run from the audio pass")
    monkeypatch.setattr(recordings_api, "_rematch_and_verify_checksums", _boom)
    ingest_api._handle_audio(rec.id)


def test_legacy_kinds_dedupe_to_audio_and_do_not_crash(app, monkeypatch, seeded_ids):
    _neutralize_seed(seeded_ids)
    rec = _live(seeded_ids["performance_id"], "X/Legacy")
    app_obj = app
    for kind in ("analysis", "score", "signals"):
        ingest_api._enqueue(app_obj, kind, rec.id)
    assert ingest_api._QUEUED_KEYS == {("audio", rec.id)}
    assert _lane_sizes() == {"audio": 1, "net": 0}

    calls = []
    monkeypatch.setattr(ingest_api, "run_audio_pass",
                        lambda rid, score=True, reanalyze=False: calls.append(rid) or {"errors": []})
    ingest_api._handle_item("score", rec.id)   # a straggler of an old kind
    ingest_api._handle_item("nonsense", 1)     # unknown kinds are logged, not raised
    assert calls == [rec.id]


def test_small_run_followups_come_before_a_large_runs_backlog(app, seeded_ids):
    _neutralize_seed(seeded_ids)
    perf_id = seeded_ids["performance_id"]
    big = BulkIngestRun(root="/big", status="running")
    small = BulkIngestRun(root="/small", status="running")
    _db.session.add_all([big, small])
    _db.session.flush()
    big_recs = [_live(perf_id, f"Big/{i}") for i in range(4)]
    small_rec = _live(perf_id, "Small/0")
    for i, r in enumerate(big_recs):
        _db.session.add(BulkIngestItem(run_id=big.id, rel_path=f"Big/{i}",
                                       status="ingested", recording_id=r.id))
    _db.session.add(BulkIngestItem(run_id=small.id, rel_path="Small/0",
                                   status="ingested", recording_id=small_rec.id))
    _db.session.commit()

    ingest_api.enqueue_followups()
    # The small run's recording was ingested LAST (highest id) but is served
    # first from the audio lane; the big run then drains in arrival order.
    order = []
    lane = ingest_api._LANES["audio"]
    while lane.qsize():
        order.append(lane.get_nowait()[1])
    big_ids = [r.id for r in big_recs]
    # (the seeded recording belongs to no run, so it is first of all)
    assert order[0] == seeded_ids["recording_id"]
    assert order[1] == small_rec.id
    assert [i for i in order if i in big_ids] == big_ids

    # A recording with no run at all goes ahead of everything.
    ingest_api._enqueue(app, "audio", 999001, run_id=big.id)
    ingest_api._enqueue(app, "audio", 999002, run_id=None)
    assert lane.get_nowait() == ("audio", 999002)


def test_do_confirm_has_no_inline_musicbrainz_or_signal_call(app, monkeypatch, seeded_ids):
    """Add Recording's single form (bulk=False) used to run both inline."""
    import numpy as np
    import soundfile as sf
    import tempfile, os

    _neutralize_seed(seeded_ids)

    def _boom(*a, **k):
        raise AssertionError("must be a follow-up, never inline")
    monkeypatch.setattr(_mb, "try_match_artist", _boom)
    monkeypatch.setattr(_mb, "try_match_release", _boom)
    monkeypatch.setattr(ingest_api, "_store_non_music_signal", _boom)
    monkeypatch.setattr("app.utils.artists._mb_try_match", _boom, raising=False)

    src = tempfile.mkdtemp()
    sf.write(os.path.join(src, "01.flac"), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    root = tempfile.mkdtemp()
    app.config["LIBRARY_ROOT"] = root
    data = {
        "source_folder_path": src, "artist_name": "Brand New Inline Band",
        "performance_date": "1977-05-08", "venue_name": "V", "is_complete": True,
        "tracks": [{"track_number": 1, "title": "T", "filename": "01.flac"}],
    }
    result = ingest_api._do_confirm(data, 1)
    artist = _db.session.get(Artist, result["artist_id"])
    assert artist.mb_status is None      # left for the mb_artist follow-up
    ingest_api.enqueue_followups()
    assert ("mb_artist", artist.id) in ingest_api._QUEUED_KEYS
    assert ("audio", result["recording_id"]) in ingest_api._QUEUED_KEYS


def test_pipeline_reports_pending_by_kind(app, seeded_ids):
    app.config["LOGIN_DISABLED"] = True
    _neutralize_seed(seeded_ids)
    _live(seeded_ids["performance_id"], "X/PipelineCheck")

    counts = ingest_api.enqueue_followups()
    assert counts["audio"] == 2 and counts["images"] == 2

    body = app.test_client().get("/api/ingest/pipeline").get_json()
    by_kind = body["analysis"]["pending_by_kind"]
    assert by_kind["audio"] == 2
    assert by_kind["mb_artist"] == 0
    assert by_kind["images"] == 2
    assert body["analysis"]["pending"] == 4


def test_bulk_ingest_run_completion_enqueues_audio_for_every_ingested_recording(app, tmp_path, seeded_ids):
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

    assert ("audio", item.recording_id) in ingest_api._QUEUED_KEYS
