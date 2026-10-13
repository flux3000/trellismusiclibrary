"""Already-imported Albums: completeness from tags, then MusicBrainz
(Ryan, 2026-10-13). Network and file reads are faked."""
import pytest

from app.extensions import db as _db
from app.models.track import Track
from app.utils import album_completeness as ac
from app.utils import musicbrainz as mb

from tests.test_studio_library_payloads import _recording, client  # noqa: F401


def _album(n_tracks=2, **kw):
    rec, _artist = _recording("Fleck", kind="studio", title="Solo Banjo Works")
    for i in range(1, n_tracks + 1):
        _db.session.add(Track(recording_id=rec.id, track_number=i, title=f"Song {i}",
                              file_path=f"{i:02d}.flac"))
    for k, v in kw.items():
        setattr(rec, k, v)
    _db.session.commit()
    return rec


def _tags(total):
    def fake(rec, tracks):
        raws = [{"tracktotal": str(total)} for _ in tracks]
        return {"audio_files": [{"filename": t.file_path} for t in tracks],
                "suggestions": {"from_tags": {"tracks": [
                    {"filename": t.file_path, "raw": r} for t, r in zip(tracks, raws)]}}}
    return fake


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "tripped", lambda: False)


def test_tags_short_marks_incomplete_offline(app, monkeypatch):
    monkeypatch.setattr(ac, "_tag_scan", _tags(24))
    rec = _album()
    assert ac.check_album(rec) is True
    assert rec.is_complete is False and rec.completeness_checked_at is not None


def test_no_tag_signal_and_musicbrainz_off_leaves_it_unchecked(app, monkeypatch):
    monkeypatch.setattr(ac, "_tag_scan", lambda rec, tracks: None)
    rec = _album()
    assert ac.check_album(rec) is None
    assert rec.is_complete is True and rec.completeness_checked_at is None


def test_linked_release_longer_than_folder(app, monkeypatch, online):
    monkeypatch.setattr(ac, "_tag_scan", lambda rec, tracks: None)
    monkeypatch.setattr(mb, "lookup_release", lambda mbid: {"tracks": [{}] * 24})
    rec = _album(mb_release_id="r1")
    assert ac.check_album(rec) is True and rec.is_complete is False


def test_unlinked_album_found_in_a_longer_release(app, monkeypatch, online):
    monkeypatch.setattr(ac, "_tag_scan", lambda rec, tracks: None)
    monkeypatch.setattr(mb, "find_incomplete_release", lambda *a: {"release_tracks": 24})
    rec = _album()
    assert ac.check_album(rec) is True and rec.is_complete is False


def test_complete_album_is_stamped_and_never_set_true(app, monkeypatch, online):
    monkeypatch.setattr(ac, "_tag_scan", lambda rec, tracks: None)
    monkeypatch.setattr(mb, "find_incomplete_release", lambda *a: None)
    rec = _album(is_complete=False)            # a person marked it
    assert ac.check_album(rec) is False
    assert rec.is_complete is False and rec.completeness_checked_at is not None


def test_followups_queue_completeness_only_after_release_lookup(app):
    from app.api import ingest
    rec = _album()
    assert rec.id not in _queued(ingest, "completeness", ingest.enqueue_followups)
    rec.mb_release_status = "none"
    _db.session.commit()
    assert rec.id in _queued(ingest, "completeness", ingest.enqueue_followups)


def _queued(ingest, kind, enqueue):
    seen = []
    orig = ingest._enqueue
    ingest._enqueue = lambda app, k, item_id, run_id=None: seen.append((k, item_id)) or True
    try:
        enqueue()
    finally:
        ingest._enqueue = orig
    return [i for k, i in seen if k == kind]


def test_manual_find_release_ignores_track_count(app, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "search_release", lambda *a, **k: [
        {"mbid": "r1", "title": "Solo Banjo Works", "release_group_id": "g1",
         "track_count": 24, "score": 100}])
    rec = _album()
    got = client.get(f"/api/recordings/{rec.id}/release-candidates").get_json()
    assert [c["mbid"] for c in got["candidates"]] == ["r1"]
