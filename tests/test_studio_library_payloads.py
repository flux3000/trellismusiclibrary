"""
tests/test_studio_library_payloads.py -- Studio Records spec v1, contract
items 1-3 (backend plumbing): title/kind on every recording list payload,
a live-only Top Shelf, and GET /api/recordings/kind-counts (owner + peer
mirror, peer counted over the visible set only).

Search's own "albums" group (contract item 4) lives entirely in
app/utils/search.py and app/api/search.py, and is covered in
tests/test_search.py, which already has the fixtures for it.
"""

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.collection import Collection, CollectionRecording, SYSTEM_FULL_LIBRARY
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.models.user import User

from app.utils.peer_auth import generate_token, hash_secret
from app.utils.serialize import recording_row, recording_summary


# ── helpers ──────────────────────────────────────────────────────────────

def _recording(label, kind="live", title=None, quality=None, year=1999):
    """A minimal Recording of the given kind, its own Artist/Performance."""
    artist = Artist(name=f"{label} Band")
    _db.session.add(artist)
    _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=year,
                       start_month=(6 if kind == "live" else None),
                       start_day=(1 if kind == "live" else None))
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                    is_official=False, folder_path=f"{label}/{year}",
                    is_published=True, kind=kind, title=title, quality=quality)
    _db.session.add(rec)
    _db.session.commit()
    return rec, artist


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def _login_as(client, username="admin"):
    """Session login that PROVES it authenticated (see test_peer_sharing's
    long-form note on why: Flask-Login caches identity on `g` for the whole
    test's single app context, so a broken login must fail loudly here, not
    downstream as an impersonated 403)."""
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    me = client.get("/api/auth/me")
    assert me.status_code == 200, (
        f"session login as {username!r} did not authenticate: /api/auth/me "
        f"returned {me.status_code}. The harness is broken, not the endpoint.")
    return user


@pytest.fixture()
def client(app):
    c = app.test_client()
    _login_as(c)
    return c


def _peer_granted_full_library(name="Peer"):
    """A peer granted the Full Library system collection -- sees every
    is_published recording of either kind (2026-09-27: kind is a display
    concern, never a visibility one)."""
    col = Collection(name="Full Library", system_key=SYSTEM_FULL_LIBRARY)
    _db.session.add(col)
    _db.session.flush()
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return col, peer, raw


def _peer_granted_junction(recs, name="Peer"):
    """A peer granted an ordinary collection holding exactly `recs` -- a
    narrower visible set than the whole library, to prove a peer count is
    scoped rather than global."""
    col = Collection(name="Narrow box")
    _db.session.add(col)
    _db.session.flush()
    for i, r in enumerate(recs):
        _db.session.add(CollectionRecording(collection_id=col.id, recording_id=r.id, order=i))
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return col, peer, raw


# ══════════════════════════════════════════════════════════════════════════
# Item 1 -- title/kind on every list payload
# ══════════════════════════════════════════════════════════════════════════

def test_recording_summary_carries_kind_and_title(app):
    rec, _artist = _recording("Odessa", kind="studio", title="American Beauty")
    row = recording_summary(rec)
    assert row["kind"] == "studio"
    assert row["title"] == "American Beauty"


def test_recording_summary_title_is_none_for_a_live_show(app, seeded_ids):
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    row = recording_summary(rec)
    assert row["kind"] == "live"
    assert row["title"] is None


def test_recording_row_carries_kind_and_title(app):
    rec, _artist = _recording("Odessa", kind="studio", title="American Beauty")
    row = recording_row(rec)
    assert row["kind"] == "studio"
    assert row["title"] == "American Beauty"


def test_recent_recordings_carries_title_and_kind(app, client):
    _recording("Odessa", kind="studio", title="American Beauty")
    body = client.get("/api/recordings/recent").get_json()
    by_title = {r["title"]: r for r in body if r["title"]}
    assert by_title["American Beauty"]["kind"] == "studio"


