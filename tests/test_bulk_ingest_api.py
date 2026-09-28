"""
tests/test_bulk_ingest_api.py -- /api/bulk-ingest/* control surface (spec chunk 5d).

The worker thread itself is exercised directly against pure functions in
test_bulk_ingest_run.py/test_bulk_ingest_resume.py/test_bulk_ingest_dedup.py; here
_start_worker is replaced with a no-op so a run's status/items never change
out from under an assertion -- these tests are about the ROUTES, not the
walk.
"""

import pytest

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.quality import RecordingQuality
from app.models.user import User


@pytest.fixture()
def api(app, monkeypatch, tmp_path):
    app.config["LOGIN_DISABLED"] = True
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    monkeypatch.setattr("app.utils.bulk_ingest_run._start_worker", lambda *a, **k: None)
    return app.test_client()


def _login_as(client, username):
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user: {username}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return user


def _make_non_admin():
    user = User(username="viewer", role="viewer", is_active=True, password_hash="x")
    _db.session.add(user)
    _db.session.commit()
    return user


def test_start_twice_returns_same_run_id(api):
    _login_as(api, "admin")
    r1 = api.post("/api/bulk-ingest/start")
    r2 = api.post("/api/bulk-ingest/start")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json()["id"] == r2.get_json()["id"]
    assert _db.session.query(BulkIngestRun).count() == 1


def test_pause_then_resume_flips_status(api):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    r = api.post(f"/api/bulk-ingest/{run_id}/pause")
    assert r.status_code == 200
    assert r.get_json()["status"] == "paused"

    r = api.post(f"/api/bulk-ingest/{run_id}/resume")
    assert r.status_code == 200
    assert r.get_json()["status"] == "running"


def test_current_counts_match_item_statuses(api):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path="A", status="ingested"),
        BulkIngestItem(run_id=run_id, rel_path="B", status="ingested"),
        BulkIngestItem(run_id=run_id, rel_path="C", status="review"),
        BulkIngestItem(run_id=run_id, rel_path="D", status="pending"),
        BulkIngestItem(run_id=run_id, rel_path="E", status="failed"),
    ])
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["counts"]["ingested"] == 2
    assert body["counts"]["review"] == 1
    assert body["counts"]["pending"] == 1
    assert body["counts"]["failed"] == 1
    assert body["counts"]["skipped"] == 0
    assert body["counts"]["in_progress"] == 0
    assert sum(body["counts"].values()) == 5


def test_current_reports_reason_and_kind_breakdowns(api):
    """The 2026-09-27 bulk_ingest page redesign needs WHY a review/skipped item
    is what it is, and how many ingested items were studio recordings --
    all grouped queries, no per-item loop."""
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path="A", status="review", reason="needs_artist"),
        BulkIngestItem(run_id=run_id, rel_path="B", status="review", reason="needs_artist"),
        BulkIngestItem(run_id=run_id, rel_path="C", status="review", reason="needs_date"),
        BulkIngestItem(run_id=run_id, rel_path="D", status="review", reason="unsupported_format"),
        BulkIngestItem(run_id=run_id, rel_path="E", status="skipped", reason="already_in_library"),
        BulkIngestItem(run_id=run_id, rel_path="F", status="skipped", reason="rejected"),
        BulkIngestItem(run_id=run_id, rel_path="G", status="failed", reason="unreadable"),
        BulkIngestItem(run_id=run_id, rel_path="H", status="ingested", kind="studio"),
        BulkIngestItem(run_id=run_id, rel_path="I", status="ingested", kind="live"),
    ])
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["reasons"] == {"needs_artist": 2, "needs_date": 1, "unsupported_format": 1}
    assert body["skipped_reasons"] == {"already_in_library": 1, "rejected": 1}
    assert body["failed"] == 1
    assert body["studio"] == 1
    assert body["counts"]["ingested"] == 2


def test_current_reports_scored_and_scorable(api):
    """scorable is every item this run ingested; scored is how many of those
    already carry a RecordingQuality row with an actual number in it -- a
    row can exist with listening_quality still NULL if extraction failed."""
    from app.models.artist import Artist
    from app.models.performance import Performance
    from app.models.recording import Recording

    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    artist = Artist(name="A")
    _db.session.add(artist)
    _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=1977)
    _db.session.add(perf)
    _db.session.flush()

    scored_rec   = Recording(performance_id=perf.id, folder_path="Scored", is_complete=True)
    unscored_rec = Recording(performance_id=perf.id, folder_path="Unscored", is_complete=True)
    null_lq_rec  = Recording(performance_id=perf.id, folder_path="NullLQ", is_complete=True)
    _db.session.add_all([scored_rec, unscored_rec, null_lq_rec])
    _db.session.flush()

    _db.session.add(RecordingQuality(recording_id=scored_rec.id, listening_quality=72.0))
    _db.session.add(RecordingQuality(recording_id=null_lq_rec.id, listening_quality=None))
    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path="Scored", status="ingested", kind="live", recording_id=scored_rec.id),
        BulkIngestItem(run_id=run_id, rel_path="Unscored", status="ingested", kind="live", recording_id=unscored_rec.id),
        BulkIngestItem(run_id=run_id, rel_path="NullLQ", status="ingested", kind="live", recording_id=null_lq_rec.id),
    ])
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["scorable"] == 3
    assert body["scored"] == 1


