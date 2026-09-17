"""
tests/test_peer_visible_set.py — the visible set (peer sharing milestone 2).

Milestone 1 proved a peer can only STREAM what was granted. Milestone 2 gives
peers real entity pages — artist, venue, musician, genre, search, Browse — and
those are built on the inverse question: what is this peer's entire world?

The leak these tests exist to prevent is NOT the obvious one. Recording lists
are easy to get right and everyone remembers to filter them. The dangerous leak
is derived: a artist, venue, musician or genre made visible by a recording the
peer was never granted, and any count computed over the full library.

So the shape of this file is: build a library with TWO disjoint halves, grant
exactly one, and assert the other half is invisible in every derived dimension.

Fixtures deliberately build their own second half rather than extending
conftest._seed(), so no existing test's expectations shift.
"""

import pytest

from app.extensions import db as _db
from app.models.musician import Musician, Membership
from app.models.collection import Collection, CollectionRecording
from app.models.genre import Genre
from app.models.peer import Peer, CollectionGrant
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.recording import Recording
from app.models.venue import Venue

from app.utils.peer_access import (
    peer_can_access_recording_id,
    peer_visible_recording_ids,
    peer_visible_performance_ids,
    peer_visible_artist_ids,
    peer_visible_venue_ids,
    peer_visible_musician_ids,
    peer_visible_genre_ids,
    peer_can_access_artist,
    peer_can_access_venue,
    peer_can_access_musician,
    peer_can_access_genre,
)


# ── World building ────────────────────────────────────────────────────────────

def _build_half(label, genre_name):
    """A complete, self-contained slice: genre → artist → member musician →
    venue → performance → recording. Returns the ids as a dict."""
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
    _db.session.flush()
    _db.session.commit()

    return {"genre": genre.id, "artist": artist.id, "musician": musician.id,
            "venue": venue.id, "performance": perf.id, "recording": rec.id}


def _collection_with(name, recording_ids):
    col = Collection(name=name)
    _db.session.add(col)
    _db.session.flush()
    for i, rid in enumerate(recording_ids):
        _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rid, order=i))
    _db.session.commit()
    return col


def _peer_granted(collections, name="Matt"):
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    for col in collections:
        _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    _db.session.commit()
    return peer


@pytest.fixture()
def two_halves(app):
    """SHARED is granted; SECRET never is. Every assertion below is some form
    of 'SECRET does not appear'."""
    shared = _build_half("Shared", "Jazz")
    secret = _build_half("Secret", "Funk")
    shared_col = _collection_with("Shared Box", [shared["recording"]])
    secret_col = _collection_with("Secret Box", [secret["recording"]])
    peer = _peer_granted([shared_col])
    return {"shared": shared, "secret": secret, "peer": peer,
            "shared_col": shared_col, "secret_col": secret_col}


# ── The root set ──────────────────────────────────────────────────────────────

def test_no_grants_means_empty_world(app):
    peer = _peer_granted([], name="Ungranted")
    assert peer_visible_recording_ids(peer) == set()
    assert peer_visible_artist_ids(peer) == set()
    assert peer_visible_venue_ids(peer) == set()
    assert peer_visible_musician_ids(peer) == set()
    assert peer_visible_genre_ids(peer) == set()


def test_none_peer_is_empty_not_everything(app):
    """A null peer must fail closed. If this ever returns the full library,
    every unauthenticated path becomes a full catalog dump."""
    assert peer_visible_recording_ids(None) == set()


def test_visible_recordings_are_exactly_the_granted_collection(two_halves):
    peer = two_halves["peer"]
    assert peer_visible_recording_ids(peer) == {two_halves["shared"]["recording"]}


# ── Derived dimensions — the real leak surface ────────────────────────────────

def test_ungranted_artist_is_invisible(two_halves):
    peer = two_halves["peer"]
    visible = peer_visible_artist_ids(peer)
    assert two_halves["shared"]["artist"] in visible
    assert two_halves["secret"]["artist"] not in visible
    assert not peer_can_access_artist(peer, two_halves["secret"]["artist"])


