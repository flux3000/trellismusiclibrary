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


# ── Queue: MusicBrainz lookup for Album rows (2026-10-13) ────────────────────

def _queue_item(app, meta, kind="studio", status="ready"):
    import json
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    run = BulkIngestRun(root="/tmp/src", mode="hold")
    _db.session.add(run); _db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="A", status=status, kind=kind,
                        meta=json.dumps(meta))
    _db.session.add(it); _db.session.commit()
    return it


def test_queue_lookup_found_and_not_found(app, monkeypatch, online):
    import json
    from app.api import ingest
    monkeypatch.setattr(mb, "search_release", lambda a, t, **k: [
        {"mbid": "r1", "title": "Solo Banjo Works", "release_group_id": "g1",
         "track_count": 2, "score": 100}] if t == "Solo Banjo Works" else [])
    hit = _queue_item(app, {"artist": "Fleck", "title": "Solo Banjo Works", "track_count": 2})
    miss = _queue_item(app, {"artist": "Fleck", "title": "Nope", "track_count": 2})
    for it in (hit, miss):
        ingest._handle_mb_queue(it.id)
    assert json.loads(_db.session.get(type(hit), hit.id).meta)["mb"]["found"] is True
    got = json.loads(_db.session.get(type(miss), miss.id).meta)["mb"]
    assert got["found"] is False and got["status"] == "none"


def test_queue_lookup_skips_live_rows_and_offline(app, monkeypatch):
    import json
    from app.api import ingest
    live = _queue_item(app, {"artist": "X", "title": "Y"}, kind="live")
    ingest._handle_mb_queue(live.id)          # MusicBrainz is off under TESTING too
    assert "mb" not in json.loads(_db.session.get(type(live), live.id).meta)
    album = _queue_item(app, {"artist": "X", "title": "Y"})
    ingest._handle_mb_queue(album.id)
    assert "mb" not in json.loads(_db.session.get(type(album), album.id).meta)


def test_followups_queue_unlooked_album_rows(app):
    from app.api import ingest
    album = _queue_item(app, {"artist": "X", "title": "Y"})
    _queue_item(app, {"artist": "X", "title": "Y", "mb": {"found": True}})
    assert _queued(ingest, "mb_queue", ingest.enqueue_followups) == [album.id]


def test_queue_row_fingerprint_field(app, monkeypatch):
    from app.api import bulk_ingest as bi
    from app.utils import checksums
    it = _queue_item(app, {"artist": "X"}, kind="live", status="review")
    for state, shown in (("valid", "valid"), ("invalid", "invalid"), ("unchecked", None), ("absent", None)):
        monkeypatch.setattr(checksums, "folder_fingerprint_state",
                            lambda p, s=state: {"state": s, "detail": ["01.flac"] if s == "invalid" else []})
        got = bi._item_fp_fields(it)
        assert got["fingerprints"] == shown
        assert got["fingerprint_detail"] == (["01.flac"] if shown == "invalid" else [])


def test_queue_found_release_is_passed_to_the_import(app, monkeypatch, tmp_path):
    from app.api import ingest
    from tests.test_resolve import _flac_with_tags
    show = tmp_path / "Fleck - Solo Banjo Works"
    for i in (1, 2):
        _flac_with_tags(show / f"0{i}.flac", ARTIST="Bela Fleck", ALBUM="Solo Banjo Works",
                        TITLE=f"Song {i}", DATE="1992")
    seen = {}
    from app.models.user import User
    uid = User.query.first().id
    monkeypatch.setattr(ingest, "promote_to_library",
                        lambda rec, user_id, payload, **k: seen.update(payload) or {"recording_id": rec.id})
    out = ingest.auto_confirm(str(show), uid, force=True, mb_release_id="r1")
    assert out["status"] == "ingested"
    assert seen["mb_release_id"] == "r1" and seen["mb_release_status"] == "matched"