def test_current_scorable_excludes_studio(api):
    """Studio recordings are never scored (2026-09-27) -- scorable/scored
    must count only ingested items with kind == 'live'."""
    from app.models.artist import Artist
    from app.models.performance import Performance
    from app.models.recording import Recording

    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    artist = Artist(name="A")
    _db.session.add(artist)
    _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=1977)
    _db.session.add(perf)
    _db.session.flush()

    live_rec   = Recording(performance_id=perf.id, folder_path="Live", is_complete=True, kind="live")
    studio_rec = Recording(performance_id=perf.id, folder_path="Studio", is_complete=True, kind="studio")
    _db.session.add_all([live_rec, studio_rec])
    _db.session.flush()

    _db.session.add(RecordingQuality(recording_id=studio_rec.id, listening_quality=90.0))
    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path="Live", status="ingested", kind="live", recording_id=live_rec.id),
        BulkIngestItem(run_id=run_id, rel_path="Studio", status="ingested", kind="studio", recording_id=studio_rec.id),
    ])
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["scorable"] == 1
    assert body["scored"] == 0


def test_current_reports_now_upcoming_and_recent(api, seeded_ids):
    """The 2026-09-27 progress-view redesign: /current also reports the
    in-progress folder (`now`), the next 8 pending folders in id order
    (`upcoming`), and the last 12 ingested recordings newest-first
    (`recent`), each recent entry carrying enough to render "Artist  <date>"
    without the client fetching anything else."""
    from app.models.recording import Recording
    from app.models.performance import Performance

    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    perf_id = seeded_ids["performance_id"]
    rec = Recording(performance_id=perf_id, folder_path="Ingested/One",
                    is_complete=True, is_official=False, kind="live")
    _db.session.add(rec)
    _db.session.flush()

    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path="Ingested/One", status="ingested",
                       recording_id=rec.id),
        BulkIngestItem(run_id=run_id, rel_path="Working/Now", status="in_progress"),
        BulkIngestItem(run_id=run_id, rel_path="Later/A", status="pending"),
        BulkIngestItem(run_id=run_id, rel_path="Later/B", status="pending"),
    ])
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["now"] == "Now"
    assert body["upcoming"] == ["A", "B"]
    assert len(body["recent"]) == 1
    entry = body["recent"][0]
    assert entry["recording_id"] == rec.id
    assert entry["kind"] == "live"
    perf = _db.session.get(Performance, perf_id)
    assert entry["date_text"] == f"{perf.start_year}-{perf.start_month:02d}-{perf.start_day:02d}" \
        if perf.start_month and perf.start_day else str(perf.start_year)


def test_current_upcoming_limited_to_eight(api, seeded_ids):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path=f"Later/{i}", status="pending")
        for i in range(12)
    ])
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["upcoming"] == [str(i) for i in range(8)]


def test_items_done_filter_excludes_pending_and_orders_newest_first(api):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    _db.session.add_all([
        BulkIngestItem(run_id=run_id, rel_path="A", status="ingested"),
        BulkIngestItem(run_id=run_id, rel_path="B", status="pending"),
        BulkIngestItem(run_id=run_id, rel_path="C", status="in_progress"),
        BulkIngestItem(run_id=run_id, rel_path="D", status="skipped", reason="rejected"),
        BulkIngestItem(run_id=run_id, rel_path="E", status="failed"),
    ])
    _db.session.commit()

    body = api.get(f"/api/bulk-ingest/{run_id}/items?status=done").get_json()
    paths = [it["rel_path"] for it in body["items"]]
    assert paths == ["E", "D", "A"]   # newest (highest id) first, no B or C
    assert body["total"] == 3


def test_items_format_column_round_trips(api):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    _db.session.add(BulkIngestItem(run_id=run_id, rel_path="A", status="ingested",
                                   format="FLAC, MP3"))
    _db.session.commit()

    body = api.get(f"/api/bulk-ingest/{run_id}/items?status=done").get_json()
    assert body["items"][0]["format"] == "FLAC, MP3"