def test_ungranted_venue_is_invisible(two_halves):
    peer = two_halves["peer"]
    visible = peer_visible_venue_ids(peer)
    assert two_halves["shared"]["venue"] in visible
    assert two_halves["secret"]["venue"] not in visible
    assert not peer_can_access_venue(peer, two_halves["secret"]["venue"])


def test_ungranted_musician_is_invisible(two_halves):
    peer = two_halves["peer"]
    visible = peer_visible_musician_ids(peer)
    assert two_halves["shared"]["musician"] in visible
    assert two_halves["secret"]["musician"] not in visible
    assert not peer_can_access_musician(peer, two_halves["secret"]["musician"])


def test_ungranted_genre_is_invisible(two_halves):
    """The genre chip is the sneakiest leak: it is a tiny piece of UI that
    would happily count the whole library if handed an unfiltered query."""
    peer = two_halves["peer"]
    visible = peer_visible_genre_ids(peer)
    assert two_halves["shared"]["genre"] in visible
    assert two_halves["secret"]["genre"] not in visible
    assert not peer_can_access_genre(peer, two_halves["secret"]["genre"])


def test_visible_performances_exclude_ungranted(two_halves):
    peer = two_halves["peer"]
    visible = peer_visible_performance_ids(peer)
    assert visible == {two_halves["shared"]["performance"]}


# ── Consistency between the two access paths ──────────────────────────────────

def test_per_recording_check_agrees_with_visible_set(two_halves):
    """
    `peer_can_access_recording_id` (milestone 1, used by stream) and
    `peer_visible_recording_ids` (milestone 2, used by browse) are separate
    implementations of the same question. If they ever disagree, a peer either
    sees something unplayable or plays something unlisted.

    Asserted over EVERY recording in the database, not a sample.
    """
    peer = two_halves["peer"]
    visible = peer_visible_recording_ids(peer)
    all_ids = [r.id for r in _db.session.query(Recording).all()]
    assert len(all_ids) >= 3, "expected conftest seed plus both halves"
    for rid in all_ids:
        assert peer_can_access_recording_id(peer, rid) == (rid in visible), (
            f"disagreement on recording {rid}")


# ── Grant lifecycle ───────────────────────────────────────────────────────────

def test_revoking_the_grant_empties_the_world(two_halves):
    from datetime import datetime, timezone
    peer = two_halves["peer"]
    assert peer_visible_recording_ids(peer)

    for grant in peer.grants:
        grant.revoked_at = datetime.now(timezone.utc)
    _db.session.commit()

    assert peer_visible_recording_ids(peer) == set()
    assert peer_visible_artist_ids(peer) == set()


def test_recording_in_both_granted_and_ungranted_collection_is_visible(two_halves):
    """Documented intent (Design Spec v1): sharing a collection shares every
    recording in it, even one that also sits in a collection not granted.
    Pinned as a test so it can't be 'fixed' into a surprise later."""
    peer = two_halves["peer"]
    shared_rec = two_halves["shared"]["recording"]
    _db.session.add(CollectionRecording(
        collection_id=two_halves["secret_col"].id, recording_id=shared_rec, order=1))
    _db.session.commit()
    assert shared_rec in peer_visible_recording_ids(peer)


def test_granting_a_second_collection_widens_the_world(two_halves):
    peer = two_halves["peer"]
    _db.session.add(CollectionGrant(
        peer_id=peer.id, collection_id=two_halves["secret_col"].id))
    _db.session.commit()
    assert peer_visible_recording_ids(peer) == {
        two_halves["shared"]["recording"], two_halves["secret"]["recording"]}


# ── Memoization ───────────────────────────────────────────────────────────────

def test_memo_does_not_bleed_between_peers(two_halves, app):
    """The per-request memo is keyed by peer id. Two peers resolved in one
    request context must not inherit each other's world."""
    peer_a = two_halves["peer"]
    peer_b = _peer_granted([two_halves["secret_col"]], name="Other")

    with app.test_request_context("/"):
        world_a = peer_visible_recording_ids(peer_a)
        world_b = peer_visible_recording_ids(peer_b)
        assert world_a == {two_halves["shared"]["recording"]}
        assert world_b == {two_halves["secret"]["recording"]}
        assert peer_visible_recording_ids(peer_a) == world_a   # memo hit, same answer