def test_favorites_carries_title_and_kind(app, client):
    rec, _artist = _recording("Odessa", kind="studio", title="American Beauty")
    rec.is_favorite = True
    _db.session.commit()
    body = client.get("/api/recordings/favorites").get_json()
    assert len(body) == 1
    assert body[0]["kind"] == "studio"
    assert body[0]["title"] == "American Beauty"


def test_collection_recordings_carries_title_and_kind(app, client):
    rec, _artist = _recording("Odessa", kind="studio", title="American Beauty")
    col = Collection(name="A box")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    _db.session.commit()

    body = client.get(f"/api/collections/{col.id}").get_json()
    row = body["recordings"][0]
    assert row["kind"] == "studio"
    assert row["title"] == "American Beauty"


def test_genre_recordings_carries_title_and_kind(app, client):
    from app.models.genre import Genre
    rec, artist = _recording("Odessa", kind="studio", title="American Beauty")
    genre = Genre(name="Folk")
    _db.session.add(genre)
    _db.session.flush()
    artist.genre_id = genre.id
    _db.session.commit()

    body = client.get(f"/api/genres/{genre.id}").get_json()
    rows = [r for a in body["artists"] for r in a.get("recordings", [])]
    found = None
    for r in rows:
        if r.get("title") == "American Beauty":
            found = r
            break
    assert found is not None, f"studio recording not found in genre payload: {body}"
    assert found["kind"] == "studio"


def test_musician_recordings_carries_title_and_kind(app, client):
    """A musician's OWN member acts don't list recordings on this endpoint
    (that lives on the artist page) -- the one place a person's payload
    carries recording_summary() rows is a sit-in guest_appearance
    (app/api/musicians.py get_musician()), so that's the path under test."""
    from app.models.musician import Musician
    from app.models.performance_personnel import PerformancePersonnel
    rec, artist = _recording("Odessa", kind="studio", title="American Beauty")
    person = Musician(name="Jerry Garcia")
    _db.session.add(person)
    _db.session.flush()
    _db.session.add(PerformancePersonnel(performance_id=rec.performance_id,
                                         musician_id=person.id, is_guest=True))
    _db.session.commit()

    body = client.get(f"/api/musicians/{person.id}").get_json()
    all_recs = [r for g in body["guest_appearances"] for r in g.get("recordings", [])]
    found = any(r.get("title") == "American Beauty" and r.get("kind") == "studio"
                for r in all_recs)
    assert found, f"studio recording not found on musician payload: {body}"


def test_all_recordings_carries_title_kind_and_image_url(app, client):
    rec, artist = _recording("Odessa", kind="studio", title="American Beauty")
    body = client.get("/api/artists/all-recordings").get_json()
    entry = next(a for a in body if a["artist_id"] == artist.id)
    row = entry["performances"][0]["recordings"][0]
    assert row["kind"] == "studio"
    assert row["title"] == "American Beauty"
    assert "image_url" in row


def test_share_all_recordings_mirrors_owner_shape(app, client):
    rec, artist = _recording("Odessa", kind="studio", title="American Beauty")
    col, peer, raw = _peer_granted_full_library()

    owner_row = None
    for a in client.get("/api/artists/all-recordings").get_json():
        if a["artist_id"] == artist.id:
            owner_row = a["performances"][0]["recordings"][0]
    assert owner_row is not None

    peer_resp = client.get("/api/share/artists/all-recordings", headers=_auth(raw))
    assert peer_resp.status_code == 200
    peer_entry = next(a for a in peer_resp.get_json() if a["artist_id"] == artist.id)
    peer_row = peer_entry["performances"][0]["recordings"][0]

    assert peer_row["kind"] == owner_row["kind"] == "studio"
    assert peer_row["title"] == owner_row["title"] == "American Beauty"


def test_share_recent_carries_title_and_kind(app, client):
    _recording("Odessa", kind="studio", title="American Beauty")
    col, peer, raw = _peer_granted_full_library()
    body = client.get("/api/share/recordings/recent", headers=_auth(raw)).get_json()
    by_title = {r["title"]: r for r in body if r["title"]}
    assert by_title["American Beauty"]["kind"] == "studio"


# ══════════════════════════════════════════════════════════════════════════
# Item 2 -- The Top Shelf is live-only
# ══════════════════════════════════════════════════════════════════════════

