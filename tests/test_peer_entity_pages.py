"""
tests/test_peer_entity_pages.py — the peer-facing entity pages (milestone 2).

test_peer_visible_set.py proves the visible SET is computed correctly. This
file proves the ENDPOINTS honour it — which is a separate failure mode: a
correct visible set queried by an endpoint that forgot to apply it leaks
everything.

Same two-halves world as the visible-set tests: SHARED is granted, SECRET never
is, and every assertion is a form of "SECRET does not appear."

The count assertions are the point of this file. A peer seeing an ungranted
recording in a list is an obvious bug someone would catch. A peer seeing
`recording_count: 41` on a venue where they can play 3 is a leak that looks
like working software.
"""

import pytest

from app.extensions import db as _db
from app.models.musician import Musician, Membership
from app.models.collection import Collection, CollectionRecording
from app.models.genre import Genre
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.recording import Recording
from app.models.venue import Venue
from app.utils.peer_auth import generate_token, hash_secret


# ── World building ────────────────────────────────────────────────────────────

def _half(label, genre_name, venue=None):
    """One self-contained slice. `venue` lets both halves SHARE a venue, which
    is how the venue count-leak gets tested."""
    genre = Genre(name=genre_name)
    _db.session.add(genre)
    _db.session.flush()

    artist = Artist(name=f"{label} Band", genre_id=genre.id)
    _db.session.add(artist)
    _db.session.flush()

    musician = Musician(name=f"{label} Player")
    _db.session.add(musician)
    _db.session.flush()
    _db.session.add(Membership(artist_id=artist.id, musician_id=musician.id, order=0))

    if venue is None:
        venue = Venue(name=f"{label} Hall", city=label, country="US")
        _db.session.add(venue)
        _db.session.flush()

    perf = Performance(artist_id=artist.id, venue_id=venue.id,
                       start_year=1975, start_month=6, start_day=1)
    _db.session.add(perf)
    _db.session.flush()

    rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                    is_official=False, folder_path=f"{label}/1975")
    _db.session.add(rec)
    _db.session.commit()

    return {"genre": genre.id, "artist": artist.id, "musician": musician.id,
            "venue": venue.id, "performance": perf.id, "recording": rec.id,
            "venue_obj": venue}


@pytest.fixture()
def world(app):
    """SHARED and SECRET share ONE venue — so the venue is visible, but only
    half its shows are, which is exactly the count-leak scenario."""
    shared = _half("Shared", "Jazz")
    secret = _half("Secret", "Funk", venue=shared["venue_obj"])

    shared_col = Collection(name="Shared Box")
    _db.session.add(shared_col)
    _db.session.flush()
    _db.session.add(CollectionRecording(
        collection_id=shared_col.id, recording_id=shared["recording"], order=0))

    peer = Peer(name="Matt")
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=shared_col.id))

    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()

    return {"shared": shared, "secret": secret, "token": raw,
            "peer": peer, "collection": shared_col}


def _auth(world):
    return {"Authorization": f"Bearer {world['token']}"}


# ── Artist ─────────────────────────────────────────────────────────────────

def test_granted_artist_page_is_full_catalog_metadata(app, world):
    c = app.test_client()
    r = c.get(f"/api/share/artists/{world['shared']['artist']}", headers=_auth(world))
    assert r.status_code == 200
    body = r.get_json()
    assert body["name"] == "Shared Band"
    # Catalog metadata travels in full — that is the whole point of the design.
    for key in ("bio", "members", "resources", "dossier", "genre", "musicbrainz"):
        assert key in body


def test_ungranted_artist_page_is_403(app, world):
    c = app.test_client()
    r = c.get(f"/api/share/artists/{world['secret']['artist']}", headers=_auth(world))
    assert r.status_code == 403


def test_artist_image_urls_point_at_the_share_route(app, world):
    """A peer cannot reach /api/artists/images/<id>. If the payload hands
    them that URL, every photo on the page is a broken image."""
    c = app.test_client()
    r = c.get(f"/api/share/artists/{world['shared']['artist']}", headers=_auth(world))
    for img in r.get_json()["images"]:
        assert img["url"].startswith("/api/share/artists/images/")


def test_artist_recordings_exclude_ungranted(app, world):
    c = app.test_client()
    r = c.get(f"/api/share/artists/{world['shared']['artist']}/recordings",
              headers=_auth(world))
    assert r.status_code == 200
    rec_ids = {rec["id"] for perf in r.get_json() for rec in perf["recordings"]}
    assert rec_ids == {world["shared"]["recording"]}


# ── Venue — the count leak ────────────────────────────────────────────────────

def test_venue_counts_are_over_the_visible_set_only(app, world):
    """
    Both halves played this venue; only one is granted. The local endpoint
    would report performance_count=2 / recording_count=2 and list both shows.
    """
    c = app.test_client()
    r = c.get(f"/api/share/venues/{world['shared']['venue']}", headers=_auth(world))
    assert r.status_code == 200
    body = r.get_json()

    assert body["performance_count"] == 1, "leaked a performance the peer can't see"
    assert body["recording_count"] == 1, "leaked a recording count over the full library"
    assert {rec["id"] for rec in body["recordings"]} == {world["shared"]["recording"]}


