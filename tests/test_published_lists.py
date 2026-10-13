"""Recordings in Backlog, Workshop or Downloads are unpublished and stay off
the library lists (Ryan, 2026-10-13)."""
import os

from app.extensions import db as _db
from app.models.recording import Recording
from app.utils.published import (in_working_folder, is_published_location,
                                  unpublish_working_folder_recordings)

from tests.test_studio_sharing import _recording
from tests.test_studio_library_payloads import client  # noqa: F401  (fixture)


def test_in_working_folder_is_prefix_safe(app):
    app.config["TRIAGE_DIRS"] = {"workshop": "/lib/Workshop"}
    assert in_working_folder("/lib/Workshop/show")
    assert in_working_folder("/lib/Workshop")
    assert not in_working_folder("/lib/Workshop2/show")
    assert not in_working_folder("/lib/Artist/show")


def test_location_inside_library_workshop_is_unpublished(app):
    root = app.config["LIBRARY_ROOT"]
    app.config["TRIAGE_DIRS"] = {"workshop": os.path.join(str(root), "Workshop")}
    assert is_published_location("Artist/show") is True
    assert is_published_location("Workshop/show") is False


def test_boot_reconcile_only_ever_unpublishes(app):
    root = app.config["LIBRARY_ROOT"]
    app.config["TRIAGE_DIRS"] = {"backlog": os.path.join(str(root), "Backlog")}
    shelf = _recording("Shelf")
    backlog = _recording("Backlogged")
    moved = _recording("Moved")
    shelf.folder_path, backlog.folder_path = "Shelf/x", "Backlog/y"
    moved.folder_path, moved.is_published = "Moved/z", False
    _db.session.commit()
    assert unpublish_working_folder_recordings() == 1
    assert _db.session.get(Recording, backlog.id).is_published is False
    assert _db.session.get(Recording, shelf.id).is_published is True
    assert _db.session.get(Recording, moved.id).is_published is False


def test_unpublished_recording_is_off_every_list(app, client):
    keep = _recording("Keep")
    gone = _recording("Gone")
    gone.is_published = False
    _db.session.commit()
    c = client
    recent = [r["id"] for r in c.get("/api/recordings/recent").get_json()]
    assert keep.id in recent and gone.id not in recent
    lib = c.get("/api/artists/all-recordings").get_json()
    ids = [r["id"] for a in lib for p in a["performances"] for r in p["recordings"]]
    assert keep.id in ids and gone.id not in ids
    assert "Gone" not in [a["artist_name"] for a in lib]
    published_live = Recording.query.filter_by(kind="live", is_published=True).count()
    assert c.get("/api/recordings/kind-counts").get_json()["live"] == published_live


def test_dimension_pages_search_collections_favorites_hide_unpublished(app, client):
    from app.models.collection import Collection, CollectionRecording
    from app.models.venue import Venue
    keep, gone = _recording("Shown"), _recording("Hidden")
    v = Venue(name="The Hall")
    _db.session.add(v)
    _db.session.flush()
    gone.performance.artist_id = keep.performance.artist_id
    for r in (keep, gone):
        r.performance.venue_id = v.id
        r.is_favorite = True
    gone.is_published = False
    col = Collection(name="Box")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add_all([CollectionRecording(collection_id=col.id, recording_id=keep.id, order=0),
                         CollectionRecording(collection_id=col.id, recording_id=gone.id, order=1)])
    _db.session.commit()
    c, aid = client, keep.performance.artist_id

    def ids(rows):
        return {r["id"] for r in rows}

    perf_recs = [r for p in c.get(f"/api/artists/{aid}/recordings").get_json() for r in p["recordings"]]
    assert ids(perf_recs) == {keep.id}
    assert next(a for a in c.get("/api/artists/").get_json() if a["id"] == aid)["recording_count"] == 1
    venue = c.get(f"/api/venues/{v.id}").get_json()
    assert ids(venue["recordings"]) == {keep.id} and venue["recording_count"] == 1
    assert next(x for x in c.get("/api/venues/").get_json() if x["id"] == v.id)["recording_count"] == 1
    assert ids(c.get(f"/api/collections/{col.id}").get_json()["recordings"]) == {keep.id}
    assert next(x for x in c.get("/api/collections/").get_json() if x["id"] == col.id)["recording_count"] == 1
    assert gone.id not in ids(c.get("/api/recordings/favorites").get_json())
    found = c.get("/api/search?q=Shown").get_json()
    rec_ids = {i["id"] for g in found["groups"] for i in g["items"] if i.get("type") == "recording"}
    assert keep.id in rec_ids and gone.id not in rec_ids
