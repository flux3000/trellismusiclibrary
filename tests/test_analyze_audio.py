"""
tests/test_analyze_audio.py -- the Analyze Audio button (renamed from
"Re-Analyze Tracks"): POST /api/recordings/<id>/reprocess now runs the whole
audio pass with the score, for a live recording AND an album, and no longer
takes the librosa-only path.
"""

import pytest

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.quality import RecordingQuality
from app.models.user import User
from app.api import ingest as ingest_api


@pytest.fixture()
def client(app):
    user = _db.session.query(User).filter_by(username="admin").first()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _rec(perf_id, path, kind):
    rec = Recording(performance_id=perf_id, folder_path=path,
                    is_complete=True, is_official=False, kind=kind)
    _db.session.add(rec)
    _db.session.commit()
    return rec


@pytest.mark.parametrize("kind", ["live", "studio"])
def test_endpoint_runs_full_audio_pass_with_score(app, client, monkeypatch, seeded_ids, kind):
    rec = _rec(seeded_ids["performance_id"], f"X/{kind}", kind)
    calls = []

    def _fake_pass(rid, score=True, reanalyze=False):
        calls.append((rid, score, reanalyze))
        _db.session.add(RecordingQuality(recording_id=rid, listening_quality=64.0))
        _db.session.commit()
        return {"score": "ok", "signals": "ok", "analysis": "ok", "errors": []}
    monkeypatch.setattr(ingest_api, "run_audio_pass", _fake_pass)

    r = client.post(f"/api/recordings/{rec.id}/reprocess")
    assert r.status_code == 200
    body = r.get_json()
    assert calls == [(rec.id, True, True)]
    # Same shape the frontend already reads, plus the new score.
    assert body["analysed"] == 0 and body["errors"] == []
    assert body["score"] == "ok" and body["listening_quality"] == 64.0


def test_endpoint_never_calls_the_librosa_only_path(app, client, monkeypatch, seeded_ids):
    rec = _rec(seeded_ids["performance_id"], "X/nolibrosaonly", "live")
    import app.api.recordings as recordings_api
    assert not hasattr(recordings_api, "analyse_recording")
    monkeypatch.setattr(ingest_api, "run_audio_pass",
                        lambda rid, score=True, reanalyze=False:
                        {"score": "failed", "signals": "ok", "analysis": "ok",
                         "errors": ["score: no audio"]})
    r = client.post(f"/api/recordings/{rec.id}/reprocess")
    assert r.status_code == 200
    assert r.get_json()["errors"] == ["score: no audio"]


def test_endpoint_500_when_every_part_fails_and_404_for_unknown(app, client, monkeypatch, seeded_ids):
    rec = _rec(seeded_ids["performance_id"], "X/allfail", "live")
    monkeypatch.setattr(ingest_api, "run_audio_pass",
                        lambda rid, score=True, reanalyze=False:
                        {"score": "failed", "signals": "failed", "analysis": "failed",
                         "errors": ["x"]})
    assert client.post(f"/api/recordings/{rec.id}/reprocess").status_code == 500
    assert client.post("/api/recordings/999999/reprocess").status_code == 404


def test_audio_pass_is_serialised_per_recording(app, monkeypatch, seeded_ids):
    import threading, time
    rec = _rec(seeded_ids["performance_id"], "X/lock", "live")
    active, peak = [0], [0]

    def slow(rid, score=True, reanalyze=False):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return {"score": "ok", "signals": "ok", "analysis": "ok", "errors": []}
    monkeypatch.setattr(ingest_api, "_run_audio_pass", slow)
    ts = [threading.Thread(target=ingest_api.run_audio_pass, args=(rec.id,)) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


def test_reprocess_drops_queued_audio_item(app, client, monkeypatch, seeded_ids):
    rec = _rec(seeded_ids["performance_id"], "X/queued", "live")
    monkeypatch.setattr(ingest_api, "run_audio_pass",
                        lambda rid, score=True, reanalyze=False:
                        {"score": "ok", "signals": "ok", "analysis": "ok", "errors": []})
    key = ("audio", rec.id)
    ingest_api._LANES["audio"].put(key, None)
    with ingest_api._Q_LOCK:
        ingest_api._QUEUED_KEYS.add(key)
        ingest_api._PENDING_BY_KIND["audio"] = ingest_api._PENDING_BY_KIND.get("audio", 0) + 1
    try:
        assert client.post(f"/api/recordings/{rec.id}/reprocess").status_code == 200
        assert key not in ingest_api._QUEUED_KEYS
        assert ingest_api._LANES["audio"].qsize() == 0
    finally:
        ingest_api._LANES["audio"].discard(key)
        ingest_api._QUEUED_KEYS.discard(key)