def test_venue_with_nothing_visible_is_403(app, world):
    lonely = Venue(name="Never Shared Hall")
    _db.session.add(lonely)
    _db.session.commit()
    c = app.test_client()
    r = c.get(f"/api/share/venues/{lonely.id}", headers=_auth(world))
    assert r.status_code == 403


# ── Musician ────────────────────────────────────────────────────────────────────

def test_musician_page_lists_only_visible_acts(app, world):
    """One person, member of both a shared and a secret act. The secret act
    must not appear — naming it would leak an act by the back door."""
    shared_artist = _db.session.get(Artist, world["shared"]["artist"])
    secret_artist = _db.session.get(Artist, world["secret"]["artist"])
    person = Musician(name="Session Player")
    _db.session.add(person)
    _db.session.flush()
    _db.session.add(Membership(artist_id=shared_artist.id, musician_id=person.id, order=0))
    _db.session.add(Membership(artist_id=secret_artist.id, musician_id=person.id, order=1))
    _db.session.commit()

    c = app.test_client()
    r = c.get(f"/api/share/musicians/{person.id}", headers=_auth(world))
    assert r.status_code == 200
    names = {p["name"] for p in r.get_json()["artists"]}
    assert names == {"Shared Band"}


def test_ungranted_musician_is_403(app, world):
    c = app.test_client()
    r = c.get(f"/api/share/musicians/{world['secret']['musician']}", headers=_auth(world))
    assert r.status_code == 403


# ── Genre ─────────────────────────────────────────────────────────────────────

def test_genres_omit_those_with_nothing_visible(app, world):
    c = app.test_client()
    r = c.get("/api/share/genres/", headers=_auth(world))
    assert r.status_code == 200
    names = {g["name"] for g in r.get_json()}
    assert "Jazz" in names
    assert "Funk" not in names, "named a genre the peer has no access to"


def test_genre_detail_is_reachable_and_scoped(app, world):
    """The list endpoint existed without a detail endpoint, so clicking a genre
    in the sidebar answered 404 and the UI said 'This genre no longer exists'.
    A list whose items cannot be opened is worse than no list."""
    c = app.test_client()
    gid = world["shared"]["genre"]
    r = c.get(f"/api/share/genres/{gid}", headers=_auth(world))
    assert r.status_code == 200
    body = r.get_json()
    assert body["name"] == "Jazz"
    assert body["artist_count"] == 1
    assert body["recording_count"] == 1
    names = {p["name"] for p in body["artists"]}
    assert names == {"Shared Band"}


def test_ungranted_genre_detail_is_refused(app, world):
    c = app.test_client()
    r = c.get(f"/api/share/genres/{world['secret']['genre']}", headers=_auth(world))
    assert r.status_code in (403, 404)


@pytest.mark.parametrize("path", [
    "/api/share/collections",
    "/api/share/collections/",
    "/api/share/genres",
    "/api/share/genres/",
])
def test_trailing_slash_is_tolerated(app, world, path):
    """The consumer proxies paths built by the LOCAL api.js, which is
    inconsistent about trailing slashes (/api/collections/ vs /api/venues/12).
    A mismatch here 404s and renders as an empty library — which is exactly how
    the granted collection went missing from the sidebar."""
    c = app.test_client()
    assert c.get(path, headers=_auth(world)).status_code == 200


def test_genre_counts_are_scoped(app, world):
    c = app.test_client()
    r = c.get("/api/share/genres/", headers=_auth(world))
    jazz = next(g for g in r.get_json() if g["name"] == "Jazz")
    assert jazz["artist_count"] == 1
    assert jazz["recording_count"] == 1


# ── The door itself ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/share/artists/1",
    "/api/share/artists/1/recordings",
    "/api/share/artists/images/1",
    "/api/share/venues/1",
    "/api/share/musicians/1",
    "/api/share/genres/",
])
def test_every_entity_endpoint_requires_a_token(app, world, path):
    """No token, no entity page — asserted per route rather than once, so a
    new endpoint added without @peer_required fails here."""
    c = app.test_client()
    assert c.get(path).status_code == 401


def test_recording_detail_carries_nav_ids(app, world):
    """Without artist_id and venue_id the entity pages are unreachable —
    the frontend builds #/artist/<id> and #/venue/<id> from these."""
    c = app.test_client()
    r = c.get(f"/api/share/recordings/{world['shared']['recording']}", headers=_auth(world))
    assert r.status_code == 200
    body = r.get_json()
    assert body["artist_id"] == world["shared"]["artist"]
    assert body["venue_id"] == world["shared"]["venue"]


def test_collection_detail_carries_card_fields(app, world):
    """Handbill cards need genre/genre_color/image_id. Without card=True the
    peer's Browse renders colourless cards with initials for every photo."""
    c = app.test_client()
    r = c.get(f"/api/share/collections/{world['collection'].id}", headers=_auth(world))
    assert r.status_code == 200
    row = r.get_json()["recordings"][0]
    for key in ("genre", "genre_color", "image_id"):
        assert key in row