def test_items_meta_fields_round_trip(api):
    """The 2026-09-27 expand panel's fields -- artist/date_text/venue/
    location/source/title -- come off BulkIngestItem.meta (a small JSON
    blob written at extraction time), flattened server-side. Location is
    joined from city/state/country the same way app.js's fmtLocation does."""
    import json
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    meta = {
        "artist": "Grateful Dead", "date_text": "1977-05-08",
        "venue": "Barton Hall", "city": "Ithaca", "state": "NY",
        "country": "USA", "source": "SBD", "title": None,
    }
    _db.session.add(BulkIngestItem(run_id=run_id, rel_path="A", status="ingested",
                                   kind="live", meta=json.dumps(meta)))
    _db.session.commit()

    body = api.get(f"/api/bulk-ingest/{run_id}/items?status=done").get_json()
    it = body["items"][0]
    assert it["artist"] == "Grateful Dead"
    assert it["date_text"] == "1977-05-08"
    assert it["venue"] == "Barton Hall"
    assert it["location"] == "Ithaca, NY"
    assert it["source"] == "SBD"
    assert it["title"] is None


def test_items_meta_fields_null_when_no_meta(api):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    _db.session.add(BulkIngestItem(run_id=run_id, rel_path="A", status="skipped",
                                   reason="already_in_library"))
    _db.session.commit()

    body = api.get(f"/api/bulk-ingest/{run_id}/items?status=done").get_json()
    it = body["items"][0]
    assert it["artist"] is None
    assert it["location"] is None


def test_non_admin_gets_403_on_start_and_pause(api):
    _make_non_admin()
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    _login_as(api, "viewer")
    assert api.post("/api/bulk-ingest/start").status_code == 403
    assert api.post(f"/api/bulk-ingest/{run_id}/pause").status_code == 403


def test_current_readable_by_non_admin(api):
    """N3: GET /current mutates nothing, so any logged-in user (not just an
    admin) gets the bulk_ingest nav item and review split during a run."""
    _make_non_admin()
    _login_as(api, "admin")
    api.post("/api/bulk-ingest/start")

    _login_as(api, "viewer")
    r = api.get("/api/bulk-ingest/current")
    assert r.status_code == 200
    assert r.get_json()["status"] == "running"

    # A mutating route on the same surface must still be admin-only.
    assert api.post("/api/bulk-ingest/start").status_code == 403


def test_current_hides_last_error_from_non_admin(api):
    """R2-N1: last_error is a full Python traceback with absolute filesystem
    paths -- an admin still sees it, a non-admin caller (GET /current is
    login_required, not admin_required -- N3) does not."""
    _make_non_admin()
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    run = _db.session.get(BulkIngestRun, run_id)
    run.last_error = "Traceback (most recent call last): ... /Volumes/music/secret"
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["last_error"] == run.last_error

    _login_as(api, "viewer")
    body = api.get("/api/bulk-ingest/current").get_json()
    assert "last_error" not in body


def test_current_reports_duplicate_basename(api):
    """N2: the duplicate's second link must carry the duplicate recording's
    own folder basename, not just its bare id."""
    from app.models.artist import Artist
    from app.models.performance import Performance
    from app.models.recording import Recording

    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]

    artist = Artist(name="A")
    _db.session.add(artist)
    _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=1977)
    _db.session.add(perf)
    _db.session.flush()
    original = Recording(performance_id=perf.id, folder_path="Show1", is_complete=True)
    _db.session.add(original)
    _db.session.flush()

    item = BulkIngestItem(run_id=run_id, rel_path="Show2", status="ingested",
                        recording_id=None, duplicate_of=original.id)
    _db.session.add(item)
    _db.session.commit()

    run = _db.session.get(BulkIngestRun, run_id)
    run.status = "done"
    _db.session.commit()

    body = api.get("/api/bulk-ingest/current").get_json()
    assert body["duplicates"] == [{
        "item_id": item.id,
        "rel_path_basename": "Show2",
        "recording_id": None,
        "duplicate_of": original.id,
        "duplicate_basename": "Show1",
    }]


def test_server_mode_still_excludes_bulk_ingest_routes():
    """
    Belt-and-braces: the SERVER_MODE route-table assertion lives in
    tests/test_server_mode_surface.py and covers every front-door blueprint
    including this one automatically (it asserts the share app's route table
    against the front door's, not against a hand-maintained list) -- this
    test only pins that bulk_ingest's own url_prefix is what that assertion
    relies on.
    """
    from config import Config

    class ServerModeConfig(Config):
        SERVER_MODE = True
        SECRET_KEY = "test-secret-key-not-the-dev-default"
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True

    from app import create_app as _create_app
    share_app = _create_app(config_class=ServerModeConfig)
    rules = {r.rule for r in share_app.url_map.iter_rules()}
    assert not any(r.startswith("/api/bulk-ingest") for r in rules)