def test_recommended_excludes_an_a_graded_studio_record(app, client):
    _recording("Odessa", kind="studio", title="American Beauty", quality="A")
    live, _artist = _recording("Fillmore Show", kind="live", quality="A")
    body = client.get("/api/recordings/recommended?limit=24").get_json()
    ids = [r["id"] for r in body]
    assert live.id in ids
    assert all(r["kind"] == "live" for r in body)


def test_recommended_pool_query_filters_kind_at_the_query_level(app):
    """The filter must be server-side SQL, not a post-hoc Python skip --
    contract item 2 says `_recommended_pool_query()` itself gains the
    condition."""
    from app.api.recordings import _recommended_pool_query
    _recording("Odessa", kind="studio", title="American Beauty", quality="A+")
    live, _artist = _recording("Fillmore Show", kind="live", quality="A+")
    pool_ids = {r.id for r in _recommended_pool_query().all()}
    assert pool_ids == {live.id}


def test_share_recommended_inherits_live_only_pool(app, client):
    _recording("Odessa", kind="studio", title="American Beauty", quality="A")
    live, _artist = _recording("Fillmore Show", kind="live", quality="A")
    col, peer, raw = _peer_granted_full_library()
    body = client.get("/api/share/recordings/recommended?limit=24",
                      headers=_auth(raw)).get_json()
    assert all(r["kind"] == "live" for r in body)


# ══════════════════════════════════════════════════════════════════════════
# Item 3 -- GET /api/recordings/kind-counts
# ══════════════════════════════════════════════════════════════════════════

def test_kind_counts_owner(app, client):
    # seeded_ids already gives one live recording.
    _recording("Odessa", kind="studio", title="American Beauty")
    _recording("Workingman's Dead", kind="studio", title="Workingman's Dead")
    body = client.get("/api/recordings/kind-counts").get_json()
    assert body == {"live": 1, "studio": 2}


def test_kind_counts_owner_requires_login(app):
    r = app.test_client().get("/api/recordings/kind-counts")
    assert r.status_code == 401


def test_kind_counts_is_one_group_by_query(app, client):
    """Contract item 3: must be one cheap GROUP BY, not a load of every row."""
    _recording("Odessa", kind="studio", title="American Beauty")
    from app.extensions import db as _db2
    from sqlalchemy import event
    queries = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(_db2.engine, "before_cursor_execute", _capture)
    try:
        r = client.get("/api/recordings/kind-counts")
        assert r.status_code == 200
    finally:
        event.remove(_db2.engine, "before_cursor_execute", _capture)

    select_recording_queries = [
        q for q in queries if "FROM recording" in q or "from recording" in q
    ]
    assert len(select_recording_queries) == 1, select_recording_queries
    assert "GROUP BY" in select_recording_queries[0].upper()


def test_kind_counts_peer_scoped_to_visible_set(app, client):
    studio, artist = _recording("Odessa", kind="studio", title="American Beauty")
    live, _a2 = _recording("Fillmore Show", kind="live")

    # A peer granted only the studio recording must not see the live one in
    # its counts, even though it exists in the library.
    col, peer, raw = _peer_granted_junction([studio], name="Narrow Peer")
    body = client.get("/api/share/recordings/kind-counts", headers=_auth(raw)).get_json()
    assert body == {"live": 0, "studio": 1}


def test_kind_counts_peer_full_library_matches_owner(app, client, seeded_ids):
    _recording("Odessa", kind="studio", title="American Beauty")
    col, peer, raw = _peer_granted_full_library()

    owner = client.get("/api/recordings/kind-counts").get_json()
    peer_body = client.get("/api/share/recordings/kind-counts", headers=_auth(raw)).get_json()
    assert peer_body == owner


def test_kind_counts_peer_with_no_grants_is_zero(app, client):
    peer = Peer(name="Nothing")
    _db.session.add(peer)
    _db.session.flush()
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    body = client.get("/api/share/recordings/kind-counts", headers=_auth(raw)).get_json()
    assert body == {"live": 0, "studio": 0}
