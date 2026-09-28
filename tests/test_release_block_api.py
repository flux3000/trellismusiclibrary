"""
tests/test_release_block_api.py -- Studio Records spec v1 chunk 3: the three
View Recording endpoints (GET .../release-candidates, POST .../release-link,
POST .../release-unlink) that sit on top of app/utils/musicbrainz.py's
already-tested search_release/classify_release/link_release/unlink_release.

Network-free throughout: mb._get() is monkeypatched wherever a real lookup
would otherwise run, same discipline as tests/test_mb_release.py.
"""

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.track import Track
from app.models.user import User
from app.utils import musicbrainz as mb


_LOOKUP_RESPONSE = {
    "id": "rel-mbid-1",
    "title": "Head Hunters",
    "release-group": {"id": "rg-1", "primary-type": "Album"},
    "label-info": [{"label": {"name": "Columbia"}, "catalog-number": "KC 32731"}],
    "date": "1973-10-13",
    "country": "US",
    "media": [{"tracks": [
        {"position": 1, "title": "Chameleon", "recording": {"title": "Chameleon"}},
    ]}],
}

_SEARCH_RESPONSE = {
    "releases": [
        {
            "id": "rel-mbid-1", "title": "Head Hunters", "score": 95,
            "release-group": {"id": "rg-1", "primary-type": "Album"},
            "label-info": [{"label": {"name": "Columbia"}, "catalog-number": "KC 32731"}],
            "date": "1973-10-13", "country": "US", "track-count": 1,
            "media": [{"track-count": 1}],
        },
    ]
}


def _make_studio_recording(label="Head Hunters"):
    artist = _db.session.query(Artist).filter_by(name="Bill Evans").first()
    perf = Performance(artist_id=artist.id, start_year=1973)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, folder_path=f"Bill Evans/{label}",
                    title=label, is_complete=True, is_official=False, kind="studio")
    _db.session.add(rec)
    _db.session.flush()
    _db.session.add(Track(recording_id=rec.id, track_number=1, title="",
                          duration=100, file_path="01.flac"))
    _db.session.commit()
    return rec


def _make_live_recording(label="Show"):
    artist = _db.session.query(Artist).filter_by(name="Bill Evans").first()
    perf = Performance(artist_id=artist.id, start_year=1980)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, folder_path=f"Bill Evans/{label}",
                    is_complete=True, is_official=False, kind="live")
    _db.session.add(rec)
    _db.session.commit()
    return rec


def _login_as(client, username="admin"):
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    probe = client.get("/api/collections/")
    assert probe.status_code == 200, (
        f"login did not take: /api/collections/ returned {probe.status_code}")


# ── GET .../release-candidates ──────────────────────────────────────────────

def test_candidates_503_when_musicbrainz_disabled(app, seeded_ids):
    rec = _make_studio_recording()
    client = app.test_client()
    _login_as(client)
    assert mb.enabled() is False  # under TESTING, no monkeypatching needed
    res = client.get(f"/api/recordings/{rec.id}/release-candidates")
    assert res.status_code == 503


def test_candidates_returns_ranked_list(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: _SEARCH_RESPONSE)
    client = app.test_client()
    _login_as(client)
    res = client.get(f"/api/recordings/{rec.id}/release-candidates")
    assert res.status_code == 200
    cands = res.get_json()["candidates"]
    assert len(cands) == 1
    assert cands[0]["mbid"] == "rel-mbid-1"


def test_candidates_admin_only(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    listener = User(username="listener_rc", role="listener", is_active=True, password_hash="x")
    _db.session.add(listener)
    _db.session.commit()
    client = app.test_client()
    _login_as(client, username="listener_rc")
    res = client.get(f"/api/recordings/{rec.id}/release-candidates")
    assert res.status_code == 403


def test_candidates_live_recording_400(app, seeded_ids):
    rec = _make_live_recording()
    client = app.test_client()
    _login_as(client)
    res = client.get(f"/api/recordings/{rec.id}/release-candidates")
    assert res.status_code == 400


def test_candidates_404_for_missing_recording(app, seeded_ids):
    client = app.test_client()
    _login_as(client)
    res = client.get("/api/recordings/999999/release-candidates")
    assert res.status_code == 404


# ── POST .../release-link ──────────────────────────────────────────────────

def test_link_sets_linked_status_and_fills_fields(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)
    client = app.test_client()
    _login_as(client)

    res = client.post(f"/api/recordings/{rec.id}/release-link", json={"mbid": "rel-mbid-1"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "linked"
    assert body["mb_label"] == "Columbia"

    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert rec.mb_release_status == "linked"
    assert rec.mb_release_id == "rel-mbid-1"
    assert rec.performance.start_year == 1973  # already set, untouched
    assert rec.performance.start_month == 10   # was null, filled from release date


def test_link_requires_mbid(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    client = app.test_client()
    _login_as(client)
    res = client.post(f"/api/recordings/{rec.id}/release-link", json={})
    assert res.status_code == 400


def test_link_502_when_lookup_fails(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: None)
    client = app.test_client()
    _login_as(client)
    res = client.post(f"/api/recordings/{rec.id}/release-link", json={"mbid": "rel-mbid-1"})
    assert res.status_code == 502


def test_link_admin_only(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    listener = User(username="listener_link", role="listener", is_active=True, password_hash="x")
    _db.session.add(listener)
    _db.session.commit()
    client = app.test_client()
    _login_as(client, username="listener_link")
    res = client.post(f"/api/recordings/{rec.id}/release-link", json={"mbid": "rel-mbid-1"})
    assert res.status_code == 403


def test_link_live_recording_400(app, seeded_ids):
    rec = _make_live_recording()
    client = app.test_client()
    _login_as(client)
    res = client.post(f"/api/recordings/{rec.id}/release-link", json={"mbid": "x"})
    assert res.status_code == 400


# ── POST .../release-unlink ─────────────────────────────────────────────────

def test_unlink_clears_columns_and_sets_unlinked(app, monkeypatch, seeded_ids):
    rec = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)
    client = app.test_client()
    _login_as(client)

    client.post(f"/api/recordings/{rec.id}/release-link", json={"mbid": "rel-mbid-1"})
    res = client.post(f"/api/recordings/{rec.id}/release-unlink")
    assert res.status_code == 200
    assert res.get_json()["status"] == "unlinked"

    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert rec.mb_release_id is None
    assert rec.mb_release_status == "unlinked"


def test_unlink_admin_only(app, seeded_ids):
    rec = _make_studio_recording()
    listener = User(username="listener_unlink", role="listener", is_active=True, password_hash="x")
    _db.session.add(listener)
    _db.session.commit()
    client = app.test_client()
    _login_as(client, username="listener_unlink")
    res = client.post(f"/api/recordings/{rec.id}/release-unlink")
    assert res.status_code == 403


def test_unlink_live_recording_400(app, seeded_ids):
    rec = _make_live_recording()
    client = app.test_client()
    _login_as(client)
    res = client.post(f"/api/recordings/{rec.id}/release-unlink")
    assert res.status_code == 400


def test_unlink_404_for_missing_recording(app, seeded_ids):
    client = app.test_client()
    _login_as(client)
    res = client.post("/api/recordings/999999/release-unlink")
    assert res.status_code == 404
